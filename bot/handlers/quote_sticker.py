"""
handlers/quote_sticker.py
Comando /q — convierte el mensaje respondido en una "tarjeta de cita"
(estilo QuotLyBot) y la envía como sticker (o como imagen, si se pide).

La tarjeta incluye:
  - La foto de perfil de la persona citada, como parte de la propia imagen
    del sticker (no es un adorno aparte: si el sticker se reenvía, la foto
    va con él). Si no se puede obtener la foto (privacidad, sin foto, etc.)
    se dibuja un avatar de respaldo con la inicial del nombre.
  - El nombre, en negrita y con su color de acento, respetando los emojis
    normales que tenga (☀️🔥❤️ etc.) y, si la persona tiene puesto un
    "emoji de estado" (los emojis animados premium que se muestran pegados
    al nombre en Telegram Premium), ese emoji también se dibuja al lado.
  - El texto del mensaje, con sus emojis normales Y sus emojis premium
    (custom_emoji) tal cual los escribió la persona.

Uso:
    /q                       -> cita el mensaje respondido como sticker
    /q red / blue / green / purple / orange / pink / black / white / gray
                              -> color del fondo de la tarjeta
    /q #cbafff                -> color personalizado (hex)
    /q random                 -> color aleatorio
    /q i / img / p / png      -> envía como imagen (foto) en vez de sticker
    /q r                      -> si el mensaje respondido a su vez respondía
                                  a otro mensaje, ese mensaje también se
                                  muestra (arriba, más chico) dentro de la
                                  misma tarjeta, igual que el ejemplo de
                                  cita anidada.
    /q 1 / /q 2 / /q 3 ...    -> además del mensaje respondido, muestra los
                                  N mensajes anteriores a él en el chat
                                  (el más viejo arriba, el respondido abajo).

Todo es combinable, ej: /q i red 2   -> imagen roja con 2 mensajes de contexto.

Requiere Pillow y pilmoji (ambos en requirements.txt) y, en el servidor,
alguna fuente TrueType con buena cobertura Unicode (DejaVu Sans, que casi
siempre viene preinstalada en Debian/Ubuntu vía el paquete
`fonts-dejavu-core`; si falta, instálala con `apt install -y fonts-dejavu-core`).
Los emojis normales se dibujan como imágenes a color (vía pilmoji/Twemoji),
así que el servidor necesita salida a internet la primera vez que se usa
cada emoji (después queda cacheado en memoria/disco por pilmoji).
"""
from __future__ import annotations

import io
import logging
import random
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from pilmoji import Pilmoji
from telegram import Message, MessageEntity, Update, User
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from utils.formatting import error
from utils.message_log import describe_media, get_previous

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
# Saneo de texto Unicode
# Ya NO se eliminan los emojis normales (antes se borraban por completo);
# ahora se dibujan con pilmoji, así que solo se limpian caracteres de
# control/formato y se limita el spam de tildes/combinantes.
# --------------------------------------------------------------------- #
_STRIP_CATEGORIES = {"Cc", "Cf", "Co", "Cs"}  # control, formato, uso privado, sustitutos


def _sanitize_text(text: str, *, max_combining: int = 2) -> str:
    normalized = unicodedata.normalize("NFC", text)  # NFC (no NFKC) para no romper secuencias de emoji

    cleaned: list[str] = []
    combining_run = 0
    for ch in normalized:
        category = unicodedata.category(ch)

        if category in _STRIP_CATEGORIES:
            continue

        if category in ("Mn", "Me"):
            combining_run += 1
            if combining_run > max_combining:
                continue
        else:
            combining_run = 0

        cleaned.append(ch)

    return "".join(cleaned).strip()


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
# Parseo de argumentos:
# /q [red|blue|...|#hex|random] [i|img|p|png] [r|reply] [N]
# --------------------------------------------------------------------- #
def _parse_command_args(args: list[str]) -> tuple[tuple[int, int, int], bool, bool, int]:
    """Devuelve (color_de_fondo, como_imagen, incluir_padre, cantidad_previos)."""
    bg = (28, 30, 34)
    as_image = False
    reply_mode = False
    count = 0

    for raw in args:
        token = raw.strip().lower()
        if token in ("i", "img", "p", "png", "image", "imagen"):
            as_image = True
        elif token in ("r", "reply", "responde", "respondido"):
            reply_mode = True
        elif token.isdigit():
            count = max(count, min(int(token), 10))  # tope de 10 para no romper el sticker
        elif token == "random":
            bg = tuple(random.randint(30, 200) for _ in range(3))  # type: ignore[assignment]
        elif token.startswith("#"):
            parsed = _parse_hex(token)
            if parsed:
                bg = parsed
        elif token in _NAMED_COLORS:
            bg = _NAMED_COLORS[token]

    return bg, as_image, reply_mode, count


# --------------------------------------------------------------------- #
# Emojis premium (custom_emoji): convertir los offsets UTF-16 que manda
# Telegram a índices de caracteres de Python, y luego ubicar esos mismos
# emojis dentro del texto ya saneado (por si el saneo corrió algún índice).
# --------------------------------------------------------------------- #
def _entity_char_spans_raw(text: str, entities: Optional[list[MessageEntity]]) -> list[tuple[int, int, str]]:
    if not entities:
        return []
    utf16 = text.encode("utf-16-le")
    spans: list[tuple[int, int, str]] = []
    for e in entities:
        if e.type != MessageEntity.CUSTOM_EMOJI or not e.custom_emoji_id:
            continue
        start_b, end_b = e.offset * 2, (e.offset + e.length) * 2
        try:
            start_c = len(utf16[:start_b].decode("utf-16-le"))
            end_c = len(utf16[:end_b].decode("utf-16-le"))
        except UnicodeDecodeError:
            continue
        spans.append((start_c, end_c, e.custom_emoji_id))
    spans.sort()
    return spans


def _map_custom_emoji_spans(raw_text: str, clean_text: str, entities: Optional[list[MessageEntity]]) -> list[tuple[int, int, str]]:
    """Ubica cada emoji premium (por su placeholder unicode) dentro del texto ya saneado."""
    mapped: list[tuple[int, int, str]] = []
    cursor = 0
    for start_c, end_c, cid in _entity_char_spans_raw(raw_text, entities):
        placeholder = raw_text[start_c:end_c]
        if not placeholder:
            continue
        idx = clean_text.find(placeholder, cursor)
        if idx == -1:
            idx = clean_text.find(placeholder)
        if idx == -1:
            continue
        mapped.append((idx, idx + len(placeholder), cid))
        cursor = idx + len(placeholder)
    return mapped


# --------------------------------------------------------------------- #
# Descarga de imágenes desde Telegram (avatar, emoji de estado, emojis
# premium dentro del texto). Todo con manejo de errores silencioso: si
# algo falla (privacidad, sin conexión, etc.) simplemente no se dibuja
# esa parte y la tarjeta se genera igual.
# --------------------------------------------------------------------- #
async def _download_file_as_image(bot, file_id: str) -> Optional[Image.Image]:
    try:
        tg_file = await bot.get_file(file_id)
        raw = await tg_file.download_as_bytearray()
        return Image.open(io.BytesIO(bytes(raw))).convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        logger.debug("No se pudo descargar el archivo %s: %s", file_id, exc)
        return None


async def _fetch_avatar_image(bot, user_id: int) -> Optional[Image.Image]:
    try:
        photos = await bot.get_user_profile_photos(user_id, limit=1)
        if not photos or not photos.photos:
            return None
        file_id = photos.photos[0][-1].file_id  # el tamaño más grande disponible
        return await _download_file_as_image(bot, file_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("No se pudo obtener el avatar de %s: %s", user_id, exc)
        return None


async def _download_sticker_image(bot, sticker) -> Optional[Image.Image]:
    file_id = None
    if getattr(sticker, "is_animated", False) or getattr(sticker, "is_video", False):
        if sticker.thumbnail:
            file_id = sticker.thumbnail.file_id
    else:
        file_id = sticker.file_id
    if not file_id:
        return None
    return await _download_file_as_image(bot, file_id)


async def _fetch_custom_emoji_image(bot, custom_emoji_id: str, cache: dict[str, Optional[Image.Image]]) -> Optional[Image.Image]:
    if custom_emoji_id in cache:
        return cache[custom_emoji_id]
    img = None
    try:
        stickers = await bot.get_custom_emoji_stickers([custom_emoji_id])
        if stickers:
            img = await _download_sticker_image(bot, stickers[0])
    except Exception as exc:  # noqa: BLE001
        logger.debug("No se pudo obtener el emoji premium %s: %s", custom_emoji_id, exc)
    cache[custom_emoji_id] = img
    return img


async def _fetch_emoji_status_image(bot, user_id: int, cache: dict[str, Optional[Image.Image]]) -> Optional[Image.Image]:
    try:
        chat = await bot.get_chat(user_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("No se pudo obtener el chat de %s: %s", user_id, exc)
        return None
    emoji_id = getattr(chat, "emoji_status_custom_emoji_id", None)
    if not emoji_id:
        return None
    return await _fetch_custom_emoji_image(bot, emoji_id, cache)


# --------------------------------------------------------------------- #
# Avatares circulares (foto real, o un avatar de respaldo con la inicial)
# --------------------------------------------------------------------- #
def _to_circle(img: Image.Image, size: int) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _fallback_avatar(name: str, seed: int, size: int) -> Image.Image:
    color = _accent_color_for(seed)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, size, size), fill=(*color, 255))
    letter = (name.strip()[:1] or "?").upper()
    font = _load_font(_BOLD_FONT_PATHS, int(size * 0.48))
    bbox = draw.textbbox((0, 0), letter, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), letter, font=font, fill=(255, 255, 255, 255))
    return img


# --------------------------------------------------------------------- #
# Word-wrap con truncado a una cantidad máxima de líneas. Además de las
# líneas, devuelve el offset (en caracteres, dentro del texto original)
# donde empieza cada línea, para poder ubicar ahí los emojis premium.
# --------------------------------------------------------------------- #
def _wrap_and_truncate(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int
) -> list[tuple[str, int]]:
    words = [(m.group(0), m.start()) for m in re.finditer(r"\S+", text)]
    lines: list[tuple[str, int]] = []
    current = ""
    current_start = 0
    for word, start in words:
        candidate = f"{current} {word}".strip() if current else word
        if draw.textlength(candidate, font=font) <= max_width:
            if not current:
                current_start = start
            current = candidate
        else:
            if current:
                lines.append((current, current_start))
            current = word
            current_start = start
            while draw.textlength(current, font=font) > max_width and len(current) > 1:
                current = current[:-1]
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append((current, current_start))

    lines = lines[:max_lines] or [("", 0)]
    total_shown = sum(len(l) for l, _ in lines)
    if total_shown < len(text.strip()) and lines:
        last, last_start = lines[-1]
        while draw.textlength(last + "…", font=font) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = (last.rstrip() + "…", last_start)
    return lines


def _safe_emoji_text(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill,
) -> None:
    """Dibuja texto con emojis a color vía pilmoji; si falla (sin red, etc.)
    cae a texto plano para no tumbar la generación del sticker."""
    try:
        with Pilmoji(img, draw=draw) as pilmoji:
            pilmoji.text(xy, text, fill=fill, font=font)
    except Exception as exc:  # noqa: BLE001
        logger.debug("pilmoji falló, dibujando texto plano: %s", exc)
        draw.text(xy, text, fill=fill, font=font)


# --------------------------------------------------------------------- #
# Dibuja una línea de texto ya wrapeada, sustituyendo los tramos que
# corresponden a un emoji premium por su imagen real, y usando pilmoji
# para que los emojis normales (unicode) del resto del texto también se
# vean a color en vez de como texto plano.
# --------------------------------------------------------------------- #
def _draw_rich_line(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    line: str,
    line_start: int,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    emoji_spans: list[tuple[int, int, str]],
    emoji_images: dict[str, Optional[Image.Image]],
) -> None:
    x, y = xy
    line_end = line_start + len(line)
    local_spans = sorted(
        (max(s, line_start) - line_start, min(e, line_end) - line_start, cid)
        for s, e, cid in emoji_spans
        if e > line_start and s < line_end
    )

    icon_size = int(font.size * 1.05)
    cursor = 0
    for start, end, cid in local_spans:
        if start > cursor:
            chunk = line[cursor:start]
            _safe_emoji_text(img, draw, (x, y), chunk, font, fill)
            x += draw.textlength(chunk, font=font)
        emoji_img = emoji_images.get(cid)
        if emoji_img:
            resized = emoji_img.resize((icon_size, icon_size), Image.LANCZOS)
            img.paste(resized, (int(x), int(y + (font.size - icon_size) // 2)), resized)
            x += icon_size + 2
        else:
            # No se pudo descargar el emoji premium: al menos no perdemos el texto.
            placeholder = line[start:end] or "❓"
            _safe_emoji_text(img, draw, (x, y), placeholder, font, fill)
            x += draw.textlength(placeholder, font=font)
        cursor = end

    if cursor < len(line):
        chunk = line[cursor:]
        _safe_emoji_text(img, draw, (x, y), chunk, font, fill)


# --------------------------------------------------------------------- #
# Filas de la tarjeta (cada una es un mensaje: el citado, o uno de contexto)
# --------------------------------------------------------------------- #
@dataclass(slots=True)
class _Row:
    name: str
    text: str
    seed: int                                  # para elegir el color de acento (barrita + nombre)
    user_id: Optional[int] = None              # para pedir avatar / emoji de estado (solo la fila principal)
    raw_text: str = ""                         # texto tal cual, sin sanear (para ubicar emojis premium)
    entities: list[MessageEntity] = field(default_factory=list)


def _row_from_message(message: Message) -> _Row:
    if message.from_user:
        name = message.from_user.first_name or message.from_user.username or "Usuario"
        seed = message.from_user.id
        user_id = message.from_user.id
    elif message.sender_chat:
        name = message.sender_chat.title or "Canal"
        seed = message.sender_chat.id
        user_id = None
    else:
        name, seed, user_id = "Usuario", 0, None

    raw_text = message.text or message.caption or ""
    entities = list(message.entities or message.caption_entities or [])
    text = raw_text.strip() or describe_media(message)
    return _Row(
        name=_sanitize_text(name) or "Usuario",
        text=_sanitize_text(text),
        seed=seed or hash(name),
        user_id=user_id,
        raw_text=raw_text,
        entities=entities,
    )


# --------------------------------------------------------------------- #
# Assets remotos (avatar, emoji de estado, emojis premium del texto) que
# hay que descargar ANTES de dibujar, porque dibujar es síncrono pero
# pedirle cosas a Telegram es asíncrono.
# --------------------------------------------------------------------- #
@dataclass(slots=True)
class _Assets:
    avatar: Optional[Image.Image] = None
    status: Optional[Image.Image] = None
    custom_emojis: dict[str, Optional[Image.Image]] = field(default_factory=dict)


async def _gather_assets(bot, main_row: _Row, rows: list[_Row]) -> _Assets:
    emoji_cache: dict[str, Optional[Image.Image]] = {}
    avatar = None
    status = None

    if main_row.user_id:
        avatar = await _fetch_avatar_image(bot, main_row.user_id)
        status = await _fetch_emoji_status_image(bot, main_row.user_id, emoji_cache)

    for row in rows:
        spans = _map_custom_emoji_spans(row.raw_text, row.text, row.entities)
        for _, _, cid in spans:
            if cid not in emoji_cache:
                await _fetch_custom_emoji_image(bot, cid, emoji_cache)

    return _Assets(avatar=avatar, status=status, custom_emojis=emoji_cache)


# --------------------------------------------------------------------- #
# Render principal — tarjeta al estilo "cita de Telegram": el avatar de
# la persona citada, como parte del propio sticker, a la izquierda; y a
# la derecha una tarjeta con su nombre (+ emoji de estado, si tiene) y su
# mensaje. Cuando hay varios mensajes (contexto de /q N o /q r), se
# apilan uno encima del otro dentro de la misma tarjeta, con los más
# viejos arriba (más chicos y tenues) y el mensaje citado al final, más
# grande.
# --------------------------------------------------------------------- #
_CANVAS_SIDE = 512
_AVATAR_SIZE = 108
_AVATAR_GAP = 16
_PADDING = 26
_TEXT_GAP = 0
_ROW_GAP = 18

_MAIN_NAME_SIZE = 30
_MAIN_TEXT_SIZE = 34
_CTX_NAME_SIZE = 22
_CTX_TEXT_SIZE = 25
_CTX_MAX_LINES = 2
_LINE_SPACING = 1.28
_STATUS_ICON_SIZE = 28


def _row_accent(row: _Row, bg: tuple[int, int, int]) -> tuple[int, int, int]:
    color = _accent_color_for(row.seed)
    if _text_is_light(bg):
        color = tuple(max(0, c - 60) for c in color)  # type: ignore[assignment]
    return color


def _render_quote_card(rows: list[_Row], bg: tuple[int, int, int], rounded: bool, assets: _Assets) -> Image.Image:
    if not rows:
        rows = [_Row(name="Usuario", text="", seed=0)]

    text_color_main = (30, 30, 32) if _text_is_light(bg) else (245, 245, 245)
    text_color_ctx = tuple((m + b) // 2 for m, b in zip(text_color_main, bg))

    body_font_main = _load_font(_REGULAR_FONT_PATHS, _MAIN_TEXT_SIZE)
    body_font_ctx = _load_font(_REGULAR_FONT_PATHS, _CTX_TEXT_SIZE)
    name_font_main = _load_font(_BOLD_FONT_PATHS, _MAIN_NAME_SIZE)
    name_font_ctx = _load_font(_BOLD_FONT_PATHS, _CTX_NAME_SIZE)

    bubble_x0 = _AVATAR_SIZE + _AVATAR_GAP
    bar_x = bubble_x0 + _PADDING
    text_left = bar_x
    max_text_width = _CANVAS_SIDE - text_left - _PADDING

    probe = Image.new("RGBA", (10, 10))
    probe_draw = ImageDraw.Draw(probe)

    line_h_ctx = int(_CTX_TEXT_SIZE * _LINE_SPACING)
    line_h_main = int(_MAIN_TEXT_SIZE * _LINE_SPACING)

    def ctx_block_h(n_lines: int) -> int:
        return _CTX_NAME_SIZE + 8 + n_lines * line_h_ctx

    def main_block_h(n_lines: int) -> int:
        extra = _STATUS_ICON_SIZE + 6 if assets.status else 0
        return max(_MAIN_NAME_SIZE, extra) + 10 + n_lines * line_h_main

    *context_rows, main_row = rows
    available_h = _CANVAS_SIDE - _PADDING * 2
    min_main_h = main_block_h(1)

    kept: list[tuple[_Row, list[tuple[str, int]]]] = []
    used_h = 0
    for row in reversed(context_rows):
        lines = _wrap_and_truncate(probe_draw, row.text.strip() or " ", body_font_ctx, max_text_width, _CTX_MAX_LINES)
        block_h = ctx_block_h(len(lines)) + _ROW_GAP
        if used_h + block_h + min_main_h > available_h:
            break
        kept.append((row, lines))
        used_h += block_h
    kept.reverse()

    remaining_for_main = available_h - used_h
    max_main_lines = max(1, (remaining_for_main - _MAIN_NAME_SIZE - 10) // line_h_main)
    main_lines = _wrap_and_truncate(probe_draw, main_row.text.strip() or " ", body_font_main, max_text_width, max_main_lines)

    content_h = used_h + main_block_h(len(main_lines))
    canvas_h = min(_CANVAS_SIDE, max(_PADDING * 2 + content_h, _AVATAR_SIZE + _PADDING))

    img = Image.new("RGBA", (_CANVAS_SIDE, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if rounded:
        draw.rounded_rectangle((bubble_x0, 0, _CANVAS_SIDE - 1, canvas_h - 1), radius=40, fill=(*bg, 255))
    else:
        draw.rectangle((bubble_x0, 0, _CANVAS_SIDE - 1, canvas_h - 1), fill=(*bg, 255))

    y = _PADDING
    for row, lines in kept:
        accent = _row_accent(row, bg)
        block_h = ctx_block_h(len(lines))
        draw.rounded_rectangle(
            (bar_x - _TEXT_GAP - 5, y, bar_x - _TEXT_GAP, y + block_h), radius=3, fill=(*accent, 220)
        )
        _safe_emoji_text(img, draw, (text_left, y - 2), row.name, name_font_ctx, (*accent, 220))
        ty = y + _CTX_NAME_SIZE + 6
        spans = _map_custom_emoji_spans(row.raw_text, row.text, row.entities)
        for line, line_start in lines:
            _draw_rich_line(
                img, draw, (text_left, ty), line, line_start, body_font_ctx, text_color_ctx, spans, assets.custom_emojis
            )
            ty += line_h_ctx
        y += block_h + _ROW_GAP

    accent = _row_accent(main_row, bg)
    block_h = main_block_h(len(main_lines))
    draw.rounded_rectangle((bar_x - _TEXT_GAP - 5, y, bar_x - _TEXT_GAP, y + block_h), radius=3, fill=(*accent, 255))

    name_x = text_left
    _safe_emoji_text(img, draw, (name_x, y - 4), main_row.name, name_font_main, accent)
    if assets.status:
        name_w = probe_draw.textlength(main_row.name + " ", font=name_font_main)
        icon = assets.status.resize((_STATUS_ICON_SIZE, _STATUS_ICON_SIZE), Image.LANCZOS)
        img.paste(icon, (int(name_x + name_w), int(y - 4 + (_MAIN_NAME_SIZE - _STATUS_ICON_SIZE) // 2)), icon)

    ty = y + _MAIN_NAME_SIZE + 10
    main_spans = _map_custom_emoji_spans(main_row.raw_text, main_row.text, main_row.entities)
    for line, line_start in main_lines:
        _draw_rich_line(
            img, draw, (text_left, ty), line, line_start, body_font_main, text_color_main, main_spans, assets.custom_emojis
        )
        ty += line_h_main

    # Avatar de la persona citada: sobre la propia imagen del sticker, a la
    # izquierda, superpuesto un poco a la tarjeta para que se vea como una
    # sola pieza (igual que las tarjetas de ejemplo).
    avatar_cy = min(max(canvas_h - _AVATAR_SIZE // 2 - 10, _AVATAR_SIZE // 2), canvas_h - _AVATAR_SIZE // 2) if canvas_h > _AVATAR_SIZE else canvas_h // 2
    avatar_top = int(avatar_cy - _AVATAR_SIZE / 2)
    if assets.avatar:
        avatar_img = _to_circle(assets.avatar, _AVATAR_SIZE)
    else:
        avatar_img = _fallback_avatar(main_row.name, main_row.seed, _AVATAR_SIZE)
    img.paste(avatar_img, (0, max(avatar_top, 0)), avatar_img)

    return img


def _to_bytes(img: Image.Image, as_webp: bool) -> io.BytesIO:
    buf = io.BytesIO()
    if as_webp:
        img.save(buf, format="WEBP", lossless=True)
    else:
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

    main_row = _row_from_message(target)
    if not main_row.text:
        await message.reply_text(
            error("Ese mensaje no tiene texto para citar (son puros caracteres/emojis que no puedo dibujar).")
        )
        return

    bg, as_image, reply_mode, count = _parse_command_args(context.args or [])

    rows: list[_Row] = []
    # /q r: si el mensaje que estamos citando a su vez respondía a otro
    # mensaje, ese mensaje "padre" se agrega arriba, dentro de la misma
    # tarjeta (más chico), tal como el mensaje respondido dentro de otro
    # mensaje en el chat original.
    if reply_mode and target.reply_to_message:
        parent_row = _row_from_message(target.reply_to_message)
        if parent_row.text:
            rows.append(parent_row)
    elif count > 0:
        stubs = get_previous(chat.id, target.message_id, count)
        for stub in stubs:
            text = (stub.text or "").strip()
            if text:
                rows.append(_Row(name=_sanitize_text(stub.name) or "Usuario", text=_sanitize_text(text), seed=stub.user_id))
    rows.append(main_row)

    try:
        await context.bot.send_chat_action(chat.id, "upload_photo" if as_image else "choose_sticker")
    except Exception:  # noqa: BLE001
        pass

    assets = await _gather_assets(context.bot, main_row, rows)

    try:
        card = _render_quote_card(rows, bg, rounded=not as_image, assets=assets)
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
