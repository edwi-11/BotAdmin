"""
handlers/gemini_chat.py
Integración con la API de Gemini (Google AI Studio). Cuando alguien escribe
un mensaje que EMPIEZA con la palabra "ceo" (sin importar mayúsculas o
minúsculas: "CEO", "Ceo", "ceo", etc.) en un grupo ACTIVADO, el bot le
manda el resto del mensaje a Gemini y responde con el resultado, como si
fuera una persona normal charlando (con emojis, tono natural, etc).

Ejemplos que disparan la respuesta de TEXTO:
    "ceo que hora es en nicaragua"
    "CEO cuéntame un chiste"
    "Ceo, ¿cómo estás?"

Si después de "ceo" sigue la palabra "audio", en vez de texto se genera
una NOTA DE VOZ real (usando el modelo TTS de Gemini) con lo que sigue:
    "ceo audio: diles buenos días a todos"
    "CEO audio cuéntales un chiste"

Requiere GEMINI_API_KEY configurada en el .env (ver README para sacar una
key gratis en https://aistudio.google.com/apikey). Si no está configurada,
el trigger simplemente no hace nada (no rompe el bot).

La función de audio además requiere tener `ffmpeg` instalado en el
servidor (para convertir el audio crudo que devuelve Gemini al formato
OGG/Opus que exige Telegram para notas de voz):
    apt install -y ffmpeg
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import tempfile
import uuid
from pathlib import Path

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

# Si justo después viene la palabra "audio", el resto se convierte en nota
# de voz en vez de responderse como texto: "ceo audio: <texto>".
_AUDIO_RE = re.compile(r"^audio\b[\s,:.\-]*", re.IGNORECASE)

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


# --------------------------------------------------------------------- #
# Texto (chat normal)
# --------------------------------------------------------------------- #
async def _ask_gemini(prompt: str) -> str:
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


# --------------------------------------------------------------------- #
# Audio (texto a voz)
# --------------------------------------------------------------------- #
def _parse_sample_rate(mime_type: str) -> int:
    match = re.search(r"rate=(\d+)", mime_type or "")
    return int(match.group(1)) if match else 24000


async def _pcm_to_ogg(pcm_bytes: bytes, sample_rate: int) -> Path:
    """Convierte audio PCM crudo (como lo entrega Gemini) a un .ogg/Opus,
    el único formato que Telegram acepta para notas de voz (send_voice).
    Requiere que `ffmpeg` esté instalado en el sistema."""
    out_path = Path(tempfile.gettempdir()) / f"ceo_tts_{uuid.uuid4().hex}.ogg"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
            "-c:a", "libopus", "-b:a", "48k", "-y", str(out_path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise GeminiError(
            "ffmpeg no está instalado en el servidor. Instálalo con: apt install -y ffmpeg"
        ) from exc

    _, stderr = await proc.communicate(input=pcm_bytes)
    if proc.returncode != 0 or not out_path.exists():
        raise GeminiError(f"ffmpeg falló al convertir el audio: {stderr.decode(errors='ignore')[:300]}")
    return out_path


async def _generate_voice_note(text: str) -> Path:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": settings.gemini_tts_voice}}
            },
        },
    }
    url = _GEMINI_URL_TEMPLATE.format(model=settings.gemini_tts_model)

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, params={"key": settings.gemini_api_key}, json=payload)
        resp.raise_for_status()
        data = resp.json()

    try:
        part = data["candidates"][0]["content"]["parts"][0]["inlineData"]
        pcm_bytes = base64.b64decode(part["data"])
        sample_rate = _parse_sample_rate(part.get("mimeType", ""))
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("Respuesta de audio inesperada de Gemini: %s", data)
        raise GeminiError("Gemini no devolvió audio (puede haber bloqueado el texto pedido)") from exc

    return await _pcm_to_ogg(pcm_bytes, sample_rate)


async def _handle_audio_request(update: Update, context: ContextTypes.DEFAULT_TYPE, text_to_speak: str) -> None:
    message = update.effective_message
    chat = update.effective_chat

    if not text_to_speak:
        await message.reply_text(
            "🎙️ Dime qué quieres que diga. Ejemplo: «ceo audio: hola a todos, buenos días»"
        )
        return

    try:
        await context.bot.send_chat_action(chat.id, ChatAction.RECORD_VOICE)
    except Exception:  # noqa: BLE001
        pass

    try:
        ogg_path = await _generate_voice_note(text_to_speak)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Error generando audio con Gemini: %s", exc)
        await message.reply_text("😅 No pude generar el audio ahora mismo, intenta de nuevo en un ratito.")
        return

    try:
        with open(ogg_path, "rb") as audio_file:
            await message.reply_voice(audio_file)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Error enviando la nota de voz: %s", exc)
        await message.reply_text("😅 Generé el audio pero no pude enviarlo, intenta de nuevo.")
    finally:
        try:
            ogg_path.unlink(missing_ok=True)
        except OSError:
            pass


# --------------------------------------------------------------------- #
# Trigger principal
# --------------------------------------------------------------------- #
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

    remainder = message.text[match.end():].strip()

    # ¿Pidieron audio? ("ceo audio: <texto>" / "ceo audio <texto>")
    audio_match = _AUDIO_RE.match(remainder)
    if audio_match:
        text_to_speak = remainder[audio_match.end():].strip()
        await _handle_audio_request(update, context, text_to_speak)
        return

    # Chat normal de texto
    question = remainder or "Salúdame brevemente y pregúntame en qué puedes ayudar."

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
