"""
handlers/warnings.py
Sección "❗ Advertencias" del menú de configuración.

Permite, sin usar ningún comando con "/", configurar:
- Cuántas advertencias soporta un usuario antes de recibir el castigo
  automático (warn_limit).
- Qué castigo se aplica al llegar al límite: silenciar, expulsar o
  banear (warn_action).
- Si el castigo es "silenciar", por cuánto tiempo (warn_mute_seconds).

El registro de advertencias en sí se hace con /warn (y se revierte con
/unwarn), ya que requieren indicar un usuario objetivo (respondiendo a
su mensaje, con @usuario o con su ID), algo que no tiene sentido como
botón de menú.

Callback pattern: "^aw:"
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from database import Database
from utils.callbacks import safe_callback
from utils.formatting import escape_md, humanize_seconds
from utils.permissions import is_chat_admin

logger = logging.getLogger(__name__)

ACTION_LABELS = {"mute": "Silenciar (mute)", "kick": "Expulsar (kick)", "ban": "Banear (ban)"}
LIMIT_OPTIONS = [1, 2, 3, 4, 5, 7, 10]
MUTE_DURATIONS: list[tuple[str, int]] = [
    ("10 min", 600), ("30 min", 1800), ("1 h", 3600),
    ("6 h", 21600), ("1 día", 86400), ("7 días", 604800),
    ("Permanente", 0),
]


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _text(settings) -> str:
    duration = "Permanente" if settings.warn_mute_seconds == 0 else humanize_seconds(settings.warn_mute_seconds)
    lines = [
        "❗ *Advertencias*",
        "",
        f"🔢 Límite antes de castigar: *{settings.warn_limit}*",
        f"⚖️ Castigo automático: *{escape_md(ACTION_LABELS[settings.warn_action])}*",
    ]
    if settings.warn_action == "mute":
        lines.append(f"⏱ Duración del mute: *{escape_md(duration)}*")
    lines.extend([
        "",
        "Usa `/warn` \\(respondiendo al mensaje, con @usuario o su ID\\) "
        "para advertir a alguien, y `/unwarn` para quitarle una advertencia\\.",
    ])
    return "\n".join(lines)


def _menu(group_id: int, settings) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"🔢 Límite: {settings.warn_limit}", callback_data=f"aw:limit:{group_id}")],
        [InlineKeyboardButton(f"⚖️ Castigo: {ACTION_LABELS[settings.warn_action]}",
                               callback_data=f"aw:action:{group_id}")],
    ]
    if settings.warn_action == "mute":
        duration = "Permanente" if settings.warn_mute_seconds == 0 else humanize_seconds(settings.warn_mute_seconds)
        rows.append([InlineKeyboardButton(f"⏱ Duración: {duration}", callback_data=f"aw:duration:{group_id}")])
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"m:main:{group_id}")])
    return InlineKeyboardMarkup(rows)


@safe_callback
async def warnings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            _text(settings), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_menu(group_id, settings)
        )
        await query.answer()
        return

    if action == "limit":
        rows = []
        row = []
        for value in LIMIT_OPTIONS:
            row.append(InlineKeyboardButton(str(value), callback_data=f"aw:setlimit:{group_id}:{value}"))
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"aw:menu:{group_id}")])
        await query.edit_message_text(
            "🔢 ¿Después de cuántas advertencias se aplica el castigo?",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        await query.answer()
        return

    if action == "setlimit":
        value = int(parts[3])
        await db.set_group_setting(group_id, "warn_limit", value)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            _text(settings), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_menu(group_id, settings)
        )
        await query.answer("Límite actualizado.")
        return

    if action == "action":
        order = ["mute", "kick", "ban"]
        settings = await db.get_group_settings(group_id)
        next_action = order[(order.index(settings.warn_action) + 1) % len(order)]
        await db.set_group_setting(group_id, "warn_action", next_action)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            _text(settings), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_menu(group_id, settings)
        )
        await query.answer()
        return

    if action == "duration":
        rows = []
        row = []
        for label, seconds in MUTE_DURATIONS:
            row.append(InlineKeyboardButton(label, callback_data=f"aw:setdur:{group_id}:{seconds}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"aw:menu:{group_id}")])
        await query.edit_message_text(
            "⏱ Elige la duración del mute:", reply_markup=InlineKeyboardMarkup(rows)
        )
        await query.answer()
        return

    if action == "setdur":
        seconds = int(parts[3])
        await db.set_group_setting(group_id, "warn_mute_seconds", seconds)
        settings = await db.get_group_settings(group_id)
        await query.edit_message_text(
            _text(settings), parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_menu(group_id, settings)
        )
        await query.answer("Duración actualizada.")
        return

    await query.answer()
