"""
handlers/utils_cmds.py
Comandos: /del /id /ping /info
"""
from __future__ import annotations

import logging
import time

from telegram import Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import settings
from database import Database
from utils.formatting import error, escape_md, mention
from utils.parsing import resolve_target
from utils.permissions import check_bot_rights, check_executor_is_admin, is_owner

logger = logging.getLogger(__name__)


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


async def _reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def del_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    executor = update.effective_user

    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return

    executor_check = await check_executor_is_admin(context.bot, chat.id, executor.id)
    if not executor_check.allowed:
        await message.reply_text(error(executor_check.reason))
        return

    bot_rights = await check_bot_rights(context.bot, chat.id)
    if not bot_rights.allowed:
        await message.reply_text(error(bot_rights.reason))
        return

    if not message.reply_to_message:
        await message.reply_text(error("Debes usar /del respondiendo al mensaje que quieres eliminar."))
        return

    reason = " ".join(context.args).strip() or "No especificado"
    target_message_id = message.reply_to_message.message_id

    try:
        await context.bot.delete_message(chat.id, target_message_id)
    except TelegramError as exc:
        await message.reply_text(error(f"No pude eliminar el mensaje: {escape_md(str(exc))}"))
        return

    try:
        await context.bot.delete_message(chat.id, message.message_id)
    except TelegramError:
        pass

    notice_text = (
        f"🗑 *Mensaje eliminado*\n"
        f"🛡 Por: {mention(executor.id, executor.first_name)}\n"
        f"📝 Motivo: {escape_md(reason)}"
    )
    notice = await context.bot.send_message(chat.id, notice_text, parse_mode=ParseMode.MARKDOWN_V2)

    async def _delete_notice_job(job_context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await job_context.bot.delete_message(chat.id, notice.message_id)
        except TelegramError:
            pass

    context.job_queue.run_once(_delete_notice_job, when=settings.del_notice_seconds)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    target_user = user
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user

    lines = [
        f"🆔 *Información de ID*",
        f"👤 Usuario: {mention(target_user.id, target_user.first_name)}",
        f"🔢 ID de usuario: `{target_user.id}`",
    ]
    if chat.type in ("group", "supergroup", "channel"):
        lines.append(f"👥 ID del grupo: `{chat.id}`")

    await message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    start = time.perf_counter()
    message = await update.effective_message.reply_text("🏓 Calculando latencia\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    elapsed_ms = (time.perf_counter() - start) * 1000
    await message.edit_text(f"🏓 *Pong\\!* Latencia: `{elapsed_ms:.0f} ms`", parse_mode=ParseMode.MARKDOWN_V2)


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    db = _get_db(context)

    resolved = await resolve_target(update, db, context.args)
    if isinstance(resolved, str):
        # Sin objetivo especificado -> mostrar info del propio usuario
        target = update.effective_user
        target_id, target_name, target_username = target.id, target.first_name, target.username
    else:
        target_id, target_name, target_username = resolved.user_id, resolved.display_name, resolved.username

    status_text = "Desconocido"
    if chat.type in ("group", "supergroup"):
        member = await context.bot.get_chat_member(chat.id, target_id)
        status_map = {
            ChatMemberStatus.OWNER: "👑 Propietario",
            ChatMemberStatus.ADMINISTRATOR: "🛡 Administrador",
            ChatMemberStatus.MEMBER: "👤 Miembro",
            ChatMemberStatus.RESTRICTED: "🔇 Restringido",
            ChatMemberStatus.LEFT: "🚪 Fuera del grupo",
            ChatMemberStatus.BANNED: "🔨 Baneado",
        }
        status_text = status_map.get(member.status, str(member.status))
        if is_owner(target_id):
            status_text = "👑 Propietario del bot"

    lines = [
        "*ℹ️ Información del usuario*",
        f"👤 Nombre: {mention(target_id, target_name)}",
        f"🔢 ID: `{target_id}`",
        f"📛 Username: {escape_md('@' + target_username) if target_username else 'No tiene'}",
        f"📌 Estado: {escape_md(status_text)}",
    ]
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
