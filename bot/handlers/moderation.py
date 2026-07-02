"""
handlers/moderation.py
Comandos de moderación: /ban /kick /mute /unmute /unban
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from telegram import ChatPermissions, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database
from utils.formatting import error, escape_md, mention, success
from utils.parsing import resolve_target
from utils.permissions import can_moderate, check_bot_rights, check_executor_is_admin
from utils.time_parser import parse_duration

logger = logging.getLogger(__name__)


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


async def _reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def _guard_group(update: Update) -> bool:
    if update.effective_chat is None or update.effective_chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(
            error("Este comando solo funciona en grupos o supergrupos.")
        )
        return False
    return True


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    perm = await can_moderate(context.bot, chat.id, executor.id, resolved.user_id)
    if not perm.allowed:
        await _reply(update, error(perm.reason))
        return

    reason = " ".join(resolved.remaining_args).strip() or "No especificado"

    try:
        await context.bot.ban_chat_member(chat.id, resolved.user_id)
    except TelegramError as exc:
        await _reply(update, error(f"No pude banear al usuario: {escape_md(str(exc))}"))
        return

    await db.add_log("ban", executor.id, executor.first_name, resolved.user_id,
                      resolved.display_name, chat.id, chat.title, reason)

    text = (
        f"🔨 *Usuario baneado*\n"
        f"👤 Usuario: {mention(resolved.user_id, resolved.display_name)}\n"
        f"🛡 Administrador: {mention(executor.id, executor.first_name)}\n"
        f"📝 Motivo: {escape_md(reason)}"
    )
    await _reply(update, text)


async def kick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    perm = await can_moderate(context.bot, chat.id, executor.id, resolved.user_id)
    if not perm.allowed:
        await _reply(update, error(perm.reason))
        return

    reason = " ".join(resolved.remaining_args).strip() or "No especificado"

    try:
        await context.bot.ban_chat_member(chat.id, resolved.user_id)
        await context.bot.unban_chat_member(chat.id, resolved.user_id, only_if_banned=True)
    except TelegramError as exc:
        await _reply(update, error(f"No pude expulsar al usuario: {escape_md(str(exc))}"))
        return

    await db.add_log("kick", executor.id, executor.first_name, resolved.user_id,
                      resolved.display_name, chat.id, chat.title, reason)

    text = (
        f"👢 *Usuario expulsado*\n"
        f"👤 Usuario: {mention(resolved.user_id, resolved.display_name)}\n"
        f"🛡 Administrador: {mention(executor.id, executor.first_name)}\n"
        f"📝 Motivo: {escape_md(reason)}"
    )
    await _reply(update, text)


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    perm = await can_moderate(context.bot, chat.id, executor.id, resolved.user_id)
    if not perm.allowed:
        await _reply(update, error(perm.reason))
        return

    duration = None
    remaining = resolved.remaining_args
    if remaining:
        duration = parse_duration(remaining[0])
        if duration is not None:
            remaining = remaining[1:]

    reason = " ".join(remaining).strip() or "No especificado"
    until_date = datetime.now(timezone.utc) + duration if duration else None

    try:
        await context.bot.restrict_chat_member(
            chat.id,
            resolved.user_id,
            permissions=ChatPermissions(can_send_messages=False, can_send_other_messages=False,
                                         can_send_polls=False, can_add_web_page_previews=False),
            until_date=until_date,
        )
    except TelegramError as exc:
        await _reply(update, error(f"No pude silenciar al usuario: {escape_md(str(exc))}"))
        return

    await db.add_log("mute", executor.id, executor.first_name, resolved.user_id,
                      resolved.display_name, chat.id, chat.title, reason)

    text = (
        f"🔇 *Usuario silenciado*\n"
        f"👤 Usuario: {mention(resolved.user_id, resolved.display_name)}\n"
        f"🛡 Administrador: {mention(executor.id, executor.first_name)}\n"
        f"⏱ Duración: {'Permanente' if not duration else escape_md(str(duration))}\n"
        f"📝 Motivo: {escape_md(reason)}"
    )
    await _reply(update, text)


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group(update):
        return
    db = _get_db(context)
    chat = update.effective_chat
    executor = update.effective_user

    bot_rights = await check_bot_rights(context.bot, chat.id)
    if not bot_rights.allowed:
        await _reply(update, error(bot_rights.reason))
        return

    executor_check = await check_executor_is_admin(context.bot, chat.id, executor.id)
    if not executor_check.allowed:
        await _reply(update, error(executor_check.reason))
        return

    resolved = await resolve_target(update, db, context.args)
    if isinstance(resolved, str):
        await _reply(update, error(resolved))
        return

    try:
        chat_full = await context.bot.get_chat(chat.id)
        default_perms = chat_full.permissions or ChatPermissions(
            can_send_messages=True, can_send_other_messages=True,
            can_send_polls=True, can_add_web_page_previews=True,
        )
        await context.bot.restrict_chat_member(chat.id, resolved.user_id, permissions=default_perms)
    except TelegramError as exc:
        await _reply(update, error(f"No pude reactivar al usuario: {escape_md(str(exc))}"))
        return

    await db.add_log("unmute", executor.id, executor.first_name, resolved.user_id,
                      resolved.display_name, chat.id, chat.title, None)

    text = (
        f"🔊 *Usuario reactivado*\n"
        f"👤 Usuario: {mention(resolved.user_id, resolved.display_name)}\n"
        f"🛡 Administrador: {mention(executor.id, executor.first_name)}"
    )
    await _reply(update, text)


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group(update):
        return
    db = _get_db(context)
    chat = update.effective_chat
    executor = update.effective_user

    bot_rights = await check_bot_rights(context.bot, chat.id)
    if not bot_rights.allowed:
        await _reply(update, error(bot_rights.reason))
        return

    executor_check = await check_executor_is_admin(context.bot, chat.id, executor.id)
    if not executor_check.allowed:
        await _reply(update, error(executor_check.reason))
        return

    resolved = await resolve_target(update, db, context.args)
    if isinstance(resolved, str):
        await _reply(update, error(resolved))
        return

    try:
        await context.bot.unban_chat_member(chat.id, resolved.user_id, only_if_banned=True)
    except TelegramError as exc:
        await _reply(update, error(f"No pude desbanear al usuario: {escape_md(str(exc))}"))
        return

    await db.add_log("unban", executor.id, executor.first_name, resolved.user_id,
                      resolved.display_name, chat.id, chat.title, None)

    text = (
        f"♻️ *Usuario desbaneado*\n"
        f"👤 Usuario: {mention(resolved.user_id, resolved.display_name)}\n"
        f"🛡 Administrador: {mention(executor.id, executor.first_name)}"
    )
    await _reply(update, text)
