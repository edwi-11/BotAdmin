"""
handlers/activity_ranking.py
Comando /top: ranking de los 10 usuarios que más mensajes mandaron en el
grupo, con una imagen dinámica (ver utils/ranking_image.py) y tres
botones [ Hoy ] [ Semana ] [ Siempre ] que EDITAN esa misma imagen
(callback_data: ranking_today / ranking_week / ranking_all), sin mandar
mensajes nuevos.

Nota de nombre: el bot ya tenía un comando /ranking (top de monedas,
ver handlers/economy.py). Para no romper esa función existente, este
ranking de actividad se expone como /top.

La actividad en sí (contar los mensajes) se registra en
utils/activity_stats.py, no acá.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from database import Database
from utils.formatting import error
from utils.ranking_image import PERIOD_LABELS, build_ranking_image

logger = logging.getLogger(__name__)

_PERIODS = ("today", "week", "all")
_NO_DATA_TEXT = {
    "today": "Todavía nadie mandó mensajes hoy en este grupo.",
    "week": "Todavía nadie mandó mensajes esta semana en este grupo.",
    "all": "Todavía no hay mensajes registrados en este grupo.",
}


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _keyboard(active: str) -> InlineKeyboardMarkup:
    row = []
    for period in _PERIODS:
        label = PERIOD_LABELS[period]
        text = f"• {label} •" if period == active else label
        row.append(InlineKeyboardButton(text, callback_data=f"ranking_{period}"))
    return InlineKeyboardMarkup([row])


async def top_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return

    db = _get_db(context)
    period = "all"
    photo = await build_ranking_image(context.bot, db, chat, period)
    if photo is None:
        await message.reply_text(_NO_DATA_TEXT[period])
        return

    await message.reply_photo(photo=photo, reply_markup=_keyboard(period))


async def ranking_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat = update.effective_chat
    period = (query.data or "").removeprefix("ranking_")
    if period not in _PERIODS:
        await query.answer()
        return

    db = _get_db(context)
    photo = await build_ranking_image(context.bot, db, chat, period)
    if photo is None:
        await query.answer(_NO_DATA_TEXT[period], show_alert=True)
        return

    try:
        await query.edit_message_media(
            media=InputMediaPhoto(photo, filename=f"ranking_{period}.png"),
            reply_markup=_keyboard(period),
        )
    except BadRequest as exc:
        # "Message is not modified" si tocan la misma pestaña ya activa,
        # o el mensaje fue borrado mientras tanto: no rompemos el flujo.
        if "not modified" not in str(exc).lower():
            logger.warning("No se pudo editar la imagen del ranking: %s", exc)
    await query.answer()
