"""
handlers/captcha.py
Captcha de edad: intercepta el PRIMER mensaje que un usuario manda en un
grupo con el captcha activado, lo borra, lo silencia, y le pide por
privado que confirme su edad en números planos (sin "/", "-" ni ningún
otro símbolo). Según la edad que responda:

- Dentro del rango permitido -> se le quita el silencio y puede escribir
  con normalidad.
- Fuera del rango permitido  -> se le informa que su edad no es aceptada
  y queda silenciado permanentemente (o expulsado, según lo configurado).

Todo se administra desde /menu -> "🔞 Captcha":
- Activar/desactivar.
- Qué hacer con quien no cumple la edad: dejarlo silenciado o expulsarlo.
- Rango de edad permitida (mínima y, opcionalmente, máxima).
- Canal de registros: el administrador debe estar en el canal y reenviar
  un mensaje de ese canal al bot por privado; así el bot obtiene el ID
  del canal y puede publicar ahí cada acción reciente (mensaje detectado,
  usuario, edad respondida y resultado).

Estructura de callback_data propia: "cap:<accion>:<resto>", separada de
"m:" (menu.py) para no interferir con el resto de wizards del menú.
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from database import CaptchaState, Database
from utils.callbacks import safe_callback
from utils.formatting import escape_md, error, mention, success
from utils.permissions import check_executor_is_admin, is_chat_admin

logger = logging.getLogger(__name__)

# Clave usada en context.user_data para los dos wizards de texto propios
# del captcha (rango de edad y espera del reenvío del canal de registros).
_PENDING_AGE_KEY = "captcha_pending_age_edit"
_PENDING_LOG_KEY = "captcha_pending_log_wait"


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _onoff(value: bool) -> str:
    return "🟢 Activado" if value else "🔴 Desactivado"


def _age_range_label(min_age: int, max_age: int) -> str:
    if max_age and max_age > 0:
        return f"{min_age}-{max_age} años"
    return f"{min_age}+ años"


def _action_label(action: str) -> str:
    return "🔇 Dejarlo silenciado" if action == "mute" else "🚫 Expulsarlo (ban)"


# --------------------------------------------------------------------- #
# Menú de configuración (/menu -> Captcha)
# --------------------------------------------------------------------- #
def build_captcha_menu(group_id: int, s) -> InlineKeyboardMarkup:
    log_label = s.captcha_log_title or ("Sin configurar" if not s.captcha_log_chat_id else str(s.captcha_log_chat_id))
    rows = [
        [InlineKeyboardButton(f"Estado: {_onoff(s.captcha_enabled)}", callback_data=f"cap:toggle:{group_id}")],
        [InlineKeyboardButton(f"Si no cumple la edad: {_action_label(s.captcha_action)}", callback_data=f"cap:action:{group_id}")],
        [InlineKeyboardButton(f"🔢 Edad permitida: {_age_range_label(s.captcha_min_age, s.captcha_max_age)}", callback_data=f"cap:edage:{group_id}")],
        [InlineKeyboardButton(f"📋 Canal de registros: {log_label}", callback_data=f"cap:setlog:{group_id}")],
    ]
    if s.captcha_log_chat_id:
        rows.append([InlineKeyboardButton("🗑 Quitar canal de registros", callback_data=f"cap:clearlog:{group_id}")])
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"m:main:{group_id}")])
    return InlineKeyboardMarkup(rows)


def _captcha_text(title: str, s) -> str:
    log_line = (
        f"Canal de registros: _{escape_md(s.captcha_log_title or s.captcha_log_chat_id)}_"
        if s.captcha_log_chat_id else "Canal de registros: _sin configurar_"
    )
    return (
        "🔞 *Captcha de edad*\n"
        f"Grupo: _{escape_md(title)}_\n\n"
        f"Estado: {_onoff(s.captcha_enabled)}\n"
        f"Edad permitida: *{escape_md(_age_range_label(s.captcha_min_age, s.captcha_max_age))}*\n"
        f"Si no cumple la edad: *{escape_md(_action_label(s.captcha_action))}*\n"
        f"{log_line}\n\n"
        "Cuando está activado, el primer mensaje de cada usuario nuevo en "
        "el grupo se borra y se silencia automáticamente a quien lo "
        "escribió, mientras se le pide por privado que confirme su edad\\."
    )


async def _refresh_captcha_menu(context: ContextTypes.DEFAULT_TYPE, db: Database, group_id: int, chat_id: int, message_id: int) -> None:
    title = await db.get_group_title(group_id) or str(group_id)
    s = await db.get_group_settings(group_id)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=_captcha_text(title, s), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_captcha_menu(group_id, s),
        )
    except TelegramError as exc:
        logger.warning("No pude refrescar el menú de captcha: %s", exc)


@safe_callback
async def captcha_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 3 or not parts[2].lstrip("-").isdigit():
        await query.answer()
        return

    action = parts[1]
    group_id = int(parts[2])
    db = _get_db(context)
    user = update.effective_user

    if not await is_chat_admin(context.bot, group_id, user.id):
        await query.answer("No tienes permisos de administrador en ese grupo.", show_alert=True)
        return

    title = await db.get_group_title(group_id) or str(group_id)

    if action == "toggle":
        s = await db.get_group_settings(group_id)
        await db.set_group_setting(group_id, "captcha_enabled", 0 if s.captcha_enabled else 1)
        await _refresh_captcha_menu(context, db, group_id, query.message.chat_id, query.message.message_id)
        await query.answer()
        return

    if action == "action":
        s = await db.get_group_settings(group_id)
        next_value = "ban" if s.captcha_action == "mute" else "mute"
        await db.set_group_setting(group_id, "captcha_action", next_value)
        await _refresh_captcha_menu(context, db, group_id, query.message.chat_id, query.message.message_id)
        await query.answer()
        return

    if action == "edage":
        context.user_data[_PENDING_AGE_KEY] = {
            "group_id": group_id, "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        await query.edit_message_text(
            "🔢 Enviá la edad mínima permitida \\(y opcionalmente la máxima\\)\\.\n\n"
            "Ejemplos:\n"
            "`18` \\- se permite desde los 18 años, sin límite superior\\.\n"
            "`18-99` \\- se permite entre 18 y 99 años\\.\n\n"
            "Escribí /cancelar para cancelar\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"cap:canceledit:{group_id}")]]),
        )
        await query.answer()
        return

    if action == "canceledit":
        context.user_data.pop(_PENDING_AGE_KEY, None)
        context.user_data.pop(_PENDING_LOG_KEY, None)
        await _refresh_captcha_menu(context, db, group_id, query.message.chat_id, query.message.message_id)
        await query.answer("Cancelado.")
        return

    if action == "setlog":
        context.user_data[_PENDING_LOG_KEY] = {
            "group_id": group_id, "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        await query.edit_message_text(
            "📋 *Canal de registros*\n\n"
            "1️⃣ Asegurate de ser miembro del canal y de que el bot también "
            "esté agregado ahí \\(como miembro o administrador, para poder "
            "publicar los mensajes\\)\\.\n"
            "2️⃣ Reenviame \\(por privado\\) cualquier mensaje publicado en "
            "ese canal, y quedará vinculado automáticamente\\.\n\n"
            "Escribí /cancelar para cancelar\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"cap:canceledit:{group_id}")]]),
        )
        await query.answer()
        return

    if action == "clearlog":
        await db.set_group_setting(group_id, "captcha_log_chat_id", None)
        await db.set_group_setting(group_id, "captcha_log_title", None)
        await _refresh_captcha_menu(context, db, group_id, query.message.chat_id, query.message.message_id)
        await query.answer("Canal de registros eliminado.")
        return

    if action == "menu":
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            _captcha_text(title, s), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_captcha_menu(group_id, s),
        )
        await query.answer()
        return

    await query.answer()


# --------------------------------------------------------------------- #
# Wizards de texto: rango de edad y espera del reenvío del canal
# --------------------------------------------------------------------- #
async def try_consume_captcha_age_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending: Optional[dict] = context.user_data.get(_PENDING_AGE_KEY)
    if not pending:
        return False

    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return False

    db = _get_db(context)
    group_id = pending["group_id"]

    if text.lower() in ("/cancelar", "cancelar"):
        context.user_data.pop(_PENDING_AGE_KEY, None)
        await _refresh_captcha_menu(context, db, group_id, pending["chat_id"], pending["message_id"])
        await message.reply_text("❌ Edición cancelada.")
        return True

    raw = text.replace(" ", "")
    min_age: Optional[int] = None
    max_age = 0
    try:
        if "-" in raw:
            lo, hi = raw.split("-", 1)
            min_age, max_age = int(lo), int(hi)
        else:
            min_age = int(raw)
    except ValueError:
        min_age = None

    if min_age is None or min_age < 0 or max_age < 0 or (max_age and max_age < min_age):
        await message.reply_text(
            error("Formato inválido. Escribí solo números, por ejemplo `18` o `18-99`."),
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    await db.set_group_setting(group_id, "captcha_min_age", min_age)
    await db.set_group_setting(group_id, "captcha_max_age", max_age)
    context.user_data.pop(_PENDING_AGE_KEY, None)
    await _refresh_captcha_menu(context, db, group_id, pending["chat_id"], pending["message_id"])
    await message.reply_text(success(f"Edad permitida actualizada: {_age_range_label(min_age, max_age)}."))
    return True


async def try_consume_captcha_log_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending: Optional[dict] = context.user_data.get(_PENDING_LOG_KEY)
    if not pending:
        return False

    message = update.effective_message
    db = _get_db(context)
    group_id = pending["group_id"]

    text = (message.text or "").strip().lower()
    if text in ("/cancelar", "cancelar"):
        context.user_data.pop(_PENDING_LOG_KEY, None)
        await _refresh_captcha_menu(context, db, group_id, pending["chat_id"], pending["message_id"])
        await message.reply_text("❌ Configuración cancelada.")
        return True

    origin = getattr(message, "forward_origin", None)
    channel_chat = None
    if origin is not None and getattr(origin, "type", "") == "channel":
        channel_chat = getattr(origin, "chat", None)

    if channel_chat is None:
        await message.reply_text(
            error("Eso no parece un mensaje reenviado desde un canal. Reenviame un mensaje publicado en el canal que quieras usar.")
        )
        return True

    channel_id = channel_chat.id
    channel_title = channel_chat.title or str(channel_id)

    # Verificamos que el bot pueda publicar ahí antes de dar por buena la
    # configuración, para no descubrir el problema recién cuando llegue el
    # primer registro real.
    try:
        test_msg = await context.bot.send_message(
            channel_id, f"✅ Este canal quedó vinculado como registro del captcha del grupo «{await db.get_group_title(group_id) or group_id}».",
        )
    except TelegramError as exc:
        await message.reply_text(
            error(f"No pude publicar en ese canal ({escape_md(str(exc))}). Agregá al bot al canal (como miembro o administrador) e intentá de nuevo."),
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    await db.set_group_setting(group_id, "captcha_log_chat_id", channel_id)
    await db.set_group_setting(group_id, "captcha_log_title", channel_title)
    context.user_data.pop(_PENDING_LOG_KEY, None)
    await _refresh_captcha_menu(context, db, group_id, pending["chat_id"], pending["message_id"])
    await message.reply_text(success(f"Canal de registros vinculado: {channel_title}."))
    return True


# --------------------------------------------------------------------- #
# Registro en el canal de logs
# --------------------------------------------------------------------- #
async def _send_log(
    context: ContextTypes.DEFAULT_TYPE, db: Database, group_id: int,
    group_title: Optional[str], user_id: int, user_name: str, username: Optional[str],
    action_desc: str, age: Optional[int] = None,
) -> None:
    settings = await db.get_group_settings(group_id)
    if not settings.captcha_log_chat_id:
        return
    lines = [
        "🔞 *Registro de captcha*",
        f"Grupo: {escape_md(group_title or group_id)}",
        f"Usuario: {mention(user_id, user_name)} \\(`{user_id}`\\)"
        + (f" — @{escape_md(username)}" if username else ""),
    ]
    if age is not None:
        lines.append(f"Edad respondida: {age}")
    lines.append(f"Acción: {escape_md(action_desc)}")
    try:
        await context.bot.send_message(
            settings.captcha_log_chat_id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2,
        )
    except TelegramError as exc:
        logger.warning("No pude publicar en el canal de registros del captcha (grupo %s): %s", group_id, exc)


# --------------------------------------------------------------------- #
# Detección del primer mensaje en el grupo
# --------------------------------------------------------------------- #
async def captcha_gatekeeper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None or user.is_bot:
        return

    db = _get_db(context)
    settings = await db.get_group_settings(chat.id)
    if not settings.captcha_enabled:
        return

    # Ya tiene una fila (pendiente/aprobado/rechazado): no es su primer
    # mensaje relevante para el captcha, no volvemos a intervenir.
    existing = await db.get_captcha_state(chat.id, user.id)
    if existing is not None:
        return

    # Los administradores (y el propietario) no pasan por el captcha.
    admin_check = await check_executor_is_admin(context.bot, chat.id, user.id)
    if admin_check.allowed:
        return

    await db.upsert_group(chat.id, chat.title)
    await db.start_captcha(chat.id, user.id, user.first_name, user.username, chat.title)

    try:
        await message.delete()
    except TelegramError as exc:
        logger.info("No pude borrar el primer mensaje de %s en %s: %s", user.id, chat.id, exc)

    try:
        await context.bot.restrict_chat_member(
            chat.id, user.id,
            permissions=ChatPermissions(can_send_messages=False, can_send_other_messages=False,
                                         can_send_polls=False, can_add_web_page_previews=False),
        )
    except TelegramError as exc:
        logger.warning("No pude silenciar a %s en %s para el captcha: %s", user.id, chat.id, exc)

    dm_text = (
        f"🔞 Detecté un mensaje tuyo en el grupo «{chat.title}».\n\n"
        "Antes de poder participar necesito verificar tu edad. Respondé "
        "este mensaje con tu edad en números, sin puntos, guiones ni "
        "ningún otro carácter (por ejemplo: 21)."
    )
    dm_sent = False
    try:
        await context.bot.send_message(user.id, dm_text)
        dm_sent = True
    except TelegramError as exc:
        logger.info("No pude mandar el captcha por privado a %s: %s", user.id, exc)

    if not dm_sent:
        try:
            bot_username = (await context.bot.get_me()).username
        except TelegramError:
            bot_username = None
        markup = None
        if bot_username:
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✉️ Iniciar chat con el bot", url=f"https://t.me/{bot_username}?start=1")]]
            )
        try:
            await context.bot.send_message(
                chat.id,
                f'🔞 <a href="tg://user?id={user.id}">{user.first_name}</a>, tu mensaje fue eliminado. '
                "Necesito verificar tu edad por privado antes de que puedas escribir acá — "
                "tocá el botón para iniciar el chat conmigo y te mando la pregunta.",
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
            )
        except TelegramError as exc:
            logger.warning("No pude avisar en %s que el captcha por privado falló: %s", chat.id, exc)

    await _send_log(
        context, db, chat.id, chat.title, user.id, user.first_name, user.username,
        "Primer mensaje detectado — silenciado y en espera de verificación de edad.",
    )

    # Evita que el resto de handlers de este mismo update (filtro de
    # palabras, historial de /q, "@admin", AFK, etc.) sigan procesando un
    # mensaje que ya borramos.
    raise ApplicationHandlerStop


# --------------------------------------------------------------------- #
# Respuesta de edad por privado
# --------------------------------------------------------------------- #
async def try_consume_captcha_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user
    if chat is None or chat.type != "private" or message is None or user is None:
        return False

    db = _get_db(context)
    pending: Optional[CaptchaState] = await db.get_pending_captcha_for_user(user.id)
    if pending is None:
        return False

    text = (message.text or "").strip()
    if not text:
        return False

    if not text.isdigit():
        await message.reply_text(
            error("Solo números, sin puntos, guiones ni ningún otro carácter. Ejemplo: 21")
        )
        return True

    age = int(text)
    settings = await db.get_group_settings(pending.group_id)
    min_age = settings.captcha_min_age
    max_age = settings.captcha_max_age
    allowed = age >= min_age and (max_age == 0 or age <= max_age)

    group_title = pending.group_title or str(pending.group_id)

    if allowed:
        await db.resolve_captcha(pending.group_id, user.id, "passed", age)
        try:
            chat_full = await context.bot.get_chat(pending.group_id)
            default_perms = chat_full.permissions or ChatPermissions(
                can_send_messages=True, can_send_other_messages=True,
                can_send_polls=True, can_add_web_page_previews=True,
            )
            await context.bot.restrict_chat_member(pending.group_id, user.id, permissions=default_perms)
        except TelegramError as exc:
            logger.warning("No pude quitar el silencio a %s en %s tras aprobar el captcha: %s", user.id, pending.group_id, exc)

        await message.reply_text(
            success(f"Edad verificada. Ya podés escribir con normalidad en «{group_title}».")
        )
        await _send_log(
            context, db, pending.group_id, group_title, user.id, user.first_name, user.username,
            "Edad aceptada — se le quitó el silencio.", age,
        )
        return True

    # Edad fuera del rango permitido.
    await db.resolve_captcha(pending.group_id, user.id, "rejected", age)
    action = settings.captcha_action  # mute | ban
    action_text = "Silenciado permanentemente"
    if action == "ban":
        try:
            await context.bot.ban_chat_member(pending.group_id, user.id)
            action_text = "Expulsado del grupo"
        except TelegramError as exc:
            logger.warning("No pude expulsar a %s de %s tras rechazar el captcha: %s", user.id, pending.group_id, exc)
    # Si la acción es "mute" (o si el ban falló), el usuario ya quedó
    # silenciado desde que se detectó su primer mensaje, así que no hace
    # falta volver a restringirlo.

    close_to_18 = 0 < (min_age - age) <= 2
    reply_lines = [f"Tu edad no es aceptada. Acción: {action_text.lower()}."]
    if close_to_18 and min_age >= 18:
        reply_lines.append("Si estás próximo a cumplir 18 años, por favor contactá a un administrador del grupo.")
    await message.reply_text(error(" ".join(reply_lines)))

    await _send_log(
        context, db, pending.group_id, group_title, user.id, user.first_name, user.username,
        f"Edad rechazada — {action_text.lower()}.", age,
    )
    return True
