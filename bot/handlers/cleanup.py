"""
handlers/cleanup.py
Eliminación automática de mensajes:

- Mensaje de servicio de Telegram cuando un usuario entra al grupo
  ("X se unió al grupo").
- Mensaje de servicio cuando un usuario sale del grupo.
- Mensaje de servicio cuando se inicia una llamada / videollamada de grupo.
- Mensajes que invocan comandos con "/" (para mantener el chat limpio;
  el comando se sigue ejecutando normalmente, solo se borra el mensaje
  después de procesarlo).

Estos comportamientos son independientes del sistema de Bienvenida /
Despedida: uno controla si se PUBLICA un aviso personalizado, y este
controla si se BORRA el mensaje nativo de Telegram. Se pueden combinar
libremente (ej. mostrar bienvenida personalizada y a la vez esconder el
aviso nativo de "X se unió al grupo").

Se configuran desde el menú ⚙️ → 🧹 Auto-eliminar.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database
from utils.callbacks import safe_callback
from utils.permissions import is_chat_admin

logger = logging.getLogger(__name__)


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


async def _try_delete(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id, message_id)
    except TelegramError:
        pass  # Puede que ya no exista o falten permisos; lo ignoramos.


# --------------------------------------------------------------------- #
# Handlers de eventos
# --------------------------------------------------------------------- #
async def on_join_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    db = _get_db(context)
    settings = await db.get_group_settings(chat.id)
    if settings.delete_join:
        await _try_delete(context, chat.id, update.effective_message.message_id)


async def on_leave_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    db = _get_db(context)
    settings = await db.get_group_settings(chat.id)
    if settings.delete_leave:
        await _try_delete(context, chat.id, update.effective_message.message_id)


async def on_call_started_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    db = _get_db(context)
    settings = await db.get_group_settings(chat.id)
    if settings.delete_call:
        await _try_delete(context, chat.id, update.effective_message.message_id)


async def on_command_cleanup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Se ejecuta DESPUÉS de que el comando ya fue procesado por su handler
    correspondiente (registrado en un grupo posterior), y borra el mensaje
    que invocó el comando si la opción está activada."""
    chat = update.effective_chat
    message = update.effective_message
    if chat is None or message is None or chat.type not in ("group", "supergroup"):
        return
    db = _get_db(context)
    settings = await db.get_group_settings(chat.id)
    if settings.delete_commands:
        await _try_delete(context, chat.id, message.message_id)


# --------------------------------------------------------------------- #
# Menú: sección "Auto-eliminar" (callback pattern "^c:")
# --------------------------------------------------------------------- #
def _onoff(value: bool) -> str:
    return "🟢 Sí" if value else "🔴 No"


async def _cleanup_text(settings) -> str:
    return (
        "🧹 *Auto\\-eliminar*\n\n"
        "Controla qué mensajes borra el bot automáticamente para mantener "
        "el grupo limpio\\.\n\n"
        f"👋 Aviso nativo al entrar: {_onoff(settings.delete_join)}\n"
        f"🚪 Aviso nativo al salir: {_onoff(settings.delete_leave)}\n"
        f"📞 Aviso de inicio de llamada: {_onoff(settings.delete_call)}\n"
        f"⌨️ Mensajes con comandos /: {_onoff(settings.delete_commands)}"
    )


def _build_cleanup_menu(group_id: int, settings) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"👋 Entrar: {_onoff(settings.delete_join)}", callback_data=f"c:join:{group_id}")],
        [InlineKeyboardButton(f"🚪 Salir: {_onoff(settings.delete_leave)}", callback_data=f"c:leave:{group_id}")],
        [InlineKeyboardButton(f"📞 Llamada: {_onoff(settings.delete_call)}", callback_data=f"c:call:{group_id}")],
        [InlineKeyboardButton(f"⌨️ Comandos /: {_onoff(settings.delete_commands)}", callback_data=f"c:cmds:{group_id}")],
        [InlineKeyboardButton("🔙 Volver", callback_data=f"m:main:{group_id}")],
    ]
    return InlineKeyboardMarkup(rows)


_TOGGLE_COLUMNS = {
    "join": "delete_join",
    "leave": "delete_leave",
    "call": "delete_call",
    "cmds": "delete_commands",
}


@safe_callback
async def cleanup_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 3 or not parts[2].lstrip("-").isdigit():
        await query.answer()
        return

    action = parts[1]
    group_id = int(parts[2])
    user = update.effective_user
    db = _get_db(context)

    if not await is_chat_admin(context.bot, group_id, user.id):
        await query.answer("No tienes permisos de administrador en ese grupo.", show_alert=True)
        return

    if action == "menu":
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _cleanup_text(settings), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_cleanup_menu(group_id, settings),
        )
        await query.answer()
        return

    if action in _TOGGLE_COLUMNS:
        column = _TOGGLE_COLUMNS[action]
        settings = await db.get_group_settings(group_id)
        current = getattr(settings, column)
        await db.set_group_setting(group_id, column, 0 if current else 1)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _cleanup_text(settings), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_cleanup_menu(group_id, settings),
        )
        await query.answer()
        return

    await query.answer()
