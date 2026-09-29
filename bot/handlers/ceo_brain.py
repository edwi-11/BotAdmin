"""
handlers/ceo_brain.py
CEO Brain: aprendizaje de patrones de moderación, memoria independiente
por cada chat.

/brain              — activa el aprendizaje en ESTE chat.
/nobrain            — lo desactiva (la memoria queda guardada para cuando
                       se reactive con /brain).
/ceochat <idGrupo>  — se ejecuta DENTRO del grupo de staff que se quiere
                       usar como bandeja: liga ese grupo de staff con el
                       grupo <idGrupo> que se va a monitorear. A partir de
                       ahí, a ese grupo de staff llegan tanto los reportes
                       de usuarios (/reportar, "@admin" — ver
                       handlers/reports.py) como el aviso de cada acción
                       automática que tome CEO Brain en <idGrupo>.

Cómo aprende:
Cuando Brain está activo y un administrador ejecuta un comando real de
moderación (/ban, /kick, /mute, /warn, /delban, /delkick, /delwarn, /del)
RESPONDIENDO al mensaje del usuario sancionado, se guarda la pareja
"texto del mensaje" -> "acción tomada" (+ motivo/duración si los hubo) en
brain_patterns — memoria exclusiva de ese chat (ver record_pattern, llamada
desde el final de esos comandos en handlers/moderation.py y
handlers/utils_cmds.py).

Cómo actúa:
Con Brain activo, cada mensaje de un usuario NO administrador en el grupo
se compara (vía IA, mismo proveedor que el resto del bot — ver
handlers.gemini_chat._ask_ai) contra los patrones guardados de ESE chat. Si
hay un patrón muy similar y claro, CEO ejecuta la misma acción directo
(ban/kick/mute/warn/borrar) y avisa en el grupo de staff configurado (si
hay uno) qué hizo y por qué. Nunca actúa contra administradores ni contra
el propietario del bot, y nunca sin que el bot tenga los permisos
necesarios. Si algo falla en la clasificación o en la acción, simplemente
no actúa — nunca revienta el procesamiento normal de mensajes del grupo.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import ChatPermissions, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database
from handlers.gemini_chat import _ask_ai  # mismo Gemini+Groq que el resto del chat de CEO
from utils.formatting import error, escape_md, mention, success
from utils.permissions import check_bot_rights, check_group_owner_or_cofounder, is_owner, is_real_admin

logger = logging.getLogger(__name__)


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


# --------------------------------------------------------------------- #
# /brain y /nobrain
# --------------------------------------------------------------------- #
async def brain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona dentro de un grupo."))
        return

    perm = await check_group_owner_or_cofounder(context.bot, chat.id, user.id)
    if not perm.allowed:
        await message.reply_text(error(perm.reason))
        return

    db = _get_db(context)
    await db.enable_brain(chat.id)
    await message.reply_text(
        success(
            "🧠 CEO Brain activado en este chat. Voy a aprender de las acciones de moderación "
            "que hagan los administradores (respondiendo al mensaje del usuario) y, si veo una "
            "situación muy parecida a algo ya aprendido, voy a actuar solo."
        )
    )


async def nobrain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona dentro de un grupo."))
        return

    perm = await check_group_owner_or_cofounder(context.bot, chat.id, user.id)
    if not perm.allowed:
        await message.reply_text(error(perm.reason))
        return

    db = _get_db(context)
    await db.disable_brain(chat.id)
    await message.reply_text(
        success(
            "🧠 CEO Brain desactivado en este chat. Dejo de actuar y de aprender acá, pero "
            "guardo lo aprendido hasta ahora por si lo reactivás con /brain."
        )
    )


# --------------------------------------------------------------------- #
# /ceochat <idGrupo> — se corre DENTRO del grupo de staff que va a
# recibir los reportes y los avisos de acciones de CEO Brain.
# --------------------------------------------------------------------- #
async def ceochat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    if not is_owner(user.id):
        await message.reply_text(error("Solo el propietario del bot puede configurar esto."))
        return

    if not context.args or not context.args[0].lstrip("-").isdigit():
        await message.reply_text(
            error(
                "Usá /ceochat <idGrupo> escribiendo DENTRO del grupo de staff donde querés que "
                "lleguen los reportes de usuarios y los avisos de las acciones de CEO Brain."
            )
        )
        return

    target_group_id = int(context.args[0])
    db = _get_db(context)
    await db.set_brain_report_chat(target_group_id, chat.id)
    await message.reply_text(
        success(f"Listo: los reportes y las acciones de CEO Brain del grupo `{target_group_id}` "
                "van a llegar a este chat."),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# --------------------------------------------------------------------- #
# Registro de patrones — llamado desde el final de los comandos reales
# de moderación (handlers/moderation.py, handlers/utils_cmds.py), SOLO
# tras el éxito de la acción.
# --------------------------------------------------------------------- #
async def record_pattern(
    update: Update, context: ContextTypes.DEFAULT_TYPE, action: str,
    reason: Optional[str] = None, duration_seconds: Optional[int] = None,
) -> None:
    if getattr(context, "_ceo_brain_auto", False):
        return  # nunca aprender de sus propias acciones automáticas (evita retroalimentarse)

    message = update.effective_message
    chat = update.effective_chat
    if chat is None or message is None or not message.reply_to_message:
        return  # sin mensaje-situación no hay nada que aprender

    situation = message.reply_to_message.text or message.reply_to_message.caption
    if not situation:
        return

    db = _get_db(context)
    if not await db.is_brain_enabled(chat.id):
        return

    if reason == "No especificado":
        reason = None
    await db.add_brain_pattern(chat.id, action, situation, reason, duration_seconds)


# --------------------------------------------------------------------- #
# Auto-detección: MessageHandler sobre TODOS los mensajes de texto/caption
# de un grupo (se registra en main.py, corre en TODOS los grupos, pero
# solo actúa donde /brain está activado).
# --------------------------------------------------------------------- #
_ACTIONS = ("BAN", "KICK", "MUTE", "WARN", "DEL")

_CLASSIFY_PROMPT = (
    "Sos el sistema de moderación automática de un grupo de Telegram (CEO Brain). Te paso una "
    "lista de patrones reales que los administradores de ESTE grupo ya aplicaron antes "
    "(situación -> acción) y un mensaje NUEVO. Tu tarea es decidir si el mensaje nuevo es una "
    "situación TAN parecida a alguno de esos patrones que amerite tomar la MISMA acción, o si no "
    "hay ningún patrón claramente aplicable.\n\n"
    "Devolvé ÚNICAMENTE un JSON (sin texto alrededor, sin bloques de código) con esta forma "
    "exacta: "
    '{"action": "BAN|KICK|MUTE|WARN|DEL|NONE", "reason": "motivo corto", '
    '"duration_seconds": numero o null}\n\n'
    "Reglas MUY importantes:\n"
    "- Si tenés la MÍNIMA duda, o el mensaje nuevo no se parece con claridad a ningún patrón, "
    "devolvé action=\"NONE\". Es preferible no actuar a actuar mal.\n"
    "- NUNCA inventes una acción que no esté en la lista de patrones de este grupo.\n"
    "- 'duration_seconds' solo aplica a MUTE: copiá la duración del patrón más parecido si la "
    "tenía, si no, null (silencio indefinido).\n"
    "- 'reason' es un motivo breve basado en el patrón que reconociste.\n\n"
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _format_patterns(patterns: list[dict]) -> str:
    lines = []
    for p in patterns:
        extra = f" (duración: {p['duration_seconds']}s)" if p.get("duration_seconds") else ""
        motivo = f" — motivo: {p['reason']}" if p.get("reason") else ""
        lines.append(f'- Situación: "{p["situation"]}" -> Acción: {p["action"]}{extra}{motivo}')
    return "\n".join(lines)


async def check_brain_and_maybe_act(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        return
    if user is None or user.is_bot:
        return
    text = message.text or message.caption
    if not text:
        return

    db = _get_db(context)
    if not await db.is_brain_enabled(chat.id):
        return

    # Nunca vigilar/actuar contra administradores ni contra el propio dueño.
    if is_owner(user.id) or await is_real_admin(context.bot, chat.id, user.id):
        return

    patterns = await db.get_brain_patterns(chat.id, limit=40)
    if not patterns:
        return  # todavía no aprendió nada en este chat, no gastamos IA en vano

    prompt = (
        _CLASSIFY_PROMPT
        + f"Patrones aprendidos en este grupo:\n{_format_patterns(patterns)}\n\n"
        + "Mensaje nuevo: " + text
    )
    try:
        raw = await _ask_ai(prompt)
    except Exception as exc:  # noqa: BLE001
        logger.info("CEO Brain no pudo clasificar (no actúa): %s", exc)
        return

    match = _JSON_RE.search(raw)
    if not match:
        return
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return

    action = str(data.get("action", "NONE")).upper()
    if action not in _ACTIONS:
        return

    bot_rights = await check_bot_rights(context.bot, chat.id)
    if not bot_rights.allowed:
        return  # el bot no tiene permisos, ni lo intenta

    reason = (data.get("reason") or "Patrón aprendido por CEO Brain").strip()
    duration_seconds = data.get("duration_seconds")

    setattr(context, "_ceo_brain_auto", True)
    try:
        if action == "DEL":
            await message.delete()
            resumen = f"🗑 Borré un mensaje de {mention(user.id, user.first_name)}."
        elif action == "BAN":
            await context.bot.ban_chat_member(chat.id, user.id)
            resumen = f"🔨 Baneé a {mention(user.id, user.first_name)}."
        elif action == "KICK":
            await context.bot.ban_chat_member(chat.id, user.id)
            await context.bot.unban_chat_member(chat.id, user.id, only_if_banned=True)
            resumen = f"👢 Expulsé a {mention(user.id, user.first_name)}."
        elif action == "MUTE":
            until_date = None
            if duration_seconds:
                try:
                    until_date = datetime.now(timezone.utc) + timedelta(seconds=int(duration_seconds))
                except (TypeError, ValueError):
                    until_date = None
            await context.bot.restrict_chat_member(
                chat.id, user.id,
                permissions=ChatPermissions(can_send_messages=False, can_send_other_messages=False,
                                             can_send_polls=False, can_add_web_page_previews=False),
                until_date=until_date,
            )
            resumen = f"🔇 Silencié a {mention(user.id, user.first_name)}."
        elif action == "WARN":
            # Reutiliza el mismo límite/castigo configurado para /warn.
            from handlers.moderation import _apply_warn_punishment  # import perezoso, evita ciclo
            group_settings = await db.get_group_settings(chat.id)
            count = await db.add_warning(chat.id, user.id)
            if count >= group_settings.warn_limit:
                await db.reset_warnings(chat.id, user.id)
                punishment_line = await _apply_warn_punishment(context, chat.id, user.id, group_settings)
                resumen = (
                    f"❗ {mention(user.id, user.first_name)} llegó a {count} advertencias.\n"
                    f"{punishment_line}"
                )
            else:
                resumen = (
                    f"❗ Le di una advertencia a {mention(user.id, user.first_name)} "
                    f"({count}/{group_settings.warn_limit})."
                )
        else:
            return
    except TelegramError as exc:
        logger.warning("CEO Brain: falló la acción %s en %s: %s", action, chat.id, exc)
        return
    finally:
        setattr(context, "_ceo_brain_auto", False)

    await db.add_log(f"brain_{action.lower()}", context.bot.id, "CEO Brain", user.id,
                      user.first_name, chat.id, chat.title, reason)

    aviso = (
        f"🧠 *CEO Brain actuó en {escape_md(chat.title or str(chat.id))}*\n"
        f"{resumen}\n"
        f"📝 Motivo: {escape_md(reason)}"
    )
    report_chat_id = await db.get_brain_report_chat(chat.id)
    if report_chat_id:
        try:
            await context.bot.send_message(report_chat_id, aviso, parse_mode=ParseMode.MARKDOWN_V2)
        except TelegramError as exc:
            logger.info("No pude avisar la acción de CEO Brain al grupo de staff: %s", exc)
