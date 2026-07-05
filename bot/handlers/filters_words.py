"""
handlers/filters_words.py
Filtro de palabras prohibidas.

- Los administradores pueden agregar o eliminar palabras, una por una o
  varias a la vez (una por línea / separadas por un salto de párrafo).
- Se configura un castigo global para el grupo: ninguno, mute (con
  duración) o ban, y si además se debe borrar el mensaje que contenía la
  palabra prohibida.
- Los administradores y el propietario nunca son afectados por el filtro.

Comandos:
    /agregarpalabra <palabra(s)>   - agrega una o varias palabras (una por línea)
    /eliminarpalabra <palabra(s)>  - elimina una o varias palabras
    /palabras                       - lista las palabras prohibidas del grupo
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database
from utils.callbacks import safe_callback
from utils.formatting import error, escape_md, humanize_seconds, mention, success
from utils.permissions import is_chat_admin, is_owner

logger = logging.getLogger(__name__)

# (etiqueta, segundos) — 0 significa mute permanente
MUTE_DURATIONS: list[tuple[str, int]] = [
    ("10 min", 600), ("30 min", 1800), ("1 h", 3600),
    ("6 h", 21600), ("1 día", 86400), ("7 días", 604800),
    ("Permanente", 0),
]

PUNISHMENT_LABELS = {"none": "Solo borrar", "mute": "Silenciar (mute)", "ban": "Banear"}


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _split_words(raw_text: str) -> list[str]:
    """Admite una palabra por línea (o por punto y aparte)."""
    parts: list[str] = []
    for chunk in raw_text.replace("\r", "").split("\n"):
        chunk = chunk.strip(" .")
        if chunk:
            parts.append(chunk)
    return parts


async def _guard_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return False
    if not await is_chat_admin(context.bot, chat.id, user.id):
        await message.reply_text(error("No tienes permisos de administrador para usar este comando."))
        return False
    return True


# --------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------- #
async def add_word_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    message = update.effective_message
    db = _get_db(context)
    # Usamos el texto completo (no context.args) para no perder los saltos de línea
    # cuando se agregan varias palabras a la vez, una por línea.
    full_text = message.text or ""
    split = full_text.split(None, 1)
    raw = split[1].strip() if len(split) > 1 else ""

    if not raw:
        await message.reply_text(
            error(
                "Indica la(s) palabra(s) a prohibir. Una por línea si son varias.\n"
                "Ejemplo:\n/agregarpalabra palabra1\npalabra2\npalabra3"
            )
        )
        return

    words = _split_words(raw)
    added = 0
    for w in words:
        if await db.add_banned_word(update.effective_chat.id, w, update.effective_user.id):
            added += 1
    await message.reply_text(success(f"Se agregaron {added} palabra(s) prohibida(s)."))


async def remove_word_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    message = update.effective_message
    db = _get_db(context)
    full_text = message.text or ""
    split = full_text.split(None, 1)
    raw = split[1].strip() if len(split) > 1 else ""

    if not raw:
        await message.reply_text(error("Indica la(s) palabra(s) a eliminar del filtro."))
        return

    words = _split_words(raw)
    removed = 0
    for w in words:
        if await db.remove_banned_word(update.effective_chat.id, w):
            removed += 1
    await message.reply_text(success(f"Se eliminaron {removed} palabra(s) del filtro."))


async def list_words_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    db = _get_db(context)
    words = await db.get_banned_words(update.effective_chat.id)
    if not words:
        await update.effective_message.reply_text("No hay palabras prohibidas configuradas en este grupo.")
        return
    text = "🚫 *Palabras prohibidas*\n\n" + "\n".join(f"• {escape_md(w)}" for w in words)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


# --------------------------------------------------------------------- #
# Menú: sección "Palabras prohibidas" (callback pattern "^w:")
# --------------------------------------------------------------------- #
async def _words_text(db: Database, group_id: int) -> str:
    settings = await db.get_group_settings(group_id)
    words = await db.get_banned_words(group_id)
    words_preview = ", ".join(words[:15]) + (f" … (+{len(words) - 15})" if len(words) > 15 else "")
    duration = "Permanente" if settings.filter_mute_seconds == 0 else humanize_seconds(settings.filter_mute_seconds)
    return (
        "🚫 *Palabras prohibidas*\n\n"
        f"Palabras configuradas: *{len(words)}*\n"
        f"{escape_md(words_preview) if words else '_ninguna_'}\n\n"
        f"⚖️ Castigo: *{escape_md(PUNISHMENT_LABELS[settings.filter_punishment])}*\n"
        + (f"⏱ Duración del mute: *{escape_md(duration)}*\n" if settings.filter_punishment == "mute" else "")
        + f"🗑 Borrar el mensaje: *{'Sí' if settings.filter_delete else 'No'}*"
    )


def _build_words_menu(group_id: int, settings) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("➕ Agregar palabra(s)", callback_data=f"w:add:{group_id}")],
        [InlineKeyboardButton("➖ Eliminar palabra(s)", callback_data=f"w:remove:{group_id}")],
        [InlineKeyboardButton("📋 Ver lista completa", callback_data=f"w:full:{group_id}")],
        [InlineKeyboardButton(f"⚖️ Castigo: {PUNISHMENT_LABELS[settings.filter_punishment]}",
                               callback_data=f"w:punishment:{group_id}")],
    ]
    if settings.filter_punishment == "mute":
        duration = "Permanente" if settings.filter_mute_seconds == 0 else humanize_seconds(settings.filter_mute_seconds)
        rows.append([InlineKeyboardButton(f"⏱ Duración: {duration}", callback_data=f"w:duration:{group_id}")])
    rows.append([InlineKeyboardButton(
        f"🗑 Borrar mensaje: {'Sí' if settings.filter_delete else 'No'}", callback_data=f"w:togdel:{group_id}"
    )])
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"m:main:{group_id}")])
    return InlineKeyboardMarkup(rows)


@safe_callback
async def words_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            await _words_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_words_menu(group_id, settings),
        )
        await query.answer()
        return

    if action == "full":
        words = await db.get_banned_words(group_id)
        text = ("🚫 *Palabras prohibidas*\n\n" + "\n".join(f"• {escape_md(w)}" for w in words)) if words \
            else "No hay palabras prohibidas configuradas."
        back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data=f"w:menu:{group_id}")]])
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back)
        await query.answer()
        return

    if action == "add":
        context.user_data["pending_words"] = {
            "action": "add", "group_id": group_id,
            "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"w:cancelwords:{group_id}")]])
        await query.edit_message_text(
            "➕ Envía la\\(s\\) palabra\\(s\\) a prohibir\\. Si son varias, una por línea\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=cancel_kb,
        )
        await query.answer()
        return

    if action == "remove":
        context.user_data["pending_words"] = {
            "action": "remove", "group_id": group_id,
            "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"w:cancelwords:{group_id}")]])
        await query.edit_message_text(
            "➖ Envía la\\(s\\) palabra\\(s\\) a eliminar del filtro\\. Si son varias, una por línea\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=cancel_kb,
        )
        await query.answer()
        return

    if action == "cancelwords":
        context.user_data.pop("pending_words", None)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _words_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_words_menu(group_id, settings),
        )
        await query.answer("Cancelado.")
        return

    if action == "punishment":
        order = ["none", "mute", "ban"]
        settings = await db.get_group_settings(group_id)
        next_p = order[(order.index(settings.filter_punishment) + 1) % len(order)]
        await db.set_group_setting(group_id, "filter_punishment", next_p)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _words_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_words_menu(group_id, settings),
        )
        await query.answer()
        return

    if action == "duration":
        rows = []
        row = []
        for label, seconds in MUTE_DURATIONS:
            row.append(InlineKeyboardButton(label, callback_data=f"w:setdur:{group_id}:{seconds}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"w:menu:{group_id}")])
        await query.edit_message_text(
            "⏱ Elige la duración del mute:", reply_markup=InlineKeyboardMarkup(rows)
        )
        await query.answer()
        return

    if action == "setdur":
        seconds = int(parts[3])
        await db.set_group_setting(group_id, "filter_mute_seconds", seconds)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _words_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_words_menu(group_id, settings),
        )
        await query.answer("Duración actualizada.")
        return

    if action == "togdel":
        settings = await db.get_group_settings(group_id)
        await db.set_group_setting(group_id, "filter_delete", 0 if settings.filter_delete else 1)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _words_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_words_menu(group_id, settings),
        )
        await query.answer()
        return

    await query.answer()


async def try_consume_pending_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending: Optional[dict] = context.user_data.get("pending_words")
    if not pending:
        return False

    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return False

    db = _get_db(context)
    group_id = pending["group_id"]
    action = pending["action"]
    chat_id = pending.get("chat_id")
    message_id = pending.get("message_id")

    if text.lower() in ("/cancelar", "cancelar"):
        context.user_data.pop("pending_words", None)
        await message.reply_text("❌ Cancelado.")
        return True

    words = _split_words(text)
    if action == "add":
        added = 0
        for w in words:
            if await db.add_banned_word(group_id, w, update.effective_user.id):
                added += 1
        summary = success(f"Se agregaron {added} palabra(s) prohibida(s).")
    else:
        removed = 0
        for w in words:
            if await db.remove_banned_word(group_id, w):
                removed += 1
        summary = success(f"Se eliminaron {removed} palabra(s) del filtro.")

    context.user_data.pop("pending_words", None)
    settings = await db.get_group_settings(group_id)
    menu_text = await _words_text(db, group_id)
    menu_markup = _build_words_menu(group_id, settings)

    # Editamos el mismo mensaje del menú (si seguimos pudiendo) para que todo
    # quede dentro de una única pantalla de configuración, sin ir dejando
    # mensajes sueltos uno tras otro.
    edited = False
    if chat_id is not None and message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=menu_text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=menu_markup,
            )
            edited = True
        except TelegramError as exc:
            logger.warning("No pude editar el menú de palabras tras la edición: %s", exc)

    await message.reply_text(summary)
    if not edited:
        await message.reply_text(menu_text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=menu_markup)
    return True


# --------------------------------------------------------------------- #
# Filtro en vivo: revisa cada mensaje de texto/caption del grupo
# --------------------------------------------------------------------- #
async def check_banned_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if message is None or chat is None or user is None or user.is_bot:
        return
    if chat.type not in ("group", "supergroup"):
        return

    text = (message.text or message.caption or "")
    if not text:
        return

    db = _get_db(context)
    words = await db.get_banned_words(chat.id)
    if not words:
        return

    lowered = text.lower()
    matched = any(w in lowered for w in words)
    if not matched:
        return

    # El propietario y los administradores nunca son afectados por el filtro
    # (como siempre). Además, cualquier usuario liberado con /free tampoco,
    # aunque no sea administrador.
    if is_owner(user.id) or await is_chat_admin(context.bot, chat.id, user.id) or await db.is_user_freed(chat.id, user.id):
        return

    settings = await db.get_group_settings(chat.id)

    if settings.filter_delete:
        try:
            await context.bot.delete_message(chat.id, message.message_id)
        except TelegramError:
            pass

    action_taken = "Se eliminó el mensaje" if settings.filter_delete else "Se detectó el mensaje"

    try:
        if settings.filter_punishment == "mute":
            until_date = None
            if settings.filter_mute_seconds > 0:
                until_date = datetime.now(timezone.utc) + timedelta(seconds=settings.filter_mute_seconds)
            await context.bot.restrict_chat_member(
                chat.id, user.id,
                permissions=ChatPermissions(can_send_messages=False, can_send_other_messages=False,
                                             can_send_polls=False, can_add_web_page_previews=False),
                until_date=until_date,
            )
            duration_text = "permanentemente" if settings.filter_mute_seconds == 0 else \
                f"por {humanize_seconds(settings.filter_mute_seconds)}"
            notice = (
                f"🚫 *Palabra prohibida detectada*\n"
                f"{action_taken} de {mention(user.id, user.first_name)}\\.\n"
                f"🔇 Silenciado {escape_md(duration_text)}\\."
            )
        elif settings.filter_punishment == "ban":
            await context.bot.ban_chat_member(chat.id, user.id)
            notice = (
                f"🚫 *Palabra prohibida detectada*\n"
                f"{action_taken} de {mention(user.id, user.first_name)}\\.\n"
                f"🔨 Usuario baneado\\."
            )
        else:
            notice = (
                f"🚫 *Palabra prohibida detectada*\n"
                f"{action_taken} de {mention(user.id, user.first_name)}\\."
            )
    except TelegramError as exc:
        logger.warning("No se pudo aplicar el castigo del filtro en %s: %s", chat.id, exc)
        notice = (
            f"🚫 *Palabra prohibida detectada*\n"
            f"{action_taken} de {mention(user.id, user.first_name)}\\."
        )

    await db.add_log("filtro_palabras", context.bot.id, "Filtro automático", user.id,
                      user.first_name, chat.id, chat.title, "Palabra prohibida")

    try:
        await context.bot.send_message(chat.id, notice, parse_mode=ParseMode.MARKDOWN_V2)
    except TelegramError:
        pass
