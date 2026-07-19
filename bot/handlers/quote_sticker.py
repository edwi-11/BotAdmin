"""
handlers/quote_sticker.py
Comando /q — convierte el mensaje respondido en una "tarjeta de cita"
(estilo QuotLyBot) y la envía como sticker (o como imagen, si se pide).

Uso:
    /q                    -> cita el mensaje respondido como sticker
    /q red / blue / green / purple / orange / pink / black / white / gray
                           -> color del fondo de la tarjeta
    /q #cbafff             -> color personalizado (hex)
    /q random               -> color aleatorio
    /q i / img / p / png     -> envía como imagen (foto) en vez de sticker

Combinable, ej: /q i red   -> imagen roja.

NO incluido en esta versión (a diferencia de QuotLyBot original): citar
varios mensajes a la vez (/q 3), incrustar fotos/videos del mensaje citado,
mostrar el mensaje al que respondía el citado, calificar citas, /qtop,
guardar en un pack de stickers propio, ni panel /qsettings. Se puede
agregar después si hace falta.

Requiere el paquete Pillow (ya agregado a requirements.txt) y, en el
servidor, alguna fuente TrueType con buena cobertura Unicode (DejaVu Sans,
que casi siempre viene preinstalada en Debian/Ubuntu vía el paquete
`fonts-dejavu-core`; si falta, instálala con `apt install -y fonts-dejavu-core`).
"""
from __future__ import annotations

import io
import logging
import random
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from utils.formatting import error

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Fuentes (con lista de rutas alternativas por si el servidor no tiene
# exactamente las mismas instaladas)
# --------------------------------------------------------------------- #
_REGULAR_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
_BOLD_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _load_font(paths: list[str], size: int) -> ImageFont.FreeTypeFont:
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    logger.warning("No se encontró ninguna fuente TrueType, usando la fuente por defecto de Pillow (se verá fea).")
    return ImageFont.load_default()


# --------------------------------------------------------------------- #
# Colores
# --------------------------------------------------------------------- #
_NAMED_COLORS: dict[str, tuple[int, int, int]] = {
    "red": (168, 39, 40),
    "blue": (34, 66, 122),
    "green": (36, 97, 62),
    "purple": (78, 52, 122),
    "orange": (163, 90, 24),
    "pink": (150, 45, 92),
    "black": (24, 24, 26),
    "white": (235, 235, 235),
    "gray": (60, 62, 66),
    "grey": (60, 62, 66),
}

# Paleta de "colores de acento" al estilo Telegram, para el nombre del autor
# y el avatar cuando no tiene foto de perfil.
_ACCENT_COLORS = [
    (230, 126, 118), (250, 168, 121), (167, 151, 227),
    (124, 199, 128), (111, 202, 204), (102, 169, 224), (238, 123, 175),
]


def _accent_color_for(seed: int) -> tuple[int, int, int]:
    return _ACCENT_COLORS[seed % len(_ACCENT_COLORS)]


def _parse_hex(text: str) -> Optional[tuple[int, int, int]]:
    text = text.strip().lstrip("#")
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


def _text_is_light(rgb: tuple[int, int, int]) -> bool:
    r, g, b = rgb
    luminance = 0.299 * r + 0.587 * g + 0.114 * b
    return luminance > 150


# --------------------------------------------------------------------- #
# Parseo de argumentos: /q [red|blue|...|#hex|random] [i|img|p|png]
# --------------------------------------------------------------------- #
def _parse_args(args: list[str]) -> tuple[tuple[int, int, int], bool]:
    """Devuelve (color_de_fondo, como_imagen)."""
    bg = (28, 30, 34)  # oscuro por defecto, al estilo QuotLy clásico
    as_image = False

    for raw in args:
        token = raw.strip().lower()
        if token in ("i", "img", "p", "png", "image", "imagen"):
            as_image = True
        elif token == "random":
            bg = tuple(random.randint(30, 200) for _ in range(3))  # type: ignore[assignment]
        elif token.startswith("#"):
            parsed = _parse_hex(token)
            if parsed:
                bg = parsed
        elif token in _NAMED_COLORS:
            bg = _NAMED_COLORS[token]
        # cualquier otro flag (m, media, c, crop, r, reply, rate, sN.N, etc.)
        # se ignora silenciosamente en esta versión.

    return bg, as_image


# --------------------------------------------------------------------- #
# Avatar
# --------------------------------------------------------------------- #
def _initials_avatar(name: str, size: int, color: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size, size), fill=(*color, 255))
    initial = (name.strip()[:1] or "?").upper()
    font = _load_font(_BOLD_FONT_PATHS, int(size * 0.5))
    bbox = draw.textbbox((0, 0), initial, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    text_color = (30, 30, 30) if _text_is_light(color) else (255, 255, 255)
    draw.text(((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]), initial, font=font, fill=text_color)
    return img


def _circle_crop(img: Image.Image, size: int) -> Image.Image:
    img = img.convert("RGBA")
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


async def _fetch_avatar(context: ContextTypes.DEFAULT_TYPE, user_id: int, name: str, size: int) -> Image.Image:
    accent = _accent_color_for(user_id)
    try:
        photos = await context.bot.get_user_profile_photos(user_id, limit=1)
        if photos.total_count > 0:
            file_id = photos.photos[0][-1].file_id
            tg_file = await context.bot.get_file(file_id)
            buf = io.BytesIO()
            await tg_file.download_to_memory(buf)
            buf.seek(0)
            with Image.open(buf) as raw:
                return _circle_crop(raw, size)
    except TelegramError as exc:
        logger.info("No se pudo obtener la foto de perfil de %s: %s", user_id, exc)
    except Exception as exc:  # noqa: BLE001
        logger.info("Error inesperado obteniendo avatar de %s: %s", user_id, exc)
    return _initials_avatar(name, size, accent)


# --------------------------------------------------------------------- #
# Word-wrap con truncado a una altura máxima (para respetar el límite de
# 512px de los stickers estáticos de Telegram)
# --------------------------------------------------------------------- #
def _wrap_and_truncate(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
            # Palabra sola más ancha que el máximo: se corta a la fuerza.
            while draw.textlength(current, font=font) > max_width and len(current) > 1:
                current = current[:-1]
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)

    truncated = len(lines) < len(words) or len(lines) > max_lines  # aproximación
    lines = lines[:max_lines]
    if len(" ".join(lines)) < len(text.strip()) and lines:
        last = lines[-1]
        while draw.textlength(last + "…", font=font) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines or [""]


# --------------------------------------------------------------------- #
# Render principal
# --------------------------------------------------------------------- #
_CANVAS_SIDE = 512
_PADDING = 34
_AVATAR_SIZE = 84
_NAME_SIZE = 30
_TEXT_SIZE = 34
_LINE_SPACING = 1.28


def _render_quote(name: str, text: str, avatar: Image.Image, bg: tuple[int, int, int], rounded: bool) -> Image.Image:
    text_color = (30, 30, 32) if _text_is_light(bg) else (245, 245, 245)
    name_color = _accent_color_for(sum(name.encode("utf-8")))
    if _text_is_light(bg):
        # Sobre fondos claros, un acento oscurecido se lee mejor que el
        # pastel original.
        name_color = tuple(max(0, c - 60) for c in name_color)  # type: ignore[assignment]

    body_font = _load_font(_REGULAR_FONT_PATHS, _TEXT_SIZE)
    name_font = _load_font(_BOLD_FONT_PATHS, _NAME_SIZE)

    header_h = _AVATAR_SIZE
    text_left = _PADDING * 2 + _AVATAR_SIZE
    max_text_width = _CANVAS_SIDE - text_left - _PADDING
    line_h = int(_TEXT_SIZE * _LINE_SPACING)

    probe = Image.new("RGBA", (10, 10))
    probe_draw = ImageDraw.Draw(probe)
    available_h = _CANVAS_SIDE - _PADDING * 2 - header_h - 14
    max_lines = max(1, available_h // line_h)
    lines = _wrap_and_truncate(probe_draw, text.strip() or " ", body_font, max_text_width, max_lines)

    content_h = header_h + 14 + len(lines) * line_h
    canvas_h = min(_CANVAS_SIDE, _PADDING * 2 + content_h)

    img = Image.new("RGBA", (_CANVAS_SIDE, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if rounded:
        draw.rounded_rectangle((0, 0, _CANVAS_SIDE - 1, canvas_h - 1), radius=40, fill=(*bg, 255))
    else:
        draw.rectangle((0, 0, _CANVAS_SIDE - 1, canvas_h - 1), fill=(*bg, 255))

    img.paste(avatar, (_PADDING, _PADDING), avatar)

    draw.text((text_left, _PADDING - 4), name, font=name_font, fill=name_color)

    y = _PADDING + _NAME_SIZE + 12
    for line in lines:
        draw.text((text_left, y), line, font=body_font, fill=text_color)
        y += line_h

    return img


def _to_bytes(img: Image.Image, as_webp: bool) -> io.BytesIO:
    buf = io.BytesIO()
    if as_webp:
        img.save(buf, format="WEBP", lossless=True)
    else:
        # Para foto/imagen, aplanamos sobre el mismo color de fondo (sin
        # transparencia) para evitar que Telegram lo muestre con fondo
        # negro por defecto si lo comprime a JPEG.
        flattened = Image.new("RGB", img.size, (255, 255, 255))
        flattened.paste(img, (0, 0), img)
        flattened.save(buf, format="PNG")
    buf.seek(0)
    return buf


# --------------------------------------------------------------------- #
# Comando /q
# --------------------------------------------------------------------- #
async def q_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat

    target = message.reply_to_message
    if not target:
        await message.reply_text(error("Responde a un mensaje con /q para citarlo."))
        return

    text = (target.text or target.caption or "").strip()
    if not text:
        await message.reply_text(
            error("Ese mensaje no tiene texto para citar (por ahora /q no incluye fotos/videos sin texto).")
        )
        return
    if len(text) > 1200:
        text = text[:1200]  # protección extra; _wrap_and_truncate ya recorta visualmente

    if target.from_user:
        name = target.from_user.first_name or (target.from_user.username or "Usuario")
        user_id = target.from_user.id
    elif target.sender_chat:
        name = target.sender_chat.title or "Canal"
        user_id = target.sender_chat.id
    else:
        name = "Usuario"
        user_id = 0

    bg, as_image = _parse_args(context.args or [])

    try:
        await context.bot.send_chat_action(chat.id, "upload_photo" if as_image else "choose_sticker")
    except Exception:  # noqa: BLE001
        pass

    avatar = await _fetch_avatar(context, user_id, name, _AVATAR_SIZE) if user_id else \
        _initials_avatar(name, _AVATAR_SIZE, _accent_color_for(hash(name)))

    try:
        card = _render_quote(name, text, avatar, bg, rounded=not as_image)
        buf = _to_bytes(card, as_webp=not as_image)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error generando la tarjeta de cita: %s", exc)
        await message.reply_text(error("No pude generar la cita, intenta de nuevo."))
        return

    try:
        if as_image:
            await message.reply_photo(buf, reply_to_message_id=target.message_id)
        else:
            await message.reply_sticker(buf, reply_to_message_id=target.message_id)
    except TelegramError as exc:
        logger.warning("Error enviando la cita: %s", exc)
        await message.reply_text(error(f"No pude enviar la cita: {exc}"))
