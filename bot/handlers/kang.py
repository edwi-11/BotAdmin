"""
handlers/kang.py
/kang y /steal — el clásico comando de "robar" un sticker: se usa
respondiendo a un sticker o a una imagen, y el bot lo agrega a un
paquete de stickers propio del usuario. Si el usuario todavía no tiene
un paquete creado por el bot, se crea automáticamente en el momento —
no hace falta ningún paso manual de por medio.

Cómo se organizan los paquetes:
- Telegram no permite mezclar formatos distintos en un mismo paquete
  (estático, animado -TGS-, y video -WEBM- son paquetes separados), así
  que cada usuario puede terminar con hasta 3 paquetes propios según qué
  tipo de sticker vaya kangeando.
- Cada paquete de Telegram tiene un límite de figuritas (120). Cuando se
  llena, el bot abre solo un "volumen" nuevo (vol 2, vol 3, ...) sin que
  el usuario tenga que hacer nada.
- El nombre interno del paquete sigue el formato que exige Telegram:
  empieza con letra, solo letras/números/guion bajo, y termina en
  "_by_<usuario_del_bot>".

Uso:
    Respondiendo a un sticker o imagen:
    /kang            -> usa el emoji del sticker original, o 🤔 si es una imagen
    /kang 😂         -> usa ese emoji para la figurita
    /steal           -> alias de /kang, hace exactamente lo mismo
"""
from __future__ import annotations

import io
import logging

from PIL import Image
from telegram import InputSticker, Update
from telegram.constants import StickerFormat
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from database import Database

logger = logging.getLogger(__name__)

_MAX_STICKERS_PER_PACK = 120
_DEFAULT_EMOJI = "🤔"
_MAX_SIDE = 512


def _pack_name(user_id: int, fmt: str, volume: int, bot_username: str) -> str:
    suffix = {"static": "static", "animated": "anim", "video": "vid"}[fmt]
    base = f"a{user_id}_{suffix}"
    if volume > 1:
        base = f"{base}_v{volume}"
    return f"{base}_by_{bot_username}"


def _pack_title(display_name: str, fmt: str, volume: int) -> str:
    label = {"static": "Kang Pack", "animated": "Kang Pack Animado", "video": "Kang Pack Video"}[fmt]
    title = f"{display_name}'s {label}"
    if volume > 1:
        title += f" vol{volume}"
    # Telegram corta el título en 64 caracteres, lo acortamos nosotros
    # antes para no depender de que la API lo trunque de forma rara.
    return title[:64]


def _convert_to_sticker_bytes(raw: bytes) -> bytes:
    """Redimensiona una imagen cualquiera para que cumpla el requisito de
    Telegram (máximo 512px de lado, uno de los dos lados exactamente en
    512) y la devuelve como WEBP (liviano, mantiene transparencia)."""
    img = Image.open(io.BytesIO(raw))
    if img.mode not in ("RGBA", "RGB"):
        img = img.convert("RGBA")
    w, h = img.size
    if w >= h:
        new_w, new_h = _MAX_SIDE, max(1, round(h * _MAX_SIDE / w))
    else:
        new_h, new_w = _MAX_SIDE, max(1, round(w * _MAX_SIDE / h))
    img = img.resize((new_w, new_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=90, method=6)
    return buf.getvalue()


async def _get_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Determina de dónde sacar la figurita a partir del mensaje
    respondido. Devuelve (formato, bytes_o_file_id, es_file_id_reusable,
    emoji_original) o None si el mensaje no tiene nada usable."""
    replied = update.effective_message.reply_to_message
    if replied is None:
        return None

    if replied.sticker:
        sticker = replied.sticker
        if sticker.is_video:
            fmt = "video"
        elif sticker.is_animated:
            fmt = "animated"
        else:
            fmt = "static"
        # Reusamos el file_id directo: no hace falta descargar ni
        # convertir nada, Telegram permite reutilizar un sticker
        # existente como InputSticker.sticker pasando el file_id.
        return fmt, sticker.file_id, True, sticker.emoji

    if replied.photo:
        file = await context.bot.get_file(replied.photo[-1].file_id)
        raw = bytes(await file.download_as_bytearray())
        return "static", _convert_to_sticker_bytes(raw), False, None

    if replied.document and (replied.document.mime_type or "").startswith("image/"):
        file = await context.bot.get_file(replied.document.file_id)
        raw = bytes(await file.download_as_bytearray())
        return "static", _convert_to_sticker_bytes(raw), False, None

    return "unsupported", None, False, None


async def _ensure_pack(
    context: ContextTypes.DEFAULT_TYPE,
    db: Database,
    user_id: int,
    display_name: str,
    fmt: str,
    input_sticker: InputSticker,
) -> tuple[str, int]:
    """Consigue (creando si hace falta) un paquete con lugar libre para
    este usuario/formato, agrega la figurita, y devuelve (pack_name,
    volume) del paquete donde terminó quedando."""
    bot_username = context.bot.username
    latest = await db.get_latest_kang_pack(user_id, fmt)

    if latest is None:
        # Primera vez que este usuario kangea algo de este formato:
        # el bot le crea el paquete en el momento, sin pedirle nada más.
        volume = 1
        pack_name = _pack_name(user_id, fmt, volume, bot_username)
        await context.bot.create_new_sticker_set(
            user_id=user_id,
            name=pack_name,
            title=_pack_title(display_name, fmt, volume),
            stickers=[input_sticker],
        )
        await db.create_kang_pack(user_id, fmt, volume, pack_name)
        await db.bump_kang_pack_count(user_id, fmt, volume)
        return pack_name, volume

    volume = latest["volume"]
    pack_name = latest["pack_name"]
    count = latest["sticker_count"]

    if count >= _MAX_STICKERS_PER_PACK:
        # Se llenó: abrimos un volumen nuevo automáticamente.
        volume += 1
        pack_name = _pack_name(user_id, fmt, volume, bot_username)
        await context.bot.create_new_sticker_set(
            user_id=user_id,
            name=pack_name,
            title=_pack_title(display_name, fmt, volume),
            stickers=[input_sticker],
        )
        await db.create_kang_pack(user_id, fmt, volume, pack_name)
        await db.bump_kang_pack_count(user_id, fmt, volume)
        return pack_name, volume

    try:
        await context.bot.add_sticker_to_set(user_id=user_id, name=pack_name, sticker=input_sticker)
    except BadRequest as exc:
        if "STICKERS_TOO_MUCH" in str(exc) or "invalid" in str(exc).lower():
            # El paquete existía en nuestra base pero ya no en Telegram
            # (o se llenó sin que lo hayamos notado): abrimos uno nuevo.
            volume += 1
            pack_name = _pack_name(user_id, fmt, volume, bot_username)
            await context.bot.create_new_sticker_set(
                user_id=user_id,
                name=pack_name,
                title=_pack_title(display_name, fmt, volume),
                stickers=[input_sticker],
            )
            await db.create_kang_pack(user_id, fmt, volume, pack_name)
            await db.bump_kang_pack_count(user_id, fmt, volume)
            return pack_name, volume
        raise

    await db.bump_kang_pack_count(user_id, fmt, volume)
    return pack_name, volume


async def kang_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if user is None or user.is_bot:
        return

    source = await _get_source(update, context)
    if source is None:
        await message.reply_text(
            "Respondé a un sticker o a una imagen con /kang (o /steal) para agregarlo a tu paquete."
        )
        return

    fmt, payload, is_file_id, original_emoji = source
    if fmt == "unsupported":
        await message.reply_text(
            "Eso no lo puedo kangear todavía — funciona con stickers, fotos, o imágenes mandadas como archivo. "
            "Videos y GIFs sueltos (sin ser ya un sticker de video) no están soportados por ahora."
        )
        return

    emoji = _DEFAULT_EMOJI
    if context.args:
        candidate = context.args[0].strip()
        if candidate:
            emoji = candidate
    elif original_emoji:
        emoji = original_emoji

    sticker_format = {"static": StickerFormat.STATIC, "animated": StickerFormat.ANIMATED, "video": StickerFormat.VIDEO}[fmt]
    input_sticker = InputSticker(sticker=payload, emoji_list=[emoji], format=sticker_format)

    display_name = (user.username or user.first_name or "usuario").replace(" ", "_")[:20]

    status_msg = await message.reply_text("⏳ Kangeando...")
    try:
        pack_name, volume = await _ensure_pack(context, context.application.bot_data["db"], user.id, display_name, fmt, input_sticker)
    except BadRequest as exc:
        logger.warning("No se pudo kangear para %s: %s", user.id, exc)
        text = str(exc)
        if "PEER_ID_INVALID" in text or "user not found" in text.lower():
            await status_msg.edit_text(
                "⚠️ No puedo crear el paquete todavía — primero tenés que abrir un chat privado "
                "conmigo (tocá mi nombre y mandame /start) al menos una vez."
            )
        else:
            await status_msg.edit_text(f"⚠️ No pude agregar la figurita: {exc}")
        return
    except TelegramError as exc:
        logger.warning("Error de Telegram al kangear para %s: %s", user.id, exc)
        await status_msg.edit_text(f"⚠️ No pude agregar la figurita: {exc}")
        return

    await status_msg.edit_text(
        f"✅ Agregado a tu paquete.\n👉 https://t.me/addstickers/{pack_name}",
        disable_web_page_preview=False,
    )
