"""
handlers/xmod.py
Moderación extrema de usuarios.

Revisa a cada usuario justo en el momento en que se acepta su solicitud
de ingreso (/aceptar, ver handlers/join_requests.py) buscando palabras o
emojis prohibidos configurados por los administradores en:

- Su nombre (nombre + apellido) y su @usuario.
- Su descripción/biografía de Telegram (si Telegram permite consultarla;
  depende de la configuración de privacidad de cada usuario).
- Si tiene o no una foto de perfil real (Telegram no permite a los bots
  "ver" el contenido de una imagen para saber si es o no un emoji, así
  que este chequeo es una señal aproximada: marca como sospechoso a
  quien NO tiene ninguna foto de perfil, algo típico de cuentas de
  spam/bots).

Si algo coincide, en vez de aprobar la solicitud se aplica el castigo
configurado:
- Banear: se rechaza la solicitud y se banea al usuario (no puede
  volver a pedir ingreso).
- Silenciar: se aprueba la solicitud (entra al grupo) pero queda
  silenciado de inmediato por la duración configurada (o de forma
  permanente).

Todo se configura desde el menú de botones, sección "🛡 Moderación
extrema" (callback pattern "^xm:").
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import Bot, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database, GroupSettings
from handlers.filters_words import MUTE_DURATIONS
from utils.callbacks import safe_callback
from utils.formatting import escape_md, humanize_seconds, success
from utils.permissions import is_chat_admin

logger = logging.getLogger(__name__)

PUNISHMENT_LABELS = {"ban": "Banear", "mute": "Silenciar (mute)"}


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _split_words(raw_text: str) -> list[str]:
    """Admite una palabra/emoji por línea (o por punto y aparte)."""
    parts: list[str] = []
    for chunk in raw_text.replace("\r", "").split("\n"):
        chunk = chunk.strip(" .")
        if chunk:
            parts.append(chunk)
    return parts


# --------------------------------------------------------------------- #
# Chequeo y castigo (usado desde /aceptar)
# --------------------------------------------------------------------- #
async def check_extreme_moderation(
    bot: Bot,
    db: Database,
    chat_id: int,
    user_id: int,
    first_name: Optional[str],
    username: Optional[str],
) -> Optional[str]:
    """Devuelve el motivo de la sanción si el usuario debe ser castigado
    al aceptar su solicitud, o None si pasa la revisión limpio."""
    settings = await db.get_group_settings(chat_id)
    if not settings.xmod_enabled:
        return None

    words = await db.get_xmod_words(chat_id)

    if words and (settings.xmod_check_name or settings.xmod_check_bio):
        pieces: list[str] = []
        if settings.xmod_check_name:
            if first_name:
                pieces.append(first_name)
            if username:
                pieces.append(username)
        if settings.xmod_check_bio:
            try:
                chat_full = await bot.get_chat(user_id)
                if chat_full.bio:
                    pieces.append(chat_full.bio)
            except TelegramError as exc:
                logger.info("No pude consultar la bio de %s: %s", user_id, exc)

        haystack = " ".join(pieces).lower()
        for w in words:
            if w in haystack:
                return f"Se detectó la palabra/emoji prohibido «{w}» en su nombre, usuario o descripción."

    if settings.xmod_check_photo:
        try:
            photos = await bot.get_user_profile_photos(user_id, limit=1)
            if photos.total_count == 0:
                return "El usuario no tiene una foto de perfil real."
        except TelegramError as exc:
            logger.info("No pude consultar la foto de perfil de %s: %s", user_id, exc)

    return None


async def apply_extreme_punishment(
    bot: Bot,
    db: Database,
    chat_id: int,
    group_title: Optional[str],
    user_id: int,
    first_name: Optional[str],
    reason: str,
) -> str:
    """Aplica el castigo configurado en vez de aprobar la solicitud.
    Devuelve una descripción corta de lo que se hizo (para el resumen)."""
    settings = await db.get_group_settings(chat_id)
    name = first_name or str(user_id)

    try:
        if settings.xmod_punishment == "mute":
            await bot.approve_chat_join_request(chat_id, user_id)
            until_date = None
            if settings.xmod_mute_seconds > 0:
                until_date = datetime.now(timezone.utc) + timedelta(seconds=settings.xmod_mute_seconds)
            await bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(can_send_messages=False, can_send_other_messages=False,
                                             can_send_polls=False, can_add_web_page_previews=False),
                until_date=until_date,
            )
            outcome = "silenciado al entrar"
        else:
            await bot.decline_chat_join_request(chat_id, user_id)
            await bot.ban_chat_member(chat_id, user_id)
            outcome = "rechazado y baneado"
    except TelegramError as exc:
        logger.warning("No pude aplicar el castigo de moderación extrema a %s en %s: %s", user_id, chat_id, exc)
        outcome = "no se pudo aplicar el castigo (revisa mis permisos)"

    await db.add_log(
        "moderacion_extrema", bot.id, "Moderación extrema (auto)",
        user_id, name, chat_id, group_title, reason,
    )
    return outcome


# --------------------------------------------------------------------- #
# Menú: sección "🛡 Moderación extrema" (callback pattern "^xm:")
# --------------------------------------------------------------------- #
def _yesno(value: bool) -> str:
    return "Sí" if value else "No"


async def _xmod_text(db: Database, group_id: int) -> str:
    settings = await db.get_group_settings(group_id)
    words = await db.get_xmod_words(group_id)
    words_preview = ", ".join(words[:15]) + (f" … (+{len(words) - 15})" if len(words) > 15 else "")
    duration = "Permanente" if settings.xmod_mute_seconds == 0 else humanize_seconds(settings.xmod_mute_seconds)
    return (
        "🛡 *Moderación extrema de usuarios*\n\n"
        "Revisa el nombre, el @usuario, la descripción y la foto de perfil "
        "de cada persona al momento de aceptar su solicitud de ingreso con "
        "/aceptar\\. Si algo coincide, en vez de aprobarla se aplica el castigo\\.\n\n"
        f"Estado: *{_yesno(settings.xmod_enabled)}*\n"
        f"Palabras/emojis configurados: *{len(words)}*\n"
        f"{escape_md(words_preview) if words else '_ninguno_'}\n\n"
        f"👤 Revisar nombre/usuario: *{_yesno(settings.xmod_check_name)}*\n"
        f"📝 Revisar descripción \\(bio\\): *{_yesno(settings.xmod_check_bio)}*\n"
        f"🖼 Exigir foto de perfil real: *{_yesno(settings.xmod_check_photo)}*\n\n"
        f"⚖️ Castigo: *{escape_md(PUNISHMENT_LABELS[settings.xmod_punishment])}*\n"
        + (f"⏱ Duración del mute: *{escape_md(duration)}*\n" if settings.xmod_punishment == "mute" else "")
    )


def _build_xmod_menu(group_id: int, settings: GroupSettings) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"Estado: {_yesno(settings.xmod_enabled)}", callback_data=f"xm:toggle:{group_id}")],
        [InlineKeyboardButton("➕ Agregar palabra(s)/emoji(s)", callback_data=f"xm:add:{group_id}")],
        [InlineKeyboardButton("➖ Eliminar palabra(s)/emoji(s)", callback_data=f"xm:remove:{group_id}")],
        [InlineKeyboardButton("📋 Ver lista completa", callback_data=f"xm:full:{group_id}")],
        [InlineKeyboardButton(f"👤 Revisar nombre/usuario: {_yesno(settings.xmod_check_name)}",
                               callback_data=f"xm:checkname:{group_id}")],
        [InlineKeyboardButton(f"📝 Revisar descripción: {_yesno(settings.xmod_check_bio)}",
                               callback_data=f"xm:checkbio:{group_id}")],
        [InlineKeyboardButton(f"🖼 Exigir foto real: {_yesno(settings.xmod_check_photo)}",
                               callback_data=f"xm:checkphoto:{group_id}")],
        [InlineKeyboardButton(f"⚖️ Castigo: {PUNISHMENT_LABELS[settings.xmod_punishment]}",
                               callback_data=f"xm:punishment:{group_id}")],
    ]
    if settings.xmod_punishment == "mute":
        duration = "Permanente" if settings.xmod_mute_seconds == 0 else humanize_seconds(settings.xmod_mute_seconds)
        rows.append([InlineKeyboardButton(f"⏱ Duración: {duration}", callback_data=f"xm:duration:{group_id}")])
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"m:main:{group_id}")])
    return InlineKeyboardMarkup(rows)


@safe_callback
async def xmod_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            await _xmod_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_xmod_menu(group_id, settings),
        )
        await query.answer()
        return

    if action == "toggle":
        settings = await db.get_group_settings(group_id)
        await db.set_group_setting(group_id, "xmod_enabled", 0 if settings.xmod_enabled else 1)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _xmod_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_xmod_menu(group_id, settings),
        )
        await query.answer()
        return

    if action == "full":
        words = await db.get_xmod_words(group_id)
        text = ("🛡 *Palabras/emojis prohibidos*\n\n" + "\n".join(f"• {escape_md(w)}" for w in words)) if words \
            else "No hay palabras/emojis configurados."
        back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver", callback_data=f"xm:menu:{group_id}")]])
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=back)
        await query.answer()
        return

    if action == "add":
        context.user_data["pending_xmod_words"] = {
            "action": "add", "group_id": group_id,
            "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"xm:cancelwords:{group_id}")]])
        await query.edit_message_text(
            "➕ Envía la\\(s\\) palabra\\(s\\) o emoji\\(s\\) a prohibir\\. Si son varias, una por línea\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=cancel_kb,
        )
        await query.answer()
        return

    if action == "remove":
        context.user_data["pending_xmod_words"] = {
            "action": "remove", "group_id": group_id,
            "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"xm:cancelwords:{group_id}")]])
        await query.edit_message_text(
            "➖ Envía la\\(s\\) palabra\\(s\\) o emoji\\(s\\) a eliminar\\. Si son varias, una por línea\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=cancel_kb,
        )
        await query.answer()
        return

    if action == "cancelwords":
        context.user_data.pop("pending_xmod_words", None)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _xmod_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_xmod_menu(group_id, settings),
        )
        await query.answer("Cancelado.")
        return

    if action == "checkname":
        settings = await db.get_group_settings(group_id)
        await db.set_group_setting(group_id, "xmod_check_name", 0 if settings.xmod_check_name else 1)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _xmod_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_xmod_menu(group_id, settings),
        )
        await query.answer()
        return

    if action == "checkbio":
        settings = await db.get_group_settings(group_id)
        await db.set_group_setting(group_id, "xmod_check_bio", 0 if settings.xmod_check_bio else 1)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _xmod_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_xmod_menu(group_id, settings),
        )
        await query.answer()
        return

    if action == "checkphoto":
        settings = await db.get_group_settings(group_id)
        await db.set_group_setting(group_id, "xmod_check_photo", 0 if settings.xmod_check_photo else 1)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _xmod_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_xmod_menu(group_id, settings),
        )
        await query.answer()
        return

    if action == "punishment":
        order = ["ban", "mute"]
        settings = await db.get_group_settings(group_id)
        next_p = order[(order.index(settings.xmod_punishment) + 1) % len(order)]
        await db.set_group_setting(group_id, "xmod_punishment", next_p)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _xmod_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_xmod_menu(group_id, settings),
        )
        await query.answer()
        return

    if action == "duration":
        rows = []
        row = []
        for label, seconds in MUTE_DURATIONS:
            row.append(InlineKeyboardButton(label, callback_data=f"xm:setdur:{group_id}:{seconds}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"xm:menu:{group_id}")])
        await query.edit_message_text(
            "⏱ Elige la duración del mute:", reply_markup=InlineKeyboardMarkup(rows)
        )
        await query.answer()
        return

    if action == "setdur":
        seconds = int(parts[3])
        await db.set_group_setting(group_id, "xmod_mute_seconds", seconds)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _xmod_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_xmod_menu(group_id, settings),
        )
        await query.answer("Duración actualizada.")
        return

    await query.answer()


async def try_consume_pending_xmod_words(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending: Optional[dict] = context.user_data.get("pending_xmod_words")
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
        context.user_data.pop("pending_xmod_words", None)
        await message.reply_text("❌ Cancelado.")
        return True

    words = _split_words(text)
    if action == "add":
        added = 0
        for w in words:
            if await db.add_xmod_word(group_id, w, update.effective_user.id):
                added += 1
        summary = success(f"Se agregaron {added} palabra(s)/emoji(s) prohibido(s).")
    else:
        removed = 0
        for w in words:
            if await db.remove_xmod_word(group_id, w):
                removed += 1
        summary = success(f"Se eliminaron {removed} palabra(s)/emoji(s) del filtro.")

    context.user_data.pop("pending_xmod_words", None)
    settings = await db.get_group_settings(group_id)
    menu_text = await _xmod_text(db, group_id)
    menu_markup = _build_xmod_menu(group_id, settings)

    edited = False
    if chat_id is not None and message_id is not None:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=menu_text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=menu_markup,
            )
            edited = True
        except TelegramError as exc:
            logger.warning("No pude editar el menú de moderación extrema tras la edición: %s", exc)

    await message.reply_text(summary)
    if not edited:
        await message.reply_text(menu_text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=menu_markup)
    return True
