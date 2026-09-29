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
                       Lo puede usar el propietario del bot Y el dueño (o
                       cofundador) de CUALQUIERA de los dos grupos: el de
                       staff o el monitoreado. Responde con un mensaje de
                       confirmación (nuevo / ya estaba configurado /
                       actualizado).

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

import html
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import ChatPermissions, Update
from telegram.constants import ParseMode
from telegram.error import ChatMigrated, TelegramError
from telegram.ext import ContextTypes

from database import Database
from handlers.gemini_chat import _ask_ai  # mismo Gemini+Groq que el resto del chat de CEO
from utils.formatting import error, success
from utils.permissions import PermissionResult, check_bot_rights, get_member, is_owner, is_real_admin

try:
    from utils.permissions import check_group_owner_or_cofounder
except ImportError:  # pragma: no cover - respaldo si esa utilidad no existe en esta versión
    async def check_group_owner_or_cofounder(bot, chat_id: int, user_id: int) -> PermissionResult:
        member = await get_member(bot, chat_id, user_id)
        if member is not None and member.status == "creator":
            return PermissionResult(True)
        return PermissionResult(False, "Solo el dueño del grupo puede usar este comando.")

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

    if chat.type not in ("group", "supergroup"):
        await message.reply_text(
            error("Usá /ceochat <idGrupo> escribiendo DENTRO del grupo de staff, no por privado.")
        )
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
    if target_group_id == chat.id:
        await message.reply_text(
            error("El grupo de staff no puede ser el mismo grupo que se monitorea. "
                  "Escribí /ceochat <idGrupo> dentro del grupo de staff, con el ID del OTRO grupo.")
        )
        return

    # Permiso: propietario del bot, o dueño/cofundador del grupo de staff
    # (donde se escribe el comando), o dueño/cofundador del grupo monitoreado.
    allowed = is_owner(user.id)
    if not allowed:
        for group_id in (chat.id, target_group_id):
            if (await check_group_owner_or_cofounder(context.bot, group_id, user.id)).allowed:
                allowed = True
                break
    if not allowed:
        await message.reply_text(
            error("Solo el propietario del bot, o el dueño/cofundador de alguno de los dos grupos "
                  "(el de staff o el monitoreado), puede configurar esto.")
        )
        return

    try:
        target_chat = await context.bot.get_chat(target_group_id)
    except TelegramError as exc:
        logger.info("/ceochat: no pude leer el grupo %s: %s", target_group_id, exc)
        await message.reply_text(
            error("No encuentro ese grupo. Revisá el ID (los supergrupos empiezan con -100) "
                  "y que el bot esté dentro de ese grupo.")
        )
        return
    if target_chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Ese ID no corresponde a un grupo."))
        return

    db = _get_db(context)
    previous = await db.get_brain_report_chat(target_group_id)
    target_title = html.escape(target_chat.title or str(target_group_id))
    target_line = f"📍 Grupo monitoreado: <b>{target_title}</b> (<code>{target_group_id}</code>)"

    if previous == chat.id:
        text = (
            "ℹ️ <b>Este chat de staff ya estaba configurado.</b>\n"
            f"{target_line}\n"
            "No hice ningún cambio: los reportes y las acciones de CEO Brain ya llegan aquí."
        )
    else:
        await db.set_brain_report_chat(target_group_id, chat.id)
        if previous is None:
            head = "✅ <b>Chat del staff configurado correctamente.</b>"
        else:
            head = (
                "🔄 <b>Chat del staff actualizado.</b>\n"
                f"Antes los avisos iban a <code>{previous}</code>; ahora llegan aquí."
            )
        text = (
            f"{head}\n{target_line}\n"
            "🚨 Aquí van a llegar los reportes de usuarios y las acciones de CEO Brain."
        )

    if not await db.is_brain_enabled(target_group_id):
        text += "\n⚠️ CEO Brain todavía no está activado en ese grupo: usá /brain allá para activarlo."

    await message.reply_text(text, parse_mode=ParseMode.HTML)


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

    who = f'<a href="tg://user?id={user.id}">{html.escape(user.first_name or "Usuario")}</a>'

    setattr(context, "_ceo_brain_auto", True)
    try:
        if action == "DEL":
            await message.delete()
            resumen = f"🗑 Borré un mensaje de {who}."
        elif action == "BAN":
            await context.bot.ban_chat_member(chat.id, user.id)
            resumen = f"🔨 Baneé a {who}."
        elif action == "KICK":
            await context.bot.ban_chat_member(chat.id, user.id)
            await context.bot.unban_chat_member(chat.id, user.id, only_if_banned=True)
            resumen = f"👢 Expulsé a {who}."
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
            resumen = f"🔇 Silencié a {who}."
        elif action == "WARN":
            # Reutiliza el mismo límite/castigo configurado para /warn.
            from handlers.moderation import _apply_warn_punishment  # import perezoso, evita ciclo
            group_settings = await db.get_group_settings(chat.id)
            count = await db.add_warning(chat.id, user.id)
            if count >= group_settings.warn_limit:
                await db.reset_warnings(chat.id, user.id)
                punishment_line = await _apply_warn_punishment(context, chat.id, user.id, group_settings)
                # punishment_line viene formateada para MarkdownV2: se limpia para HTML.
                punishment_html = html.escape(re.sub(r"\\(.)", r"\1", punishment_line))
                resumen = (
                    f"❗ {who} llegó a {count} advertencias.\n"
                    f"{punishment_html}"
                )
            else:
                resumen = f"❗ Le di una advertencia a {who} ({count}/{group_settings.warn_limit})."
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
        f"🧠 <b>CEO Brain actuó en {html.escape(chat.title or str(chat.id))}</b>\n"
        f"{resumen}\n"
        f"📝 Motivo: {html.escape(reason)}"
    )
    await _notify_staff(context, chat.id, aviso)


async def _notify_staff(context: ContextTypes.DEFAULT_TYPE, group_id: int, text_html: str) -> None:
    """Manda el aviso de una acción de CEO Brain al grupo de staff ligado con
    /ceochat. Usa HTML (no MarkdownV2) para que ningún carácter del texto lo
    haga fallar, y registra el error real si aun así no se puede enviar."""
    db = _get_db(context)
    report_chat_id = await db.get_brain_report_chat(group_id)
    if not report_chat_id:
        return
    try:
        await context.bot.send_message(
            report_chat_id, text_html, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except ChatMigrated as exc:
        # El grupo de staff pasó a supergrupo: se actualiza el vínculo y se reintenta.
        await db.set_brain_report_chat(group_id, exc.new_chat_id)
        try:
            await context.bot.send_message(
                exc.new_chat_id, text_html, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
        except TelegramError as exc2:
            logger.warning("CEO Brain: no pude avisar al staff (%s) tras migrar: %s", exc.new_chat_id, exc2)
    except TelegramError as exc:
        logger.warning(
            "CEO Brain: no pude avisar la acción al grupo de staff %s (grupo %s): %s",
            report_chat_id, group_id, exc,
        )
