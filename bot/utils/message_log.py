"""
utils/message_log.py
Telegram no deja que un bot pida el historial de un chat (solo puede ver los
mensajes que le van llegando en vivo), así que para poder implementar
`/q 2`, `/q 3`, etc. (citar el mensaje respondido + los N mensajes
anteriores) mantenemos nosotros mismos un historial corto y en memoria de
los últimos mensajes de cada chat.

No se persiste en disco a propósito: solo hace falta para citar mensajes
"recientes" mientras el bot está corriendo; no tiene sentido guardarlo
permanentemente en la base de datos.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from telegram import Message, Update
from telegram.ext import ContextTypes

# Cuántos mensajes recientes se recuerdan por chat.
_MAX_PER_CHAT = 400


@dataclass(slots=True)
class MessageStub:
    message_id: int
    user_id: int
    name: str
    username: Optional[str]
    text: str


_LOG: dict[int, "deque[MessageStub]"] = {}


def describe_media(message: Message) -> str:
    """Texto representativo para mensajes sin texto/caption (fotos, stickers, etc.)."""
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    if message.sticker:
        emoji = message.sticker.emoji or "🖼️"
        return f"{emoji} Sticker"
    if message.photo:
        return "📷 Foto"
    if message.video:
        return "🎥 Video"
    if message.animation:
        return "🎞️ GIF"
    if message.voice:
        return "🎤 Nota de voz"
    if message.audio:
        return "🎵 Audio"
    if message.document:
        return "📄 Documento"
    if message.video_note:
        return "⭕ Nota de video"
    if message.poll:
        return f"📊 {message.poll.question}"
    if message.location:
        return "📍 Ubicación"
    if message.contact:
        return "👤 Contacto"
    if message.dice:
        return f"🎲 {message.dice.emoji}"
    return ""


def record(message: Optional[Message]) -> None:
    """Guarda (o actualiza) un mensaje en el historial en memoria de su chat."""
    if message is None or message.chat is None or message.chat.type == "private":
        return

    if message.from_user:
        name = message.from_user.first_name or message.from_user.username or "Usuario"
        username = message.from_user.username
        user_id = message.from_user.id
    elif message.sender_chat:
        name = message.sender_chat.title or "Canal"
        username = message.sender_chat.username
        user_id = message.sender_chat.id
    else:
        return

    stub = MessageStub(
        message_id=message.message_id,
        user_id=user_id,
        name=name,
        username=username,
        text=describe_media(message),
    )

    buf = _LOG.setdefault(message.chat.id, deque(maxlen=_MAX_PER_CHAT))
    if buf and buf[-1].message_id == stub.message_id:
        return  # ya lo teníamos (update duplicado, ej. edición reprocesada)
    buf.append(stub)


def get_previous(chat_id: int, message_id: int, count: int) -> list[MessageStub]:
    """Hasta `count` mensajes anteriores a `message_id` (sin incluirlo),
    ordenados del más antiguo al más nuevo."""
    if count <= 0:
        return []
    buf = _LOG.get(chat_id)
    if not buf:
        return []
    earlier = [stub for stub in buf if stub.message_id < message_id]
    earlier.sort(key=lambda s: s.message_id)
    return earlier[-count:]


async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler genérico: registra CUALQUIER mensaje de grupo que pase por el
    bot (se registra en un `group` bien temprano para no depender de que
    ningún otro handler lo procese primero)."""
    record(update.effective_message)
