"""
handlers/afk.py
Sistema AFK / BRB.

- Escribiendo "brb" (sin necesidad de "/") a inicio de mensaje, opcionalmente
  seguido de un motivo (ej. "brb almorzando"), se activa el estado AFK.
  También se mantiene /brb como alias por compatibilidad.
- Al responder o mencionar a un usuario AFK: se muestra su motivo y tiempo ausente.
- Al escribir cualquier mensaje, si el usuario estaba AFK, se desactiva
  automáticamente y se muestra un mensaje de bienvenida (una sola vez).
- El AFK puede activarse/desactivarse por grupo desde el menú de botones
  (⚙️ Configuración → 💤 AFK / BRB).

Rendimiento: el estado AFK se mantiene en un diccionario en memoria
(`context.application.bot_data["afk_cache"]`), indexado por user_id,
por lo que no se recorre la base de datos en cada mensaje. La base de
datos solo se usa para persistir el estado entre reinicios.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from telegram import MessageEntity, Update
from telegram.constants import MessageEntityType, ParseMode
from telegram.ext import ContextTypes

from database import AfkRecord, Database
from utils.formatting import escape_md, humanize_seconds, mention

logger = logging.getLogger(__name__)

# Coincide con "brb" al inicio del mensaje (sin importar mayúsculas/minúsculas),
# seguido opcionalmente de un motivo. No dispara con palabras como "brbrb".
_BRB_TEXT_PATTERN = re.compile(r"^brb\b[\s:,-]*(.*)$", re.IGNORECASE | re.DOTALL)


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _get_cache(context: ContextTypes.DEFAULT_TYPE) -> dict[int, AfkRecord]:
    return context.application.bot_data["afk_cache"]


async def load_afk_cache(db: Database) -> dict[int, AfkRecord]:
    """Carga el estado AFK persistido en la base de datos hacia memoria al iniciar el bot."""
    records = await db.get_all_afk()
    cache = {record.user_id: record for record in records}
    logger.info("Caché AFK cargada con %d usuario(s).", len(cache))
    return cache


async def _is_afk_enabled_here(db: Database, chat) -> bool:
    if chat.type not in ("group", "supergroup"):
        return True
    settings = await db.get_group_settings(chat.id)
    return settings.afk_enabled


async def _activate_afk(update: Update, context: ContextTypes.DEFAULT_TYPE, reason: Optional[str]) -> None:
    user = update.effective_user
    chat = update.effective_chat
    db = _get_db(context)
    cache = _get_cache(context)

    if not await _is_afk_enabled_here(db, chat):
        await update.effective_message.reply_text(
            "⚠️ El estado AFK está desactivado en este grupo\\.", parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    record = await db.set_afk(
        user_id=user.id,
        first_name=user.first_name,
        username=user.username,
        group_id=chat.id if chat.type in ("group", "supergroup") else None,
        group_name=chat.title if chat.type in ("group", "supergroup") else None,
        reason=reason,
    )
    cache[user.id] = record

    reason_display = escape_md(reason) if reason else "No especificado"
    text = (
        f"💤 *{mention(user.id, user.first_name)} ahora está ausente \\(AFK\\)*\n"
        f"📝 Motivo: {reason_display}"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def brb_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Alias de compatibilidad: /brb [motivo]. El uso recomendado es escribir "brb" directamente."""
    reason = " ".join(context.args).strip() or None
    await _activate_afk(update, context, reason)


async def brb_text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Detecta el mensaje de texto plano "brb" (sin "/") al inicio del mensaje.
    Devuelve True si el mensaje fue consumido como activación de AFK.
    """
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or user.is_bot:
        return False

    text = message.text or ""
    match = _BRB_TEXT_PATTERN.match(text.strip())
    if not match:
        return False

    reason = match.group(1).strip() or None
    await _activate_afk(update, context, reason)
    return True


def _extract_mentioned_usernames(message) -> list[str]:
    usernames: list[str] = []
    text = message.text or message.caption or ""
    for entity in (message.entities or []) + (message.caption_entities or []):
        if entity.type == MessageEntityType.MENTION:
            usernames.append(text[entity.offset: entity.offset + entity.length].lstrip("@").lower())
    return usernames


def _extract_text_mention_ids(message) -> list[int]:
    ids: list[int] = []
    for entity in (message.entities or []) + (message.caption_entities or []):
        if entity.type == MessageEntityType.TEXT_MENTION and entity.user:
            ids.append(entity.user.id)
    return ids


async def _build_afk_notice(record: AfkRecord) -> str:
    elapsed = int(time.time()) - record.since
    reason_display = escape_md(record.reason) if record.reason else "No especificado"
    return (
        f"💤 {mention(record.user_id, record.first_name)} está ausente \\(AFK\\)\n"
        f"📝 Motivo: {reason_display}\n"
        f"⏱ Tiempo ausente: {escape_md(humanize_seconds(elapsed))}"
    )


async def track_and_check_afk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Middleware ejecutado en cada mensaje no-comando:
    1) Actualiza el caché de usuarios (para resolver @username en comandos).
    2) Si el autor estaba AFK, lo da de baja y da la bienvenida (una sola vez).
    3) Si el mensaje responde o menciona a un usuario AFK, muestra su estado.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if message is None or user is None or user.is_bot:
        return

    db = _get_db(context)
    cache = _get_cache(context)

    # 1) Mantener el caché de usuarios actualizado para resolución @username -> ID
    await db.upsert_user(user.id, user.username, user.first_name)
    if chat is not None and chat.type in ("group", "supergroup"):
        await db.upsert_group(chat.id, chat.title)
        if not await _is_afk_enabled_here(db, chat):
            return  # AFK desactivado en este grupo: no procesamos ni avisamos nada más.

    # 2) ¿El autor estaba AFK? -> bienvenida (una sola vez) y salimos.
    record = cache.pop(user.id, None)
    if record is not None:
        await db.remove_afk(user.id)
        elapsed = int(time.time()) - record.since
        reason_display = escape_md(record.reason) if record.reason else "No especificado"
        text = (
            f"👋 *¡Bienvenido de nuevo, {escape_md(user.first_name)}\\!*\n"
            f"⏱ Estuviste ausente: {escape_md(humanize_seconds(elapsed))}\n"
            f"📝 Motivo: {reason_display}"
        )
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        return

    if not cache:
        return  # Nadie está AFK, no hay nada más que comprobar.

    notices: list[str] = []
    checked: set[int] = set()

    # 3a) Respuesta a un mensaje de un usuario AFK
    if message.reply_to_message and message.reply_to_message.from_user:
        target_id = message.reply_to_message.from_user.id
        target_record = cache.get(target_id)
        if target_record and target_id not in checked:
            notices.append(await _build_afk_notice(target_record))
            checked.add(target_id)

    # 3b) Menciones por @username
    for username in _extract_mentioned_usernames(message):
        target_id = await db.get_user_id_by_username(username)
        if target_id is None:
            # Respaldo: si nunca vimos a ese usuario escribir un mensaje,
            # a Telegram directamente para resolver el @username (funciona
            # con cualquier username público, no depende de nuestra caché).
            try:
                chat_info = await context.bot.get_chat(f"@{username}")
                target_id = chat_info.id
            except Exception:  # noqa: BLE001 - username no existe o no es resoluble
                target_id = None
        if target_id and target_id not in checked:
            target_record = cache.get(target_id)
            if target_record:
                notices.append(await _build_afk_notice(target_record))
                checked.add(target_id)

    # 3c) Menciones directas (text_mention, usuarios sin @username)
    for target_id in _extract_text_mention_ids(message):
        if target_id not in checked:
            target_record = cache.get(target_id)
            if target_record:
                notices.append(await _build_afk_notice(target_record))
                checked.add(target_id)

    if notices:
        await message.reply_text("\n\n".join(notices), parse_mode=ParseMode.MARKDOWN_V2)
