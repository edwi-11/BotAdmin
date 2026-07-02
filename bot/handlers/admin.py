"""
handlers/admin.py
Comandos: /admin /unadmin /admins /staff
"""
from __future__ import annotations

import logging

from telegram import ChatMemberAdministrator, ChatPermissions, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database
from utils.formatting import error, escape_md, mention, success
from utils.parsing import resolve_target
from utils.permissions import can_grant_admin, check_bot_rights, is_owner

logger = logging.getLogger(__name__)


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


async def _reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def _guard_group(update: Update) -> bool:
    if update.effective_chat is None or update.effective_chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(error("Este comando solo funciona en grupos."))
        return False
    return True


ADMIN_PERMISSIONS = dict(
    can_manage_chat=True,
    can_delete_messages=True,
    can_manage_video_chats=True,
    can_restrict_members=True,
    can_promote_members=False,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=True,
)


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group(update):
        return
    db = _get_db(context)
    chat = update.effective_chat
    executor = update.effective_user

    bot_rights = await check_bot_rights(context.bot, chat.id)
    if not bot_rights.allowed:
        await _reply(update, error(bot_rights.reason))
        return

    resolved = await resolve_target(update, db, context.args)
    if isinstance(resolved, str):
        await _reply(update, error(resolved))
        return

    perm = await can_grant_admin(context.bot, chat.id, executor.id, resolved.user_id)
    if not perm.allowed:
        await _reply(update, error(perm.reason))
        return

    try:
        await context.bot.promote_chat_member(chat.id, resolved.user_id, **ADMIN_PERMISSIONS)
    except TelegramError as exc:
        await _reply(update, error(f"No pude promover al usuario: {escape_md(str(exc))}"))
        return

    await db.add_bot_admin(chat.id, resolved.user_id, executor.id)
    await db.add_log("admin", executor.id, executor.first_name, resolved.user_id,
                      resolved.display_name, chat.id, chat.title, None)

    text = (
        f"⭐ *Nuevo administrador*\n"
        f"👤 Usuario: {mention(resolved.user_id, resolved.display_name)}\n"
        f"🛡 Otorgado por: {mention(executor.id, executor.first_name)}"
    )
    await _reply(update, text)


async def unadmin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group(update):
        return
    db = _get_db(context)
    chat = update.effective_chat
    executor = update.effective_user

    bot_rights = await check_bot_rights(context.bot, chat.id)
    if not bot_rights.allowed:
        await _reply(update, error(bot_rights.reason))
        return

    resolved = await resolve_target(update, db, context.args)
    if isinstance(resolved, str):
        await _reply(update, error(resolved))
        return

    perm = await can_grant_admin(context.bot, chat.id, executor.id, resolved.user_id)
    if not perm.allowed:
        await _reply(update, error(perm.reason))
        return

    try:
        await context.bot.promote_chat_member(
            chat.id, resolved.user_id,
            can_manage_chat=False, can_delete_messages=False, can_manage_video_chats=False,
            can_restrict_members=False, can_promote_members=False, can_change_info=False,
            can_invite_users=False, can_pin_messages=False,
        )
    except TelegramError as exc:
        await _reply(update, error(f"No pude revocar al usuario: {escape_md(str(exc))}"))
        return

    await db.remove_bot_admin(chat.id, resolved.user_id)
    await db.add_log("unadmin", executor.id, executor.first_name, resolved.user_id,
                      resolved.display_name, chat.id, chat.title, None)

    text = (
        f"🚫 *Administración revocada*\n"
        f"👤 Usuario: {mention(resolved.user_id, resolved.display_name)}\n"
        f"🛡 Revocado por: {mention(executor.id, executor.first_name)}"
    )
    await _reply(update, text)


async def admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group(update):
        return
    chat = update.effective_chat
    try:
        members = await context.bot.get_chat_administrators(chat.id)
    except TelegramError as exc:
        await _reply(update, error(f"No pude obtener la lista de administradores: {escape_md(str(exc))}"))
        return

    lines = []
    for m in members:
        role = "👑 Propietario" if m.status == ChatMemberStatus.OWNER else "🛡 Administrador"
        lines.append(f"{role}: {mention(m.user.id, m.user.first_name)}")

    text = "*📋 Administradores del grupo*\n" + "\n".join(lines)
    await _reply(update, text)


async def staff_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group(update):
        return
    chat = update.effective_chat
    try:
        members = await context.bot.get_chat_administrators(chat.id)
    except TelegramError as exc:
        await _reply(update, error(f"No pude obtener el staff: {escape_md(str(exc))}"))
        return

    owner_lines = []
    admin_lines = []
    seen_ids = set()

    for m in members:
        seen_ids.add(m.user.id)
        if m.status == ChatMemberStatus.OWNER or is_owner(m.user.id):
            owner_lines.append(f"👑 {mention(m.user.id, m.user.first_name)}")
        else:
            admin_lines.append(f"🛡 {mention(m.user.id, m.user.first_name)}")

    lines = ["*👥 Staff del grupo*", "", "*Propietario:*"]
    lines.extend(owner_lines or ["No especificado"])
    lines.append("")
    lines.append("*Administradores:*")
    lines.extend(admin_lines or ["Ninguno"])

    await _reply(update, "\n".join(lines))
