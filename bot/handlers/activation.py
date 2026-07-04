"""
handlers/activation.py
Sistema de "grupos activados": el bot solo funciona en los grupos que el
propietario activó explícitamente con /activar, y dentro de esos grupos
solo pueden usarlo administradores con el permiso "Cambiar info del grupo"
(o el propio propietario).

Flujo:
- Al agregar el bot a un grupo nuevo, o al intentar usar cualquier comando
  en un grupo no activado, se responde pidiendo contactar al propietario.
- El propietario activa el grupo mandando /activar DENTRO del grupo.
- Una vez activado, si alguien sin permisos intenta usar un comando, se le
  avisa que debe contactar al propietario para poder usarlo.
"""
from __future__ import annotations

import logging

from telegram import ChatMemberUpdated, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.ext import ApplicationHandlerStop, ContextTypes

from database import Database
from utils.permissions import check_executor_is_admin, is_owner

logger = logging.getLogger(__name__)

OWNER_CONTACT = "@Sky_lent"

# Comandos que SIEMPRE deben poder ejecutarse (aunque el grupo no esté
# activado), porque son justamente los que sirven para activar/desactivar.
EXEMPT_COMMANDS = {"activar", "desactivar"}

MSG_NOT_ACTIVATED = (
    "🚫 Este grupo no tiene permisos para usar este bot.\n"
    f"Por favor, contáctate con {OWNER_CONTACT} para su activación."
)

MSG_NOT_ADMIN = (
    f"🔒 Este bot le pertenece a {OWNER_CONTACT}.\n"
    "Para poder usarlo en un grupo, contáctate con él para su activación.\n"
    "Una vez activado, regresa a este chat y usa el comando /start."
)


def _extract_command(text: str) -> str:
    first_word = text.split()[0] if text.split() else ""
    return first_word[1:].split("@")[0].lower()


async def activar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("Este comando se usa dentro del grupo que quieres activar.")
        return
    if not is_owner(user.id):
        # No delatamos que el comando existe/qué hace; mismo tono que el resto.
        await update.effective_message.reply_text(MSG_NOT_ADMIN)
        return
    db: Database = context.application.bot_data["db"]
    await db.set_group_activated(chat.id, chat.title, True)
    await update.effective_message.reply_text(
        "✅ Bot activado en este grupo.\n"
        "Ya pueden usarlo los administradores con el permiso «Cambiar info del grupo»."
    )
    logger.info("Grupo %s (%s) activado por el propietario %s", chat.id, chat.title, user.id)


async def desactivar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == ChatType.PRIVATE:
        await update.effective_message.reply_text("Este comando se usa dentro del grupo que quieres desactivar.")
        return
    if not is_owner(user.id):
        await update.effective_message.reply_text(MSG_NOT_ADMIN)
        return
    db: Database = context.application.bot_data["db"]
    await db.set_group_activated(chat.id, chat.title, False)
    await update.effective_message.reply_text("🚫 Bot desactivado en este grupo.")
    logger.info("Grupo %s (%s) desactivado por el propietario %s", chat.id, chat.title, user.id)


async def on_bot_membership_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Se dispara cuando el bot es agregado (o cambia su estado) en un
    grupo. Si el grupo no está activado, avisa de inmediato."""
    result: ChatMemberUpdated = update.my_chat_member
    chat = update.effective_chat
    new_status = result.new_chat_member.status
    old_status = result.old_chat_member.status

    just_joined = old_status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and new_status in (
        ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
    )
    if not just_joined:
        return

    db: Database = context.application.bot_data["db"]
    await db.upsert_group(chat.id, chat.title)
    if not await db.is_group_activated(chat.id):
        try:
            await context.bot.send_message(chat.id, MSG_NOT_ACTIVATED)
        except Exception:  # noqa: BLE001 - no queremos tumbar el bot por esto
            logger.warning("No se pudo enviar el aviso de activación al grupo %s", chat.id)


async def group_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Corre ANTES que cualquier otro handler de comandos en grupos.
    Bloquea el comando (y detiene la propagación del update) si el grupo
    no está activado, o si quien lo ejecuta no es admin habilitado."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or not message.text or chat is None or user is None:
        return
    if chat.type == ChatType.PRIVATE:
        return

    command = _extract_command(message.text)
    if command in EXEMPT_COMMANDS:
        return  # /activar y /desactivar tienen su propia validación

    db: Database = context.application.bot_data["db"]
    activated = await db.is_group_activated(chat.id)
    if not activated:
        await message.reply_text(MSG_NOT_ACTIVATED)
        raise ApplicationHandlerStop

    if is_owner(user.id):
        return  # el propietario siempre puede usar el bot

    check = await check_executor_is_admin(context.bot, chat.id, user.id)
    if not check.allowed:
        await message.reply_text(MSG_NOT_ADMIN)
        raise ApplicationHandlerStop
