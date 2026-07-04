"""
utils/callbacks.py
Decorador de seguridad para CallbackQueryHandler.

Bug que arregla: cuando un botón inline llama a un handler y ese handler
lanza una excepción (por ejemplo, TelegramError al editar un mensaje que
no cambió, un error de red, o cualquier bug), Telegram NUNCA recibe la
confirmación (answerCallbackQuery) y el botón se queda con el "reloj de
carga" girando indefinidamente en el cliente del usuario, dando la
sensación de que el botón "no funciona" (aunque el comando por / sí,
porque ese flujo no depende de responder un callback).

Con @safe_callback, pase lo que pase dentro del handler, SIEMPRE se
responde el callback (con un aviso de error si algo falló), así el botón
nunca se queda colgado.
"""
from __future__ import annotations

import functools
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def safe_callback(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        try:
            return await func(update, context)
        except TelegramError as exc:
            # "Message is not modified" y similares no son errores reales
            # para el usuario: solo avisamos y respondemos el callback para
            # que el botón deje de "cargar", sin romper el bot.
            logger.warning("TelegramError en callback %s: %s", func.__name__, exc)
        except Exception:
            logger.exception("Error inesperado en callback %s", func.__name__)
        # Si el handler ya respondió el callback dentro de su try, esta
        # llamada extra es inofensiva (Telegram simplemente la ignora);
        # si no llegó a responderlo por la excepción, esto evita que el
        # botón se quede "cargando" para siempre.
        if query is not None:
            try:
                await query.answer("⚠️ Ocurrió un error, intenta de nuevo.")
            except TelegramError:
                pass
    return wrapper
