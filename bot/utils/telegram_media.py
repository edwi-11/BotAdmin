"""
utils/telegram_media.py
Helpers para /q (quote_sticker.py): descargar y preparar en memoria
(como imágenes Pillow) todo lo que la tarjeta de cita necesita pedirle
a la API de Telegram — foto de perfil, miniaturas de fotos/stickers/
video/GIF, las imágenes de los emojis premium (custom_emoji), y las
imágenes de los emojis unicode normales (estilo Twemoji, descargadas
de GitHub) para que el nombre y el texto se vean igual sin depender de
que el servidor tenga una fuente de emoji-color instalada.

Todo lo de acá es "best effort": si algo falla (sin conexión, el bot no
tiene permiso, el archivo ya no existe, etc.) las funciones devuelven
None en vez de levantar una excepción, para que quote_sticker.py pueda
seguir armando la tarjeta sin esa parte.

Limitaciones conocidas (no hay forma de evitarlas solo con la Bot API):
- Stickers/emojis premium ANIMADOS (.tgs, formato Lottie) no se pueden
  rasterizar sin la librería `rlottie` (no es un paquete de Python puro
  y no viene instalada). En esos casos usamos la miniatura estática que
  Telegram sí manda (`thumbnail`), y si no hay, se omite.
- Stickers/GIFs en VIDEO (.webm) tampoco se pueden decodificar sin
  `ffmpeg`/`av`. Mismo fallback: se usa el `thumbnail` que manda Telegram.
- Los emojis unicode normales se descargan de GitHub (twemoji) la
  primera vez que aparecen y se cachean en memoria; el servidor
  necesita salida a internet hacia raw.githubusercontent.com. Si no la
  tiene, cae de nuevo a dibujar el carácter con la fuente de texto.
"""
from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import Iterable, Optional

import httpx
from PIL import Image, ImageDraw
from telegram import Bot, MessageEntity, PhotoSize
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- #
# Descarga genérica de un file_id a una imagen Pillow
# --------------------------------------------------------------------- #
async def _download_image(bot: Bot, file_id: str) -> Optional[Image.Image]:
    try:
        tg_file = await bot.get_file(file_id)
        raw = await tg_file.download_as_bytearray()
        img = Image.open(io_bytes(raw))
        img.load()
        return img.convert("RGBA")
    except (TelegramError, OSError, ValueError) as exc:
        logger.debug("No pude descargar el archivo %s: %s", file_id, exc)
        return None


def io_bytes(raw: bytearray):
    return io.BytesIO(bytes(raw))


# --------------------------------------------------------------------- #
# Recortes / máscaras
# --------------------------------------------------------------------- #
def circular_crop(img: Image.Image, size: int) -> Image.Image:
    """Recorta `img` a un círculo de `size`x`size` (cover-fit)."""
    img = img.convert("RGBA")
    w, h = img.size
    side = min(w, h)
    img = img.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
    img = img.resize((size, size), Image.LANCZOS)

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def rounded_crop(img: Image.Image, box_w: int, box_h: int, radius: int) -> Image.Image:
    """Escala `img` a cover-fit dentro de box_w x box_h y le redondea las esquinas."""
    img = img.convert("RGBA")
    w, h = img.size
    scale = max(box_w / w, box_h / h)
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - box_w) // 2
    top = (new_h - box_h) // 2
    img = img.crop((left, top, left + box_w, top + box_h))

    mask = Image.new("L", (box_w, box_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, box_w, box_h), radius=radius, fill=255)
    out = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def fit_within(img: Image.Image, max_w: int, max_h: int) -> Image.Image:
    """Escala manteniendo proporción para que quepa dentro de max_w x max_h."""
    w, h = img.size
    scale = min(max_w / w, max_h / h, 1.0) if w and h else 1.0
    if scale < 1.0:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
    return img


# --------------------------------------------------------------------- #
# Foto de perfil
# --------------------------------------------------------------------- #
async def fetch_avatar(bot: Bot, user_id: int, size: int) -> Optional[Image.Image]:
    """Foto de perfil circular del usuario, o None si no tiene / falla la descarga."""
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
    except TelegramError as exc:
        logger.debug("No pude pedir la foto de perfil de %s: %s", user_id, exc)
        return None
    if not photos or photos.total_count == 0:
        return None

    largest: PhotoSize = photos.photos[0][-1]
    img = await _download_image(bot, largest.file_id)
    if img is None:
        return None
    return circular_crop(img, size)


# --------------------------------------------------------------------- #
# Miniaturas de media (foto, sticker, video, gif, etc.)
# --------------------------------------------------------------------- #
@dataclass(slots=True)
class MediaThumb:
    kind: str                      # "photo" | "sticker" | "video" | "animation" | "video_note"
    image: Image.Image
    is_static: bool                # False si es un fallback (thumbnail de algo animado)


async def fetch_message_media(bot: Bot, message) -> Optional[MediaThumb]:
    """Miniatura representativa del contenido multimedia del mensaje (si tiene)."""
    try:
        if message.photo:
            img = await _download_image(bot, message.photo[-1].file_id)
            return MediaThumb("photo", img, True) if img else None

        if message.sticker:
            sticker = message.sticker
            if not sticker.is_animated and not sticker.is_video:
                img = await _download_image(bot, sticker.file_id)
                if img:
                    return MediaThumb("sticker", img, True)
            # Animado (.tgs) o video (.webm): no se puede rasterizar sin
            # rlottie/ffmpeg, así que usamos la miniatura estática si existe.
            if sticker.thumbnail:
                img = await _download_image(bot, sticker.thumbnail.file_id)
                if img:
                    return MediaThumb("sticker", img, False)
            return None

        if message.video:
            if message.video.thumbnail:
                img = await _download_image(bot, message.video.thumbnail.file_id)
                if img:
                    return MediaThumb("video", img, False)
            return None

        if message.animation:
            if message.animation.thumbnail:
                img = await _download_image(bot, message.animation.thumbnail.file_id)
                if img:
                    return MediaThumb("animation", img, False)
            return None

        if message.video_note:
            if message.video_note.thumbnail:
                img = await _download_image(bot, message.video_note.thumbnail.file_id)
                if img:
                    return MediaThumb("video_note", circular_crop(img, img.size[0]), False)
            return None

    except Exception as exc:  # noqa: BLE001
        logger.debug("No pude armar la miniatura del mensaje: %s", exc)
        return None

    return None


# --------------------------------------------------------------------- #
# Emojis premium (custom_emoji) — descarga best-effort de su imagen
# --------------------------------------------------------------------- #
async def fetch_custom_emoji_images(bot: Bot, entities: list[MessageEntity]) -> dict[str, Optional[Image.Image]]:
    """
    Devuelve {custom_emoji_id: imagen_o_None} para cada entidad custom_emoji.
    None significa "no se pudo rasterizar" (casi siempre porque es un emoji
    premium ANIMADO, .tgs/.webm) — en ese caso quote_sticker.py debe caer
    de nuevo al carácter unicode de reemplazo que Telegram ya incluye en el
    texto original (todo custom_emoji envuelve un emoji unicode normal).
    """
    ids = [e.custom_emoji_id for e in entities if e.type == MessageEntity.CUSTOM_EMOJI and e.custom_emoji_id]
    if not ids:
        return {}

    result: dict[str, Optional[Image.Image]] = {i: None for i in ids}
    try:
        stickers = await bot.get_custom_emoji_stickers(ids)
    except TelegramError as exc:
        logger.debug("No pude pedir los emojis premium %s: %s", ids, exc)
        return result

    for sticker in stickers:
        if sticker.is_animated or sticker.is_video:
            continue  # ver docstring: no rasterizable sin rlottie/ffmpeg
        img = await _download_image(bot, sticker.file_id)
        if img:
            result[sticker.custom_emoji_id] = img
    return result


# --------------------------------------------------------------------- #
# Emojis unicode normales — se descarga la imagen real (estilo Twemoji)
# en vez de depender de que el servidor tenga una fuente de emoji a
# color instalada. Esto es lo que evita que nombres/mensajes con emoji
# se vean como "□□□" cuando el servidor no tiene esa fuente.
#
# Se cachean en memoria por el tiempo de vida del proceso (un emoji
# siempre se ve igual, no hace falta volver a pedirlo).
# --------------------------------------------------------------------- #
_UNICODE_EMOJI_CACHE: dict[str, Optional[Image.Image]] = {}
_VARIATION_SELECTORS = {0xFE0E, 0xFE0F}
_TWEMOJI_URL_TEMPLATES = [
    # jdecked/twemoji es el fork mantenido activamente (Twitter/twemoji
    # quedó archivado); se prueba primero y se cae al original como respaldo.
    "https://raw.githubusercontent.com/jdecked/twemoji/main/assets/72x72/{cp}.png",
    "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/{cp}.png",
]

_http_client_lock = asyncio.Lock()
_http_client: Optional[httpx.AsyncClient] = None


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    async with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.AsyncClient(timeout=6.0)
        return _http_client


def _cluster_codepoints(cluster: str) -> str:
    return "-".join(f"{ord(ch):x}" for ch in cluster if ord(ch) not in _VARIATION_SELECTORS)


async def fetch_unicode_emoji_image(cluster: str) -> Optional[Image.Image]:
    """Imagen (color) de un emoji unicode "normal" (no premium), identificado
    por su cluster de texto exacto (p. ej. "🐺" o "👨‍👩‍👧"). None si no se
    pudo descargar — en ese caso quote_sticker.py cae de nuevo a dibujar el
    carácter con la fuente de texto (puede verse como "tofu" si el servidor
    no tiene una fuente de emoji instalada)."""
    if cluster in _UNICODE_EMOJI_CACHE:
        return _UNICODE_EMOJI_CACHE[cluster]

    codepoints = _cluster_codepoints(cluster)
    if not codepoints:
        _UNICODE_EMOJI_CACHE[cluster] = None
        return None

    img: Optional[Image.Image] = None
    try:
        client = await _get_http_client()
        for template in _TWEMOJI_URL_TEMPLATES:
            url = template.format(cp=codepoints)
            try:
                resp = await client.get(url, follow_redirects=True)
                if resp.status_code == 200 and resp.content:
                    candidate = Image.open(io.BytesIO(resp.content))
                    candidate.load()
                    img = candidate.convert("RGBA")
                    break
            except (httpx.HTTPError, OSError):
                continue
    except Exception as exc:  # noqa: BLE001
        logger.debug("No pude descargar el emoji %s (%s): %s", cluster, codepoints, exc)

    _UNICODE_EMOJI_CACHE[cluster] = img
    return img


async def fetch_unicode_emoji_images(clusters: Iterable[str]) -> dict[str, Optional[Image.Image]]:
    """Descarga (en paralelo) las imágenes de todos los clusters de emoji
    únicos en `clusters`, usando la caché en memoria cuando ya se pidieron
    antes. Devuelve {cluster: imagen_o_None}."""
    unique = list(dict.fromkeys(clusters))
    pending = [c for c in unique if c not in _UNICODE_EMOJI_CACHE]
    if pending:
        await asyncio.gather(*(fetch_unicode_emoji_image(c) for c in pending))
    return {c: _UNICODE_EMOJI_CACHE.get(c) for c in unique}
