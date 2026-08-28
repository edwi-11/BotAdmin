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

RESPALDO AUTOMÁTICO CON GROQ: si Gemini falla (por ejemplo, se acabó la
cuota gratuita del día), y hay una GROQ_API_KEY configurada en el .env
(gratis, sin tarjeta, en https://console.groq.com/keys), el bot reintenta
automáticamente la misma pregunta con Groq (modelo Llama) para no quedarse
sin responder. Esto solo aplica al chat de TEXTO; el audio ("ceo audio")
sigue dependiendo únicamente de Gemini, ya que Groq no ofrece un TTS
equivalente en este flujo.

La función de audio además requiere tener `ffmpeg` instalado en el
servidor (para convertir el audio crudo que devuelve Gemini al formato
OGG/Opus que exige Telegram para notas de voz):
    apt install -y ffmpeg
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

import httpx
from telegram import Update
from telegram.constants import ChatAction, ChatType
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from config import settings
from database import Database

logger = logging.getLogger(__name__)

# Cada cuánto (segundos) se actualiza el mensaje mientras se "escribe en
# vivo". Menos que esto y arriesgamos pegarle demasiado rápido a los
# límites de edición de Telegram; más y se ve menos fluido.
_STREAM_EDIT_THROTTLE = 1.2
# Tope de caracteres por edición (Telegram permite hasta 4096 por mensaje).
_STREAM_MAX_CHARS = 4000

# Dispara con "ceo" al INICIO del mensaje (mayúsc/minúsc, con o sin coma/
# dos puntos después: "ceo,", "ceo:", "ceo que hora es...").
_TRIGGER_RE = re.compile(r"^\s*ceo\b[\s,:.\-]*", re.IGNORECASE)

# Si justo después viene la palabra "audio", el resto se convierte en nota
# de voz en vez de responderse como texto: "ceo audio: <texto>".
_AUDIO_RE = re.compile(r"^audio\b[\s,:.\-]*", re.IGNORECASE)

_GEMINI_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

_SYSTEM_PROMPT = (
    "Eres un integrante más de un grupo de Telegram, no un asistente corporativo ni "
    "formal. Escribes en español neutro/latino, tal como hablaría cualquier persona "
    "del grupo, no como un ejecutivo ni con lenguaje de oficina.\n\n"
    "Se te muestra el historial reciente de la conversación con esta persona (si lo "
    "hay) antes del mensaje nuevo. Úsalo para seguir el hilo: si la pregunta se conecta "
    "con algo dicho antes, respóndela con ese contexto en mente en vez de repetirte o "
    "ignorar lo anterior, como haría cualquiera siguiendo una charla.\n\n"
    "Sobre el tono: fíjate en cómo habla la gente en ese chat (formal, relajado, con "
    "groserías leves, sarcástico, con modismos locales, etc.) y responde en ese mismo "
    "registro, no con un tono propio fijo. Evita los emojis salvo que aporten algo "
    "puntual; nunca los uses en cada frase ni como muletilla.\n\n"
    "Adapta la extensión según lo que te pregunten:\n"
    "- Para saludos, comentarios casuales, bromas o preguntas simples/random: responde "
    "breve y natural, 1-4 frases, sin sonar impostado.\n"
    "- Para preguntas específicas, técnicas, que pidan un dato concreto, una explicación, "
    "instrucciones, o algo que requiera precisión (cálculos, definiciones, cómo hacer algo, "
    "hechos, tutoriales, etc.): responde clara, completa y bien explicada, con el nivel de "
    "detalle que la pregunta necesite (puede ser más larga si hace falta, con pasos o "
    "puntos si ayuda a entender mejor), sin sacrificar precisión por el tono.\n\n"
    "No expliques que eres una IA, que estás 'siguiendo el hilo' o que estás adaptando "
    "el tono; simplemente responde cada mensaje como corresponda."
)

# Historial de conversación en memoria, por (chat_id, user_id), para que el
# bot pueda seguir el hilo si la misma persona le vuelve a escribir. Se
# guardan como máximo las últimas _MAX_HISTORY_TURNS interacciones (par
# pregunta/respuesta) por conversación, para no disparar el consumo de
# tokens ni arrastrar contexto viejo indefinidamente.
_MAX_HISTORY_TURNS = 8
_conversation_history: dict[tuple[int, int], list[dict[str, str]]] = {}


def _get_history(chat_id: int, user_id: int) -> list[dict[str, str]]:
    return _conversation_history.setdefault((chat_id, user_id), [])


def _push_history(chat_id: int, user_id: int, question: str, answer: str) -> None:
    history = _get_history(chat_id, user_id)
    history.append({"role": "user", "text": question})
    history.append({"role": "model", "text": answer})
    excess = len(history) - _MAX_HISTORY_TURNS * 2
    if excess > 0:
        del history[:excess]


class GeminiError(Exception):
    pass


# --------------------------------------------------------------------- #
# Texto (chat normal)
# --------------------------------------------------------------------- #
async def _ask_gemini(prompt: str, history: list[dict[str, str]] | None = None) -> str:
    contents = [
        {"role": turn["role"], "parts": [{"text": turn["text"]}]}
        for turn in (history or [])
    ]
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    payload = {
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 1000},
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


async def _ask_groq(prompt: str, history: list[dict[str, str]] | None = None) -> str:
    """Respaldo gratuito (sin tarjeta) cuando Gemini falla o se quedó sin
    cuota. Usa la API de Groq, compatible con el formato de OpenAI."""
    if not settings.groq_api_key:
        raise GeminiError("Groq no está configurado (falta GROQ_API_KEY en el .env)")

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for turn in (history or []):
        role = "assistant" if turn["role"] == "model" else "user"
        messages.append({"role": role, "content": turn["text"]})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": settings.groq_model,
        "messages": messages,
        "temperature": 0.9,
        "max_tokens": 1000,
    }
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(_GROQ_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    try:
        text = data["choices"][0]["message"]["content"].strip()
        if not text:
            raise KeyError("texto vacío")
        return text
    except (KeyError, IndexError, TypeError) as exc:
        logger.warning("Respuesta inesperada de Groq: %s", data)
        raise GeminiError("Respuesta vacía o bloqueada por Groq") from exc


async def _ask_ai(prompt: str, history: list[dict[str, str]] | None = None) -> str:
    """Intenta responder con Gemini primero. Si falla por CUALQUIER motivo
    (cuota agotada, error de red, respuesta bloqueada, etc.), o si Gemini
    ni siquiera está configurado, y hay una GROQ_API_KEY configurada,
    reintenta automáticamente con Groq antes de rendirse. Así el bot casi
    nunca se queda "mudo" por falta de cuota."""
    if not settings.gemini_api_key:
        return await _ask_groq(prompt, history)

    try:
        return await _ask_gemini(prompt, history)
    except Exception as gemini_exc:  # noqa: BLE001
        if not settings.groq_api_key:
            raise
        logger.info("Gemini falló (%s), usando respaldo Groq...", gemini_exc)
        try:
            return await _ask_groq(prompt, history)
        except Exception as groq_exc:  # noqa: BLE001
            logger.warning("El respaldo de Groq también falló: %s", groq_exc)
            raise groq_exc from gemini_exc


# --------------------------------------------------------------------- #
# Streaming de texto (respuesta "en vivo", como se escribe)
# --------------------------------------------------------------------- #
# Nota: Telegram agregó en marzo 2026 (Bot API 9.5) el método nativo
# sendMessageDraft para streaming, pero según la propia documentación de
# Telegram ese método solo sirve en chats privados; en grupos (que es
# donde vive "ceo") la manera soportada de simular streaming sigue siendo
# ir editando un mismo mensaje con editMessageText a medida que llegan
# pedazos de texto. Por eso acá no usamos sendMessageDraft: en vez de eso,
# consumimos el endpoint de streaming (SSE) de Gemini/Groq y vamos
# editando el mensaje ya enviado.
async def _stream_gemini(prompt: str, history: list[dict[str, str]] | None = None) -> AsyncIterator[str]:
    contents = [
        {"role": turn["role"], "parts": [{"text": turn["text"]}]}
        for turn in (history or [])
    ]
    contents.append({"role": "user", "parts": [{"text": prompt}]})

    payload = {
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.9, "maxOutputTokens": 1000},
    }
    url = _GEMINI_URL_TEMPLATE.format(model=settings.gemini_model) + ":streamGenerateContent"

    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream(
            "POST", url,
            params={"key": settings.gemini_api_key, "alt": "sse"},
            json=payload,
        ) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise GeminiError(f"Gemini streaming devolvió {resp.status_code}: {body[:300]!r}")

            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk_raw = line[len("data:"):].strip()
                if not chunk_raw or chunk_raw == "[DONE]":
                    continue
                try:
                    chunk = json.loads(chunk_raw)
                except json.JSONDecodeError:
                    continue
                try:
                    candidate = chunk["candidates"][0]
                    parts = candidate.get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts)
                except (KeyError, IndexError, TypeError):
                    text = ""
                if text:
                    yield text


async def _stream_groq(prompt: str, history: list[dict[str, str]] | None = None) -> AsyncIterator[str]:
    if not settings.groq_api_key:
        raise GeminiError("Groq no está configurado (falta GROQ_API_KEY en el .env)")

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for turn in (history or []):
        role = "assistant" if turn["role"] == "model" else "user"
        messages.append({"role": role, "content": turn["text"]})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": settings.groq_model,
        "messages": messages,
        "temperature": 0.9,
        "max_tokens": 1000,
        "stream": True,
    }
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}

    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream("POST", _GROQ_URL, headers=headers, json=payload) as resp:
            if resp.status_code >= 400:
                body = await resp.aread()
                raise GeminiError(f"Groq streaming devolvió {resp.status_code}: {body[:300]!r}")

            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                chunk_raw = line[len("data:"):].strip()
                if not chunk_raw or chunk_raw == "[DONE]":
                    continue
                try:
                    chunk = json.loads(chunk_raw)
                except json.JSONDecodeError:
                    continue
                try:
                    delta = chunk["choices"][0]["delta"].get("content") or ""
                except (KeyError, IndexError, TypeError):
                    delta = ""
                if delta:
                    yield delta


async def _stream_ai(prompt: str, history: list[dict[str, str]] | None = None) -> AsyncIterator[str]:
    """Igual que `_ask_ai` (Gemini primero, Groq de respaldo) pero
    entregando el texto en pedazos a medida que va llegando, para poder
    editar el mensaje en vivo. Si Gemini falla ANTES de soltar el primer
    pedazo, se reintenta completo con Groq (streaming también). Si Gemini
    falla a medias (ya se mostró texto parcial), se corta ahí -- no tiene
    sentido reiniciar con otro modelo cuando el usuario ya está viendo una
    respuesta a medio escribir.
    """
    if not settings.gemini_api_key:
        async for delta in _stream_groq(prompt, history):
            yield delta
        return

    got_any = False
    try:
        async for delta in _stream_gemini(prompt, history):
            got_any = True
            yield delta
    except Exception as exc:  # noqa: BLE001
        if got_any:
            logger.warning("El streaming de Gemini se cortó a medias: %s", exc)
            return
        if not settings.groq_api_key:
            raise
        logger.info("Gemini falló antes de responder (%s), reintentando con Groq...", exc)
        async for delta in _stream_groq(prompt, history):
            yield delta


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

    if not settings.gemini_api_key and not settings.groq_api_key:
        return  # función no configurada todavía (ni Gemini ni Groq): ignorar en silencio

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

    # Chat normal de texto: la respuesta se va mostrando en vivo, editando
    # el mismo mensaje a medida que llegan pedazos de texto (streaming).
    question = remainder or "Salúdame brevemente y pregúntame en qué puedes ayudar."
    user_id = update.effective_user.id if update.effective_user else 0
    history = _get_history(chat.id, user_id)

    try:
        await context.bot.send_chat_action(chat.id, ChatAction.TYPING)
    except Exception:  # noqa: BLE001
        pass

    placeholder = await message.reply_text("💭")

    buffer = ""
    last_shown: str | None = None
    last_edit = 0.0

    async def _flush(final: bool = False) -> None:
        nonlocal last_shown, last_edit
        shown = buffer[:_STREAM_MAX_CHARS]
        text_to_send = shown if final else f"{shown}▌"
        if text_to_send == last_shown:
            return
        try:
            await context.bot.edit_message_text(
                chat_id=chat.id, message_id=placeholder.message_id, text=text_to_send,
            )
            last_shown = text_to_send
            last_edit = time.monotonic()
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                logger.debug("No se pudo editar el mensaje en streaming: %s", exc)
        except TelegramError as exc:
            logger.debug("No se pudo editar el mensaje en streaming: %s", exc)

    try:
        async for delta in _stream_ai(question, history):
            buffer += delta
            if time.monotonic() - last_edit >= _STREAM_EDIT_THROTTLE:
                await _flush()
    except Exception as exc:  # noqa: BLE001
        if not buffer:
            logger.warning("Error consultando la IA (Gemini + Groq): %s", exc)
            try:
                await context.bot.edit_message_text(
                    chat_id=chat.id, message_id=placeholder.message_id,
                    text="😅 Se me trabó la cabeza justo ahora, intenta de nuevo en un ratito.",
                )
            except TelegramError:
                pass
            return
        logger.warning("La respuesta se cortó a medias: %s", exc)

    answer = buffer.strip()
    if not answer:
        answer = "😅 No me salió nada, intenta de nuevo en un ratito."
    await _flush(final=True)
    if len(answer) > _STREAM_MAX_CHARS:
        # Si se pasó del límite que mostramos en vivo, mandamos el resto
        # como mensaje(s) aparte para no perder texto.
        try:
            await message.reply_text(answer[_STREAM_MAX_CHARS:])
        except TelegramError:
            pass

    _push_history(chat.id, user_id, question, answer)
