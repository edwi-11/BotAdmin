"""
handlers/greetings.py
Sistema de Bienvenida / Despedida / Reglamento.

Comandos (solo administradores, excepto /rules que es público):
    /setwelcome <texto>   - define el mensaje de bienvenida
    /welcome [on|off]      - activa/desactiva o muestra el estado
    /resetwelcome           - vuelve al mensaje de bienvenida por defecto
    /setgoodbye <texto>      - define el mensaje de despedida
    /goodbye [on|off]         - activa/desactiva o muestra el estado
    /resetgoodbye              - vuelve al mensaje de despedida por defecto
    /setrules <texto>           - define el reglamento del grupo
    /rules                       - muestra el reglamento (cualquier usuario)
    /resetrules                   - borra el reglamento configurado

Placeholders disponibles en las plantillas:
    {name}      Nombre del usuario
    {mention}   Mención clickeable del usuario
    {username}  @usuario (o "sin usuario" si no tiene)
    {id}        ID numérico del usuario
    {group}     Nombre del grupo

Pensado para comunidades grandes: al activar "clean_welcome" (activado por
defecto), el bot borra el mensaje de bienvenida anterior antes de publicar
uno nuevo, evitando que el chat se llene de avisos cuando entran muchos
usuarios seguidos.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import DEFAULT_GOODBYE_TEXT, DEFAULT_RULES_TEXT, DEFAULT_WELCOME_TEXT, Database
from utils.formatting import error, render_template, success
from utils.permissions import check_executor_is_admin

logger = logging.getLogger(__name__)


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


async def _guard_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
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


def _extract_text(message, context) -> str | None:
    """Obtiene el texto a guardar: desde args, o del texto de un mensaje citado."""
    if context.args:
        return " ".join(context.args).strip()
    if message.reply_to_message and (message.reply_to_message.text or message.reply_to_message.caption):
        return (message.reply_to_message.text or message.reply_to_message.caption).strip()
    return None


# --------------------------------------------------------------------- #
# /setwelcome /welcome /resetwelcome
# --------------------------------------------------------------------- #
async def setwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    message = update.effective_message
    db = _get_db(context)

    text = _extract_text(message, context)
    if not text:
        await message.reply_text(
            error(
                "Debes indicar el texto de bienvenida. Ejemplo:\n"
                "/setwelcome ¡Hola {mention}, bienvenido a {group}!"
            )
        )
        return

    await db.set_group_setting(update.effective_chat.id, "welcome_text", text)
    preview = render_template(
        text, user_id=update.effective_user.id, first_name=update.effective_user.first_name,
        username=update.effective_user.username, group_title=update.effective_chat.title,
    )
    await message.reply_text(success("Mensaje de bienvenida actualizado."))
    await message.reply_text(f"👀 Vista previa:\n\n{preview}", parse_mode=ParseMode.HTML)


async def welcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    message = update.effective_message
    db = _get_db(context)
    chat_id = update.effective_chat.id

    if not context.args:
        settings = await db.get_group_settings(chat_id)
        estado = "activada ✅" if settings.welcome_enabled else "desactivada ❌"
        await message.reply_text(f"La bienvenida está actualmente {estado}\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    arg = context.args[0].lower()
    if arg not in ("on", "off"):
        await message.reply_text(error("Uso: /welcome on | /welcome off"))
        return

    await db.set_group_setting(chat_id, "welcome_enabled", 1 if arg == "on" else 0)
    await message.reply_text(success(f"Bienvenida {'activada' if arg == 'on' else 'desactivada'}."))


async def resetwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    db = _get_db(context)
    await db.reset_group_setting(update.effective_chat.id, "welcome_text")
    await update.effective_message.reply_text(success("Mensaje de bienvenida restablecido al valor por defecto."))


# --------------------------------------------------------------------- #
# /setgoodbye /goodbye /resetgoodbye
# --------------------------------------------------------------------- #
async def setgoodbye_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    message = update.effective_message
    db = _get_db(context)

    text = _extract_text(message, context)
    if not text:
        await message.reply_text(
            error(
                "Debes indicar el texto de despedida. Ejemplo:\n"
                "/setgoodbye {name} ha salido de {group}."
            )
        )
        return

    await db.set_group_setting(update.effective_chat.id, "goodbye_text", text)
    preview = render_template(
        text, user_id=update.effective_user.id, first_name=update.effective_user.first_name,
        username=update.effective_user.username, group_title=update.effective_chat.title,
    )
    await message.reply_text(success("Mensaje de despedida actualizado."))
    await message.reply_text(f"👀 Vista previa:\n\n{preview}", parse_mode=ParseMode.HTML)


async def goodbye_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    message = update.effective_message
    db = _get_db(context)
    chat_id = update.effective_chat.id

    if not context.args:
        settings = await db.get_group_settings(chat_id)
        estado = "activada ✅" if settings.goodbye_enabled else "desactivada ❌"
        await message.reply_text(f"La despedida está actualmente {estado}\\.", parse_mode=ParseMode.MARKDOWN_V2)
        return

    arg = context.args[0].lower()
    if arg not in ("on", "off"):
        await message.reply_text(error("Uso: /goodbye on | /goodbye off"))
        return

    await db.set_group_setting(chat_id, "goodbye_enabled", 1 if arg == "on" else 0)
    await message.reply_text(success(f"Despedida {'activada' if arg == 'on' else 'desactivada'}."))


async def resetgoodbye_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    db = _get_db(context)
    await db.reset_group_setting(update.effective_chat.id, "goodbye_text")
    await update.effective_message.reply_text(success("Mensaje de despedida restablecido al valor por defecto."))


# --------------------------------------------------------------------- #
# /setrules /rules /resetrules
# --------------------------------------------------------------------- #
async def setrules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    message = update.effective_message
    db = _get_db(context)

    text = _extract_text(message, context)
    if not text:
        await message.reply_text(
            error("Debes indicar el texto del reglamento. Ejemplo:\n/setrules 1. Respeta a los demás...")
        )
        return

    await db.set_group_setting(update.effective_chat.id, "rules_text", text)
    await message.reply_text(success("Reglamento actualizado. Los miembros pueden verlo con /rules."))


async def rules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    db = _get_db(context)

    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return

    settings = await db.get_group_settings(chat.id)
    text = render_template(
        settings.rules_text, user_id=update.effective_user.id,
        first_name=update.effective_user.first_name, username=update.effective_user.username,
        group_title=chat.title,
    )
    await message.reply_text(f"📜 <b>Reglamento de {chat.title}</b>\n\n{text}", parse_mode=ParseMode.HTML)


async def resetrules_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    db = _get_db(context)
    await db.reset_group_setting(update.effective_chat.id, "rules_text")
    await update.effective_message.reply_text(success("Reglamento restablecido (vacío)."))


# --------------------------------------------------------------------- #
# /welcomeclean - controla el borrado automático del aviso anterior
# --------------------------------------------------------------------- #
async def welcomeclean_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    message = update.effective_message
    db = _get_db(context)
    chat_id = update.effective_chat.id

    if not context.args:
        settings = await db.get_group_settings(chat_id)
        estado = "activada ✅" if settings.clean_welcome else "desactivada ❌"
        await message.reply_text(
            f"La limpieza automática de bienvenidas está {estado}\\.\n"
            f"Cuando está activada, el bot borra el aviso de bienvenida anterior "
            f"antes de publicar uno nuevo\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    arg = context.args[0].lower()
    if arg not in ("on", "off"):
        await message.reply_text(error("Uso: /welcomeclean on | /welcomeclean off"))
        return

    await db.set_group_setting(chat_id, "clean_welcome", 1 if arg == "on" else 0)
    await message.reply_text(success(f"Limpieza automática {'activada' if arg == 'on' else 'desactivada'}."))


# --------------------------------------------------------------------- #
# Detección automática de ingresos / salidas
# --------------------------------------------------------------------- #
async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    db = _get_db(context)

    settings = await db.get_group_settings(chat.id)
    if not settings.welcome_enabled:
        return

    # Si "clean_welcome" está activo, borramos el aviso de bienvenida anterior
    # (guardado en chat_data, aislado automáticamente por chat por PTB).
    if settings.clean_welcome:
        previous_id = context.chat_data.get("last_welcome_msg_id")
        if previous_id:
            try:
                await context.bot.delete_message(chat.id, previous_id)
            except TelegramError:
                pass  # Puede que ya no exista o no tengamos permiso; lo ignoramos.

    send_to = settings.welcome_send_to  # group | private | both
    need_group = send_to in ("group", "both")
    need_private = send_to in ("private", "both")
    dm_failed: list = []

    for new_user in message.new_chat_members:
        if new_user.is_bot and new_user.id == context.bot.id:
            continue  # El propio bot fue añadido al grupo; no es un "nuevo miembro" a saludar.

        await db.upsert_user(new_user.id, new_user.username, new_user.first_name)
        text = render_template(
            settings.welcome_text, user_id=new_user.id, first_name=new_user.first_name,
            username=new_user.username, group_title=chat.title,
        )

        if need_group:
            try:
                sent = await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML)
                if settings.clean_welcome:
                    context.chat_data["last_welcome_msg_id"] = sent.message_id
            except TelegramError as exc:
                logger.warning("No se pudo enviar mensaje de bienvenida en %s: %s", chat.id, exc)

        if need_private:
            already_welcomed = await db.was_join_request_welcomed(chat.id, new_user.id)
            if already_welcomed:
                continue  # ya le llegó por privado apenas mandó la solicitud de ingreso
            try:
                await context.bot.send_message(new_user.id, text, parse_mode=ParseMode.HTML)
                await db.set_dm_ok(new_user.id, True)
            except TelegramError as exc:
                # Motivo casi siempre: el usuario nunca abrió un chat con el
                # bot, y Telegram no deja que un bot le escriba primero a
                # nadie en esa situación (no hay forma de saltarse esto).
                logger.info("No pude mandar bienvenida privada a %s en %s: %s", new_user.id, chat.id, exc)
                await db.set_dm_ok(new_user.id, False)
                dm_failed.append(new_user)

    if need_private and not need_group and dm_failed:
        # Aviso corto en el grupo solo para los que no pudieron recibir el
        # privado, con un botón que los manda a iniciar chat con el bot.
        try:
            bot_username = (await context.bot.get_me()).username
        except TelegramError:
            bot_username = None
        for new_user in dm_failed:
            mention = f'<a href="tg://user?id={new_user.id}">{new_user.first_name}</a>'
            markup = None
            if bot_username:
                markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✉️ Iniciar chat con el bot", url=f"https://t.me/{bot_username}?start=1")]]
                )
            try:
                await context.bot.send_message(
                    chat.id,
                    f"👋 {mention}, tu bienvenida es por privado pero todavía no iniciaste "
                    "un chat conmigo — tocá el botón para recibirla.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
            except TelegramError as exc:
                logger.warning("No se pudo avisar en %s que la bienvenida privada falló: %s", chat.id, exc)


async def on_left_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    db = _get_db(context)

    settings = await db.get_group_settings(chat.id)
    if not settings.goodbye_enabled:
        return

    left_user = message.left_chat_member
    if left_user is None or (left_user.is_bot and left_user.id == context.bot.id):
        return

    text = render_template(
        settings.goodbye_text, user_id=left_user.id, first_name=left_user.first_name,
        username=left_user.username, group_title=chat.title,
    )
    try:
        await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        logger.warning("No se pudo enviar mensaje de despedida en %s: %s", chat.id, exc)
