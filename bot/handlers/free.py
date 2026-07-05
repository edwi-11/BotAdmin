"""
handlers/free.py
Comandos: /free /unfree /freelist

/free (respondiendo a un mensaje | @usuario | ID) — exime a ese usuario de
TODOS los filtros automáticos del bot en este grupo (por ahora: el filtro
de palabras prohibidas). Un usuario "freed" puede escribir cualquier
palabra prohibida, mandar links, etc. sin que el bot haga nada.

IMPORTANTE: ser administrador de Telegram YA NO exime automáticamente del
filtro. Solo el propietario del bot y quienes fueron liberados explícita-
mente con /free están exentos — incluidos los propios administradores,
que deben recibir /free igual que cualquier otro usuario si se quiere que
no les aplique el filtro.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database import Database
from utils.formatting import error, mention, success
from utils.parsing import resolve_target
from utils.permissions import check_executor_is_admin

logger = logging.getLogger(__name__)


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return False
    check = await check_executor_is_admin(context.bot, chat.id, user.id)
    if not check.allowed:
        await message.reply_text(error(check.reason))
        return False
    return True


async def free_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    db = _get_db(context)
    chat = update.effective_chat
    executor = update.effective_user

    resolved = await resolve_target(update, db, context.args)
    if isinstance(resolved, str):
        await update.effective_message.reply_text(error(resolved))
        return

    await db.add_freed_user(chat.id, resolved.user_id, executor.id)
    text = (
        f"🕊 *Usuario liberado*\n"
        f"👤 {mention(resolved.user_id, resolved.display_name)} ya no será afectado por "
        f"ningún filtro automático de este grupo (palabras prohibidas, etc.), aunque no sea "
        f"administrador.\n"
        f"🛡 Por: {mention(executor.id, executor.first_name)}"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def unfree_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard(update, context):
        return
    db = _get_db(context)
    chat = update.effective_chat
    executor = update.effective_user

    resolved = await resolve_target(update, db, context.args)
    if isinstance(resolved, str):
        await update.effective_message.reply_text(error(resolved))
        return

    removed = await db.remove_freed_user(chat.id, resolved.user_id)
    if not removed:
        await update.effective_message.reply_text(
            error(f"{resolved.display_name} no estaba liberado en este grupo.")
        )
        return

    text = (
        f"🔒 *Liberación revocada*\n"
        f"👤 {mention(resolved.user_id, resolved.display_name)} vuelve a estar sujeto a los "
        f"filtros automáticos del grupo.\n"
        f"🛡 Por: {mention(executor.id, executor.first_name)}"
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def freelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(error("Este comando solo funciona en grupos."))
        return
    db = _get_db(context)
    user_ids = await db.get_freed_users(chat.id)
    if not user_ids:
        await update.effective_message.reply_text("No hay usuarios liberados en este grupo.")
        return

    lines = ["🕊 *Usuarios liberados en este grupo*", ""]
    for uid in user_ids:
        name = await db.get_user_display_name(uid) or str(uid)
        lines.append(f"• {mention(uid, name)}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
