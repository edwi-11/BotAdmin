"""
utils/action_stickers.py
Envía el sticker configurado (por file_id) para un evento del bot
(ban, mute, brb, vuelta de brb, del). Si no hay sticker configurado para
ese evento, no hace nada. Si Telegram rechaza el envío (file_id inválido,
por ejemplo), se registra en el log pero nunca rompe el comando.
"""
from __future__ import annotations

import logging

from telegram import Bot
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


async def send_action_sticker(bot: Bot, chat_id: int, file_id: str) -> None:
    if not file_id:
        return
    try:
        await bot.send_sticker(chat_id, file_id)
    except TelegramError as exc:
        logger.warning("No pude enviar el sticker %s en el chat %s: %s", file_id, chat_id, exc)
