"""
handlers/ceo_moderation.py
Capa de lenguaje natural sobre el trigger de "CEO" (ver
handlers/gemini_chat.py): permite que, en vez de escribir el comando "/"
exacto, un admin le diga a CEO en lenguaje natural qué acción de
moderación quiere —"CEO banealo", "CEO mutealo 10 minutos", "CEO borra
esto", "CEO dale admin a @fulano"— y CEO la traduzca a una de las
acciones REALES que ya existen en el bot, ejecutando exactamente la
misma función que corre el comando "/" correspondiente: mismos
permisos, misma resolución de usuario, mismos mensajes, mismos
stickers, mismo log. Esta capa nunca inventa un comando, un usuario o
un ID que no exista.

Cómo funciona:
1. `classify_moderation_intent` le pide al mismo proveedor de IA que ya
   usa el chat normal (Gemini, con Groq de respaldo — ver
   handlers.gemini_chat._ask_ai) que devuelva una intención en JSON
   estricto: acción + duración + motivo. Si el texto no es una orden de
   moderación (o si la IA falla por cualquier motivo), se devuelve
   acción NONE / None y el flujo normal de chat sigue como si nada — un
   error acá NUNCA debe impedir la charla normal con CEO.
2. El OBJETIVO (a quién aplicar la acción) nunca lo decide la IA: se
   resuelve exactamente igual que en los comandos reales — respondiendo
   al mensaje de esa persona (siempre tiene prioridad, igual que en
   utils/parsing.py:resolve_target) o mencionándola con un @usuario REAL
   (entidad de Telegram) en el mismo mensaje. Si no hay ninguna de las
   dos cosas, se le pide aclaración al que escribió, nunca se adivina.
3. Con la intención + el objetivo resueltos, se llama DIRECTO a la
   función real del comando (de handlers/moderation.py,
   handlers/admin.py y handlers/utils_cmds.py), fijando `context.args`
   con lo que se haya extraído (usuario/duración/motivo) para que la
   función corra exactamente igual que si se hubiera escrito el comando
   a mano — todas las validaciones de permisos existentes (can_moderate,
   check_executor_is_admin, check_bot_rights, can_grant_admin) se
   ejecutan sin cambios dentro de esas mismas funciones.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from config import settings
from handlers.admin import admin_command, unadmin_command
from handlers.gemini_chat import _ask_ai  # reutiliza el mismo Gemini+Groq que el chat normal
from handlers.moderation import (
    ban_command, kick_command, mute_command, unban_command,
    unmute_command, unwarn_command, warn_command,
)
from handlers.utils_cmds import del_command

logger = logging.getLogger(__name__)

_ACTIONS = {
    "BAN": ban_command,
    "UNBAN": unban_command,
    "KICK": kick_command,
    "MUTE": mute_command,
    "UNMUTE": unmute_command,
    "WARN": warn_command,
    "UNWARN": unwarn_command,
    "ADMIN": admin_command,
    "UNADMIN": unadmin_command,
    "DEL": del_command,
}

# Todas menos DEL necesitan un usuario objetivo (DEL actúa sobre el
# mensaje respondido, no sobre una persona puntual).
_NEEDS_TARGET = set(_ACTIONS) - {"DEL"}

# ADMIN es el único que, si no hay objetivo identificable, puede recaer
# en "a mí mismo" (mismo fallback que ya tiene admin_command para
# "/admin" a secas) en vez de pedir aclaración.
_SELF_TARGET_FALLBACK = {"ADMIN"}

_CLASSIFY_PROMPT = (
    "Sos un clasificador de intención para un bot de moderación de grupos de Telegram. "
    "Te paso un mensaje que alguien le escribió al bot (después de la palabra de activación "
    "'CEO'). Tu ÚNICA tarea es decidir si es una ORDEN DE MODERACIÓN o charla normal, y "
    "devolver ÚNICAMENTE un JSON (sin texto alrededor, sin bloques de código) con esta forma "
    "exacta:\n\n"
    '{"action": "BAN|UNBAN|KICK|MUTE|UNMUTE|WARN|UNWARN|ADMIN|UNADMIN|DEL|NONE|AMBIGUOUS", '
    '"duration": "10m" o null, "reason": "texto o null"}\n\n'
    "Reglas:\n"
    "- BAN: banear/expulsar para siempre, que no vuelva nunca, bloquear del grupo.\n"
    "- KICK: expulsar pero SIN prohibir que vuelva a entrar (echar, sacar, patear).\n"
    "- MUTE: silenciar, callar, que no pueda escribir. UNMUTE: dejarlo hablar de nuevo.\n"
    "- WARN: dar una advertencia (no es lo mismo que banear/mutear). UNWARN: quitarle una.\n"
    "- ADMIN: darle privilegios de administrador. UNADMIN: quitárselos.\n"
    "- DEL: borrar/eliminar el mensaje al que se está respondiendo.\n"
    "- UNBAN: revertir un ban.\n"
    "- 'duration' SOLO tiene sentido para MUTE, en formato compacto <número><unidad> con "
    "unidad de una sola letra entre s/m/h/d/w (segundos/minutos/horas/días/semanas), por "
    "ejemplo '30s', '10m', '2h', '1d'. Convertí cualquier expresión natural ('media hora', "
    "'diez minutos') a ese formato exacto. Si no mencionan duración, null — no la inventes.\n"
    "- 'reason' es el motivo SOLO si lo dieron explícitamente, si no, null. No inventes uno.\n"
    "- Si el mensaje NO es una orden de moderación (pregunta, charla, chiste, pedido de info, "
    "etc.), action = \"NONE\".\n"
    "- Si CLARAMENTE quieren que se haga algo pero no se entiende cuál de las acciones de "
    "arriba (ej. \"CEO haz algo con ese\"), action = \"AMBIGUOUS\".\n"
    "- Ante la duda entre una orden concreta de esta lista y charla normal, elegí NONE.\n\n"
    "Mensaje: __MENSAJE_A_CLASIFICAR__"
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


async def classify_moderation_intent(text: str) -> Optional[dict]:
    """Devuelve el dict de intención, o None si no se pudo clasificar
    (IA no configurada, falla de red, JSON inválido, acción desconocida,
    etc.) — quien llama debe tratar None igual que NONE (seguir con el
    chat normal), para que un error de clasificación nunca rompa la
    charla habitual con CEO."""
    if not settings.gemini_api_key and not settings.groq_api_key:
        return None
    try:
        raw = await _ask_ai(_CLASSIFY_PROMPT.replace("__MENSAJE_A_CLASIFICAR__", text))
    except Exception as exc:  # noqa: BLE001
        logger.info("No se pudo clasificar intención de moderación (sigue como chat normal): %s", exc)
        return None

    match = _JSON_RE.search(raw)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    action = str(data.get("action", "NONE")).upper()
    if action not in _ACTIONS and action not in ("NONE", "AMBIGUOUS"):
        return None
    return {
        "action": action,
        "duration": (data.get("duration") or None),
        "reason": (data.get("reason") or None),
    }


def _extract_mention(update: Update) -> Optional[str]:
    """Si el mensaje tiene un @username mencionado como entidad REAL de
    Telegram (nunca un nombre suelto que la IA se haya inventado), lo
    devuelve tal cual (con @) para que resolve_target lo procese
    exactamente igual que si se hubiera escrito a mano."""
    message = update.effective_message
    if not message or not message.entities or not message.text:
        return None
    for ent in message.entities:
        if ent.type == "mention":
            return message.text[ent.offset:ent.offset + ent.length]
    return None


async def try_handle_moderation_intent(
    update: Update, context: ContextTypes.DEFAULT_TYPE, remainder: str
) -> bool:
    """Punto de entrada desde ceo_trigger (handlers/gemini_chat.py).
    Devuelve True si el mensaje se manejó como una acción de moderación
    (con éxito, con error de permisos, o pidiendo aclaración) — en ese
    caso ceo_trigger NO debe seguir con el chat normal. Devuelve False
    si hay que seguir con el chat normal (no era una orden, o no se
    pudo clasificar)."""
    message = update.effective_message

    intent = await classify_moderation_intent(remainder)
    if intent is None or intent["action"] == "NONE":
        return False

    if intent["action"] == "AMBIGUOUS":
        await message.reply_text(
            "🤔 No me quedó claro qué querés que haga. Decime la acción bien clara "
            "(banear, mutear, expulsar, advertir, dar/quitar admin, borrar) y a quién, "
            "respondiendo su mensaje o mencionándolo con @usuario."
        )
        return True

    action = intent["action"]
    handler = _ACTIONS[action]
    args: list[str] = []

    if action in _NEEDS_TARGET:
        has_reply_target = bool(message.reply_to_message and message.reply_to_message.from_user)
        mention = _extract_mention(update)

        if not has_reply_target and not mention:
            if action in _SELF_TARGET_FALLBACK:
                pass  # admin_command ya sabe recaer en "a mí mismo" sin args
            else:
                await message.reply_text(
                    "🤔 ¿A quién? Respondé al mensaje de esa persona o mencionala con @usuario."
                )
                return True
        elif not has_reply_target:
            args.append(mention)

        if intent["duration"]:
            args.append(intent["duration"])
        if intent["reason"]:
            args.append(intent["reason"])
    else:
        # DEL: no hay usuario objetivo, actúa sobre el mensaje respondido
        # (del_command exige el reply, no inventamos ningún message ID).
        if not message.reply_to_message:
            await message.reply_text(
                "🤔 Para borrar un mensaje, respondé al mensaje que querés eliminar y "
                'decime "CEO borra esto".'
            )
            return True
        if intent["reason"]:
            args.append(intent["reason"])

    context.args = args
    await handler(update, context)
    return True
