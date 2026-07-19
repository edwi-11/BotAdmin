"""
handlers/quote_sticker.py
Comando /q — convierte el mensaje respondido en una "tarjeta de cita"
(estilo QuotLyBot) y la envía como sticker (o como imagen, si se pide).

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
                                  misma tarjeta.
    /q 1 / /q 2 / /q 3 ...    -> además del mensaje respondido, muestra los
                                  N mensajes anteriores a él en el chat
                                  (el más viejo arriba, el respondido abajo).

Todo es combinable, ej: /q i red 2   -> imagen roja con 2 mensajes de contexto.

Requiere el paquete Pillow (ya agregado a requirements.txt) y, en el
servidor, alguna fuente TrueType con buena cobertura Unicode (DejaVu Sans,
que casi siempre viene preinstalada en Debian/Ubuntu vía el paquete
`fonts-dejavu-core`; si falta, instálala con `apt install -y fonts-dejavu-core`).
"""
from __future__ import annotations

import io
import logging
import random
import unicodedata
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from telegram import Message, Update
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
# --------------------------------------------------------------------- #
_STRIP_CATEGORIES = {"Cc", "Cf", "Co", "Cs"}  # control, formato, uso privado, sustitutos

_EMOJI_RANGES: list[tuple[int, int]] = [
    (0x1F000, 0x1FFFF),  # emojis, pictogramas, banderas, cartas, etc.
    (0x2600, 0x27BF),    # símbolos diversos / dingbats (☀ ✔ etc.)
    (0x2B00, 0x2BFF),    # flechas y símbolos diversos (⭐ ⬛ etc.)
    (0xFE00, 0xFE0F),    # selectores de variación de emoji
]


def _is_emoji_codepoint(code: int) -> bool:
    return any(start <= code <= end for start, end in _EMOJI_RANGES)


def _sanitize_text(text: str, *, max_combining: int = 2) -> str:
    normalized = unicodedata.normalize("NFKC", text)

    cleaned: list[str] = []
    combining_run = 0
    for ch in normalized:
        category = unicodedata.category(ch)
        code = ord(ch)

        if category in _STRIP_CATEGORIES or _is_emoji_codepoint(code):
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
# Word-wrap con truncado a una cantidad máxima de líneas
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
            while draw.textlength(current, font=font) > max_width and len(current) > 1:
                current = current[:-1]
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)

    lines = lines[:max_lines] or [""]
    if len(" ".join(lines)) < len(text.strip()) and lines:
        last = lines[-1]
        while draw.textlength(last + "…", font=font) > max_width and len(last) > 1:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


# --------------------------------------------------------------------- #
# Filas de la tarjeta (cada una es un mensaje: el citado, o uno de contexto)
# --------------------------------------------------------------------- #
@dataclass(slots=True)
class _Row:
    name: str
    text: str
    seed: int          # para elegir el color de acento (barrita + nombre)


def _row_from_message(message: Message) -> _Row:
    if message.from_user:
        name = message.from_user.first_name or message.from_user.username or "Usuario"
        seed = message.from_user.id
    elif message.sender_chat:
        name = message.sender_chat.title or "Canal"
        seed = message.sender_chat.id
    else:
        name, seed = "Usuario", 0

    text = (message.text or message.caption or "").strip() or describe_media(message)
    return _Row(name=_sanitize_text(name) or "Usuario", text=_sanitize_text(text), seed=seed or hash(name))


# --------------------------------------------------------------------- #
# Render principal — tarjeta al estilo "cita de Telegram": una barrita de
# color a la izquierda, el nombre en negrita encima y el texto debajo.
# Cuando hay varios mensajes (contexto de /q N o /q r), se apilan uno
# encima del otro dentro de la misma tarjeta, con los más viejos arriba
# (más chicos y tenues) y el mensaje citado al final, más grande.
# --------------------------------------------------------------------- #
_CANVAS_SIDE = 512
_PADDING = 30
_BAR_WIDTH = 5
_BAR_RADIUS = 3
_TEXT_GAP = 16
_ROW_GAP = 18

_MAIN_NAME_SIZE = 30
_MAIN_TEXT_SIZE = 34
_CTX_NAME_SIZE = 22
_CTX_TEXT_SIZE = 25
_CTX_MAX_LINES = 2
_LINE_SPACING = 1.28


def _row_accent(row: _Row, bg: tuple[int, int, int]) -> tuple[int, int, int]:
    color = _accent_color_for(row.seed)
    if _text_is_light(bg):
        color = tuple(max(0, c - 60) for c in color)  # type: ignore[assignment]
    return color


def _render_quote_card(rows: list[_Row], bg: tuple[int, int, int], rounded: bool) -> Image.Image:
    if not rows:
        rows = [_Row(name="Usuario", text="", seed=0)]

    text_color_main = (30, 30, 32) if _text_is_light(bg) else (245, 245, 245)
    text_color_ctx = tuple((m + b) // 2 for m, b in zip(text_color_main, bg))

    body_font_main = _load_font(_REGULAR_FONT_PATHS, _MAIN_TEXT_SIZE)
    body_font_ctx = _load_font(_REGULAR_FONT_PATHS, _CTX_TEXT_SIZE)
    name_font_main = _load_font(_BOLD_FONT_PATHS, _MAIN_NAME_SIZE)
    name_font_ctx = _load_font(_BOLD_FONT_PATHS, _CTX_NAME_SIZE)

    text_left = _PADDING + _BAR_WIDTH + _TEXT_GAP
    max_text_width = _CANVAS_SIDE - text_left - _PADDING

    probe = Image.new("RGBA", (10, 10))
    probe_draw = ImageDraw.Draw(probe)

    line_h_ctx = int(_CTX_TEXT_SIZE * _LINE_SPACING)
    line_h_main = int(_MAIN_TEXT_SIZE * _LINE_SPACING)

    def ctx_block_h(n_lines: int) -> int:
        return _CTX_NAME_SIZE + 8 + n_lines * line_h_ctx

    def main_block_h(n_lines: int) -> int:
        return _MAIN_NAME_SIZE + 10 + n_lines * line_h_main

    *context_rows, main_row = rows
    available_h = _CANVAS_SIDE - _PADDING * 2
    min_main_h = main_block_h(1)

    kept: list[tuple[_Row, list[str]]] = []
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
    canvas_h = min(_CANVAS_SIDE, _PADDING * 2 + content_h)

    img = Image.new("RGBA", (_CANVAS_SIDE, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if rounded:
        draw.rounded_rectangle((0, 0, _CANVAS_SIDE - 1, canvas_h - 1), radius=40, fill=(*bg, 255))
    else:
        draw.rectangle((0, 0, _CANVAS_SIDE - 1, canvas_h - 1), fill=(*bg, 255))

    y = _PADDING
    for row, lines in kept:
        accent = _row_accent(row, bg)
        block_h = ctx_block_h(len(lines))
        draw.rounded_rectangle(
            (_PADDING, y, _PADDING + _BAR_WIDTH, y + block_h), radius=_BAR_RADIUS, fill=(*accent, 220)
        )
        draw.text((text_left, y - 2), row.name, font=name_font_ctx, fill=(*accent, 220))
        ty = y + _CTX_NAME_SIZE + 6
        for line in lines:
            draw.text((text_left, ty), line, font=body_font_ctx, fill=text_color_ctx)
            ty += line_h_ctx
        y += block_h + _ROW_GAP

    accent = _row_accent(main_row, bg)
    block_h = main_block_h(len(main_lines))
    draw.rounded_rectangle((_PADDING, y, _PADDING + _BAR_WIDTH, y + block_h), radius=_BAR_RADIUS, fill=(*accent, 255))
    draw.text((text_left, y - 4), main_row.name, font=name_font_main, fill=accent)
    ty = y + _MAIN_NAME_SIZE + 10
    for line in main_lines:
        draw.text((text_left, ty), line, font=body_font_main, fill=text_color_main)
        ty += line_h_main

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

    try:
        card = _render_quote_card(rows, bg, rounded=not as_image)
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
