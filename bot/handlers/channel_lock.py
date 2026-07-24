"""
handlers/channel_lock.py
/canal — comando oculto, solo para el propietario del bot, se usa por
privado (nunca dentro de un grupo).

Al mandarlo, recorre TODOS los grupos donde está el bot y, para cada uno:
  - Busca al dueño (creator) del grupo.
  - Revisa si ese dueño ya es miembro del canal de anuncios
    (t.me/CeoBotupdates).
  - Si YA está en el canal: no hace nada (no manda mensaje) y se asegura
    de que el grupo quede desbloqueado.
  - Si NO está en el canal: bloquea el grupo (el bot deja de responder
    ahí, ver `channel_gate`) y manda un mensaje etiquetando @ al dueño,
    con un botón para unirse al canal y otro para verificar que ya se
    unió.

Cuando el dueño toca "✅ Ya me uní" (`canal_verify_callback`) y de verdad
ya es miembro del canal, el grupo se desbloquea y el bot vuelve a
funcionar con normalidad ahí.

El bloqueo en sí (impedir que el bot procese comandos/mensajes mientras
un grupo está bloqueado) se aplica con `channel_gate`, un MessageHandler
de máxima prioridad registrado en main.py.
"""
from __future__ import annotations

import html
import logging
from typing import Optional

from telegram import (
    ChatMember,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    User,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from database import Database
from utils.callbacks import safe_callback
from utils.permissions import is_owner

logger = logging.getLogger(__name__)

CHANNEL_USERNAME = "@CeoBotupdates"
CHANNEL_URL = "https://t.me/CeoBotupdates"

_JOINED_STATUSES = {
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
}

_CALLBACK_PREFIX = "canalver:"


def _keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📣 Unirme al canal", url=CHANNEL_URL)],
            [InlineKeyboardButton("✅ Ya me uní", callback_data=f"{_CALLBACK_PREFIX}{group_id}")],
        ]
    )


async def _find_group_owner(bot, chat_id: int) -> Optional[ChatMember]:
    try:
        admins = await bot.get_chat_administrators(chat_id)
    except TelegramError as exc:
        logger.warning("No se pudo obtener los administradores del grupo %s: %s", chat_id, exc)
        return None
    for admin in admins:
        if admin.status == ChatMemberStatus.OWNER:
            return admin
    return None


async def _is_member_of_channel(bot, user_id: int) -> Optional[bool]:
    """True/False si se pudo verificar, None si hubo un error (el bot no
    es miembro del canal, el usuario nunca le habló al canal, etc.)."""
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
    except TelegramError as exc:
        logger.warning("No se pudo verificar la membresía de %s en %s: %s", user_id, CHANNEL_USERNAME, exc)
        return None
    return member.status in _JOINED_STATUSES


def _owner_mention(owner: User) -> str:
    if owner.username:
        return f"@{owner.username}"
    return f'<a href="tg://user?id={owner.id}">{html.escape(owner.full_name)}</a>'


def _notice_text(owner) -> str:
    return (
        f"📢 {_owner_mention(owner)}, dueño de este grupo:\n\n"
        "Para seguir usando este bot necesitas unirte a nuestro canal de anuncios.\n"
        "Únete tocando el botón de abajo y luego confirma con «✅ Ya me uní» para reactivar el bot."
    )


# --------------------------------------------------------------------- #
# /canal (solo propietario, solo en privado)
# --------------------------------------------------------------------- #
async def canal_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat

    if not is_owner(user.id):
        return  # comando oculto: no delatamos que existe

    if chat.type != ChatType.PRIVATE:
        await message.reply_text("Este comando se usa por privado conmigo, no dentro de un grupo.")
        return

    db: Database = context.application.bot_data["db"]
    groups = await db.get_known_groups()
    if not groups:
        await message.reply_text("No tengo registrado ningún grupo todavía.")
        return

    locked_count = 0
    already_ok_count = 0
    error_count = 0

    for group_id, title in groups:
        owner_member = await _find_group_owner(context.bot, group_id)
        if owner_member is None:
            error_count += 1
            continue

        owner = owner_member.user
        joined = await _is_member_of_channel(context.bot, owner.id)
        if joined is None:
            error_count += 1
            continue

        if joined:
            already_ok_count += 1
            await db.unlock_group_channel(group_id)
            continue

        try:
            sent = await context.bot.send_message(
                group_id,
                _notice_text(owner),
                parse_mode="HTML",
                reply_markup=_keyboard(group_id),
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.warning("No se pudo enviar el aviso del canal al grupo %s: %s", group_id, exc)
            error_count += 1
            continue

        await db.lock_group_channel(group_id, title, owner.id, sent.message_id)
        locked_count += 1

    await message.reply_text(
        "📋 Resultado de /canal:\n"
        f"🔒 {locked_count} grupo(s) bloqueados y notificados.\n"
        f"✅ {already_ok_count} grupo(s) ya cumplían (el dueño ya estaba en el canal).\n"
        f"⚠️ {error_count} grupo(s) con error (no se pudo verificar; revisa que sea admin ahí "
        "y que el bot sea miembro del canal)."
    )


# --------------------------------------------------------------------- #
# Botón "✅ Ya me uní"
# --------------------------------------------------------------------- #
@safe_callback
async def canal_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    try:
        group_id = int(data[len(_CALLBACK_PREFIX):])
    except ValueError:
        await query.answer()
        return

    db: Database = context.application.bot_data["db"]
    locked, stored_owner_id, _notice_msg_id = await db.get_channel_lock(group_id)
    user = query.from_user

    if not locked:
        await query.answer("✅ Este grupo ya está activo.", show_alert=True)
        return

    if stored_owner_id is not None and user.id != stored_owner_id and not is_owner(user.id):
        await query.answer("🔒 Solo el dueño del grupo puede confirmar esto.", show_alert=True)
        return

    target_id = stored_owner_id if stored_owner_id is not None else user.id
    joined = await _is_member_of_channel(context.bot, target_id)
    if not joined:
        await query.answer(
            "Todavía no te veo en el canal. Únete y volvé a tocar el botón.", show_alert=True
        )
        return

    await db.unlock_group_channel(group_id)
    try:
        await query.edit_message_text(
            "✅ ¡Gracias! El bot fue reactivado en este grupo.",
            reply_markup=InlineKeyboardMarkup([]),
        )
    except TelegramError:
        pass
    await query.answer("✅ Bot reactivado.")


# --------------------------------------------------------------------- #
# Bloqueo: se registra en main.py con MÁXIMA prioridad, antes que
# cualquier otro handler, para que un grupo bloqueado quede "mudo" hasta
# que su dueño confirme que se unió al canal.
# --------------------------------------------------------------------- #
async def channel_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or chat.type == ChatType.PRIVATE:
        return
    if user is not None and is_owner(user.id):
        return  # el propietario del bot nunca queda bloqueado

    db: Database = context.application.bot_data["db"]
    if await db.is_channel_locked(chat.id):
        raise ApplicationHandlerStop
