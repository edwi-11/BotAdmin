"""
handlers/gemini_chat.py
Integración con la API de Gemini (Google AI Studio). Cuando alguien escribe
un mensaje que EMPIEZA con la palabra "ceo" (sin importar mayúsculas o
minúsculas: "CEO", "Ceo", "ceo", etc.) en un grupo ACTIVADO, el bot le
manda el resto del mensaje a Gemini y responde con el resultado, como si
fuera una persona normal charlando (con emojis, tono natural, etc).

Ejemplos que disparan la respuesta:
    "ceo que hora es en nicaragua"
    "CEO cuéntame un chiste"
    "Ceo, ¿cómo estás?"

Requiere GEMINI_API_KEY configurada en el .env (ver README para sacar una
key gratis en https://aistudio.google.com/apikey). Si no está configurada,
el trigger simplemente no hace nada (no rompe el bot).
"""
from __future__ import annotations

import logging
import re

import httpx
from telegram import Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import ContextTypes

from config import settings
from database import Database

logger = logging.getLogger(__name__)

# Dispara con "ceo" al INICIO del mensaje (mayúsc/minúsc, con o sin coma/
# dos puntos después: "ceo,", "ceo:", "ceo que hora es...").
_TRIGGER_RE = re.compile(r"^\s*ceo\b[\s,:.\-]*", re.IGNORECASE)

_GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_SYSTEM_PROMPT = (
    "Eres 'CEO', un integrante más de un grupo de Telegram que responde de forma natural, "
    "como una persona normal y cercana, no como un asistente formal. Escribe en español "
    "neutro/latino, con un tono relajado, amistoso y espontáneo. Usa emojis de vez en "
    "cuando para darle calidez, sin abusar de ellos. Sé breve: la mayoría de tus "
    "respuestas deben caber en 1-4 frases, salvo que te pidan explícitamente algo más "
    "largo o detallado. No expliques que eres una IA ni des rodeos innecesarios, ve "
    "directo a responder lo que te preguntan."
)


class GeminiError(Exception):
    pass


async def _ask_gemini(prompt: str) -> str:
    if not settings.gemini_api_key:
        raise GeminiError("GEMINI_API_KEY no está configurada")

    payload = {
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 500},
    }
    url = _GEMINI_URL_TEMPLATE.format(model=settings.gemini_model)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, params={"key": settings.gemini_api_key}, json=payload)
        resp.raise_for_status()
        data = resp.json()

    try:
        candidate = data["candidates"][0]
        parts = candidate["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise KeyError("texto vacío")
        return text
    except (KeyError, IndexError, TypeError) as exc:
        finish_reason = data.get("candidates", [{}])[0].get("finishReason") if data.get("candidates") else None
        logger.warning("Respuesta inesperada de Gemini (finishReason=%s): %s", finish_reason, data)
        raise GeminiError("Respuesta vacía o bloqueada por Gemini") from exc


async def ceo_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or not message.text or chat is None or chat.type == ChatType.PRIVATE:
        return

    match = _TRIGGER_RE.match(message.text)
    if not match:
        return

    if not settings.gemini_api_key:
        return  # función no configurada todavía: ignorar en silencio

    db: Database = context.application.bot_data["db"]
    if not await db.is_group_activated(chat.id):
        return  # solo respondemos en grupos activados por el owner

    question = message.text[match.end():].strip()
    if not question:
        question = "Salúdame brevemente y pregúntame en qué puedes ayudar."

    try:
        await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    except Exception:  # noqa: BLE001
        pass

    try:
        answer = await _ask_gemini(question)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Error consultando Gemini: %s", exc)
        await message.reply_text("😅 Se me trabó la cabeza justo ahora, intenta de nuevo en un ratito.")
        return

    await message.reply_text(answer)
