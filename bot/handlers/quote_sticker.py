"""
handlers/quote_sticker.py
Comando /q — convierte el mensaje respondido en una "tarjeta de cita"
al estilo Telegram y la envía como sticker (o como imagen, si se pide).

Uso:
    /q                         -> cita el mensaje respondido como sticker
    /q red / blue / green / purple / orange / pink / black / white / gray
                                -> color del fondo de la tarjeta
    /q #cbafff                  -> color personalizado (hex)
    /q random                   -> color aleatorio
    /q i / img / p / png        -> envía como imagen (foto) en vez de sticker
    /q r                        -> si el mensaje respondido a su vez respondía
                                    a otro mensaje, ese mensaje se muestra
                                    arriba, en una tarjeta de respuesta anidada
                                    (igual que la burbuja de "responder" de
                                    Telegram).
    /q 1 / /q 2 / /q 3 ...      -> además del mensaje respondido, muestra los
                                    N mensajes anteriores a él en el chat, en
                                    orden cronológico (conversación).

Todo es combinable: /q r 2, /q 4 r, /q i red 2, etc.

Qué se dibuja además del texto (según el tipo de mensaje citado):
    foto, sticker, video, GIF, nota de video -> miniatura
    documento, audio, nota de voz            -> tarjeta con ícono
    ubicación / venue                        -> tarjeta con coordenadas
    encuesta                                 -> tarjeta con la pregunta
    contacto                                 -> tarjeta con nombre/teléfono
    reenviado                                -> insignia "↪ Reenviado"
    emojis premium (custom_emoji)            -> se descargan y se dibujan
                                                 tal cual (si son estáticos)

Limitaciones que vienen de la Bot API de Telegram y no se pueden evitar:
    - Stickers/GIFs o emojis premium ANIMADOS (.tgs / Lottie) no se pueden
      rasterizar sin la librería `rlottie`; se usa la miniatura estática
      que Telegram manda, o si no hay, se cae al emoji unicode original.
    - Stickers/GIFs en VIDEO (.webm) necesitarían `ffmpeg`; mismo fallback.
    - La ubicación se muestra como tarjeta con coordenadas, no como mapa
      real (necesitaría una API de mapas con su propia clave).

Requiere Pillow (ya en requirements.txt). Para emojis a color de verdad
hace falta una fuente de emoji-color instalada en el servidor, por ejemplo:
    apt install -y fonts-noto-color-emoji fonts-dejavu-core
Sin esa fuente, los emojis se siguen mostrando (con la fuente normal, en
blanco y negro / contorno) en vez de desaparecer.
"""
from __future__ import annotations

import io
import logging
import random
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw, ImageFont
from telegram import Message, MessageEntity, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from utils.formatting import error
from utils.message_log import describe_media, get_previous
from utils.telegram_media import (
    MediaThumb,
    fetch_avatar,
    fetch_custom_emoji_images,
    fetch_message_media,
    fit_within,
    rounded_crop,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Fuentes
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
_ITALIC_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Italic.ttf",
]
# Fuente de emoji A COLOR (opcional). Si el servidor no la tiene instalada,
# los emojis se dibujan con la fuente normal (se ven como contornos/"tofu").
_COLOR_EMOJI_FONT_PATHS = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf",
]

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _load_font(paths: list[str], size: int) -> ImageFont.FreeTypeFont:
    key = (paths[0] if paths else "default", size)
    if key in _font_cache:
        return _font_cache[key]
    for path in paths:
        try:
            font = ImageFont.truetype(path, size)
            _font_cache[key] = font
            return font
        except OSError:
            continue
    logger.warning("No se encontró ninguna fuente TrueType en %s, usando la de Pillow por defecto.", paths)
    font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _load_color_emoji_font() -> Optional[ImageFont.FreeTypeFont]:
    key = ("__color_emoji__", 0)
    if key in _font_cache:
        cached = _font_cache[key]
        return cached if cached is not None else None
    for path in _COLOR_EMOJI_FONT_PATHS:
        try:
            # Las fuentes de emoji a color suelen venir con un solo tamaño de
            # "strike" embebido (típicamente 109px); Pillow igual la puede
            # usar con draw.text(..., embedded_color=True).
            font = ImageFont.truetype(path, 109)
            _font_cache[key] = font
            return font
        except OSError:
            continue
    _font_cache[key] = None
    return None


# --------------------------------------------------------------------- #
# Saneo de texto — a diferencia de la versión anterior, ahora se preserva
# el texto tal cual llega (mayúsculas/minúsculas, tipografías Unicode,
# espacios invisibles, emojis, RTL, CJK...). Solo se descartan caracteres
# que directamente romperían el render: control C0 (salvo salto de línea)
# y sustitutos UTF-16 sueltos (inválidos por sí solos en un str de Python).
# --------------------------------------------------------------------- #
def _sanitize_text(text: str) -> str:
    cleaned = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cs":
            continue
        if cat == "Cc" and ch not in ("\n", "\t"):
            continue
        cleaned.append(ch)
    return "".join(cleaned).strip("\n").strip()


# --------------------------------------------------------------------- #
# Detección de "runs" de emoji dentro de una línea, para poder elegir
# fuente de emoji (a color, si hay) en vez de la fuente normal, sin romper
# secuencias compuestas (ZWJ, banderas, tonos de piel).
# --------------------------------------------------------------------- #
_EMOJI_RANGES: list[tuple[int, int]] = [
    (0x1F000, 0x1FFFF),
    (0x2600, 0x27BF),
    (0x2190, 0x21FF),
    (0x2B00, 0x2BFF),
    (0x2300, 0x23FF),
    (0xFE00, 0xFE0F),
]
_JOINERS = {0x200D}  # ZWJ


def _is_emoji_codepoint(code: int) -> bool:
    return code in _JOINERS or any(start <= code <= end for start, end in _EMOJI_RANGES)


def _split_runs(text: str) -> list[tuple[str, bool]]:
    """Divide `text` en tramos alternados (texto, es_emoji)."""
    if not text:
        return []
    runs: list[tuple[str, bool]] = []
    current = text[0]
    current_is_emoji = _is_emoji_codepoint(ord(text[0]))
    for ch in text[1:]:
        is_emoji = _is_emoji_codepoint(ord(ch))
        if is_emoji == current_is_emoji:
            current += ch
        else:
            runs.append((current, current_is_emoji))
            current = ch
            current_is_emoji = is_emoji
    runs.append((current, current_is_emoji))
    return runs


# --------------------------------------------------------------------- #
# Offsets UTF-16 (los que usa Telegram) <-> índices de str de Python
# --------------------------------------------------------------------- #
def _utf16_unit_len(ch: str) -> int:
    return 2 if ord(ch) > 0xFFFF else 1


def _utf16_offset_to_py_index(text: str, utf16_offset: int) -> int:
    count = 0
    for i, ch in enumerate(text):
        if count >= utf16_offset:
            return i
        count += _utf16_unit_len(ch)
    return len(text)


def _custom_emoji_spans(text: str, entities: Optional[list[MessageEntity]]) -> list[tuple[int, int, str]]:
    """[(inicio_py, fin_py, custom_emoji_id), ...] ordenado, sobre `text`."""
    if not entities:
        return []
    spans = []
    for e in entities:
        if e.type != MessageEntity.CUSTOM_EMOJI or not e.custom_emoji_id:
            continue
        start = _utf16_offset_to_py_index(text, e.offset)
        end = _utf16_offset_to_py_index(text, e.offset + e.length)
        if start < end:
            spans.append((start, end, e.custom_emoji_id))
    spans.sort(key=lambda s: s[0])
    return spans


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
    words = text.split(" ")
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


def _wrap_preserving_emoji_spans(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int
) -> list[tuple[str, int]]:
    """Como _wrap_and_truncate, pero además devuelve, por línea, el índice
    (en `text`) donde empieza esa línea — necesario para poder ubicar los
    emojis premium (que se referencian por offset en el texto original)."""
    lines = _wrap_and_truncate(draw, text, font, max_width, max_lines)
    out: list[tuple[str, int]] = []
    search_from = 0
    for line in lines:
        probe = line.rstrip("…").rstrip()
        idx = text.find(probe, search_from) if probe else search_from
        if idx == -1:
            idx = search_from
        out.append((line, idx))
        search_from = idx + len(probe)
    return out


# --------------------------------------------------------------------- #
# Dibuja una línea de texto, sustituyendo emojis premium por su imagen
# real (si se pudo descargar) y usando la fuente de emoji a color (si hay)
# para el resto de los emojis unicode normales.
# --------------------------------------------------------------------- #
def _draw_rich_line(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    line: str,
    line_start_idx: int,
    font: ImageFont.FreeTypeFont,
    emoji_font: Optional[ImageFont.FreeTypeFont],
    fill: tuple[int, int, int],
    emoji_spans: list[tuple[int, int, str]],
    emoji_images: dict[str, Optional[Image.Image]],
    canvas: Image.Image,
) -> None:
    x, y = xy
    font_size = font.size
    pos = 0  # índice dentro de `line`
    while pos < len(line):
        abs_idx = line_start_idx + pos

        # ¿Este punto cae dentro de un emoji premium con imagen descargada?
        custom = next(
            (s for s in emoji_spans if s[0] <= abs_idx < s[1] and emoji_images.get(s[2]) is not None), None
        )
        if custom:
            _, span_end, emoji_id = custom
            span_len_in_line = min(span_end, line_start_idx + len(line)) - abs_idx
            img = emoji_images[emoji_id]
            side = font_size
            paste = img.convert("RGBA").resize((side, side), Image.LANCZOS)
            canvas.paste(paste, (int(x), int(y)), paste)
            x += side
            pos += span_len_in_line
            continue

        # Si no, dibujamos el siguiente "run" (texto normal o emoji unicode)
        rest = line[pos:]
        run_text, run_is_emoji = _split_runs(rest)[0]
        # No cruzar hacia un emoji premium ya cubierto: recortamos el run
        # si un span premium empieza en el medio.
        for s in emoji_spans:
            if s[0] > abs_idx and s[0] < abs_idx + len(run_text) and emoji_images.get(s[2]) is not None:
                run_text = run_text[: s[0] - abs_idx]
                break

        use_font = emoji_font if (run_is_emoji and emoji_font is not None) else font
        try:
            if run_is_emoji and emoji_font is not None:
                draw.text((x, y), run_text, font=use_font, embedded_color=True)
            else:
                draw.text((x, y), run_text, font=use_font, fill=fill)
        except OSError:
            # Alguna fuente de emoji-color puede fallar con ciertos tamaños;
            # si pasa, se cae de nuevo a la fuente de texto normal.
            draw.text((x, y), run_text, font=font, fill=fill)
        x += draw.textlength(run_text, font=use_font)
        pos += len(run_text)


# --------------------------------------------------------------------- #
# Filas de la tarjeta (cada una es un mensaje: el citado, uno de contexto,
# o la vista previa de "a qué respondía")
# --------------------------------------------------------------------- #
@dataclass(slots=True)
class _Attachment:
    icon: str
    title: str
    subtitle: str = ""


@dataclass(slots=True)
class _ReplyPreview:
    name: str
    text: str
    seed: int


@dataclass(slots=True)
class _Row:
    name: str
    text: str
    seed: int
    emoji_spans: list[tuple[int, int, str]] = field(default_factory=list)
    emoji_images: dict[str, Optional[Image.Image]] = field(default_factory=dict)
    avatar: Optional[Image.Image] = None
    media: Optional[MediaThumb] = None
    attachment: Optional[_Attachment] = None
    forwarded_from: Optional[str] = None
    reply_preview: Optional[_ReplyPreview] = None


def _sender_name(message: Message) -> tuple[str, int]:
    if message.from_user:
        name = message.from_user.first_name or message.from_user.username or "Usuario"
        seed = message.from_user.id
    elif message.sender_chat:
        name = message.sender_chat.title or "Canal"
        seed = message.sender_chat.id
    else:
        name, seed = "Usuario", 0
    return name, (seed or hash(name))


def _forward_label(message: Message) -> Optional[str]:
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    kind = getattr(origin, "type", "")
    if kind == "user" and getattr(origin, "sender_user", None):
        return origin.sender_user.first_name or origin.sender_user.username or "alguien"
    if kind == "hidden_user":
        return getattr(origin, "sender_user_name", None) or "alguien"
    if kind == "chat" and getattr(origin, "sender_chat", None):
        return origin.sender_chat.title or "un canal"
    if kind == "channel" and getattr(origin, "chat", None):
        return origin.chat.title or "un canal"
    return "otro chat"


def _build_attachment(message: Message) -> Optional[_Attachment]:
    if message.document:
        size = message.document.file_size or 0
        subtitle = _human_size(size) if size else ""
        return _Attachment("📄", message.document.file_name or "Documento", subtitle)
    if message.audio:
        title = message.audio.title or message.audio.file_name or "Audio"
        subtitle = message.audio.performer or _human_duration(message.audio.duration)
        return _Attachment("🎵", title, subtitle)
    if message.voice:
        return _Attachment("🎤", "Nota de voz", _human_duration(message.voice.duration))
    if message.location:
        loc = message.location
        return _Attachment("📍", "Ubicación", f"{loc.latitude:.5f}, {loc.longitude:.5f}")
    if message.venue:
        return _Attachment("📍", message.venue.title, message.venue.address or "")
    if message.poll:
        poll = message.poll
        subtitle = f"{len(poll.options)} opciones · {poll.total_voter_count} votos"
        return _Attachment("📊", poll.question, subtitle)
    if message.contact:
        contact = message.contact
        name = " ".join(filter(None, [contact.first_name, contact.last_name])) or "Contacto"
        return _Attachment("👤", name, contact.phone_number or "")
    if message.dice:
        return _Attachment(message.dice.emoji, f"Dado: {message.dice.value}", "")
    return None


def _human_size(n: int) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _human_duration(seconds: int) -> str:
    m, s = divmod(int(seconds or 0), 60)
    return f"{m}:{s:02d}"


def _has_media(message: Message) -> bool:
    return bool(
        message.photo or message.sticker or message.video or message.animation or message.video_note
    )


async def _row_from_message(
    bot, message: Message, *, with_avatar: bool = False, with_media: bool = False
) -> _Row:
    name, seed = _sender_name(message)
    raw_text = (message.text or message.caption or "")
    entities = message.entities or message.caption_entities or []

    text = _sanitize_text(raw_text) or _sanitize_text(describe_media(message))
    spans = _custom_emoji_spans(raw_text, entities) if raw_text else []

    emoji_images: dict[str, Optional[Image.Image]] = {}
    if spans:
        try:
            emoji_images = await fetch_custom_emoji_images(bot, entities)
        except Exception as exc:  # noqa: BLE001
            logger.debug("No pude traer los emojis premium: %s", exc)

    row = _Row(
        name=_sanitize_text(name) or "Usuario",
        text=text,
        seed=seed,
        emoji_spans=spans,
        emoji_images=emoji_images,
        forwarded_from=_forward_label(message),
        attachment=None if raw_text.strip() and not _has_media(message) else _build_attachment(message),
    )

    if with_avatar and message.from_user:
        try:
            row.avatar = await fetch_avatar(bot, message.from_user.id, 96)
        except Exception as exc:  # noqa: BLE001
            logger.debug("No pude traer el avatar: %s", exc)

    if with_media and _has_media(message):
        try:
            row.media = await fetch_message_media(bot, message)
        except Exception as exc:  # noqa: BLE001
            logger.debug("No pude traer la miniatura: %s", exc)

    return row


# --------------------------------------------------------------------- #
# Render principal
# --------------------------------------------------------------------- #
_CANVAS_SIDE = 512
_PADDING = 28
_BAR_WIDTH = 5
_BAR_RADIUS = 3
_TEXT_GAP = 16
_ROW_GAP = 16
_AVATAR_SIZE = 60
_MEDIA_MAX = 220

_MAIN_NAME_SIZE = 28
_MAIN_TEXT_SIZE = 32
_CTX_NAME_SIZE = 21
_CTX_TEXT_SIZE = 24
_CTX_MAX_LINES = 2
_MAIN_MAX_LINES_CAP = 8
_LINE_SPACING = 1.28
_REPLY_PREVIEW_NAME_SIZE = 19
_REPLY_PREVIEW_TEXT_SIZE = 19
_FORWARD_LABEL_SIZE = 18


def _panel_color(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """Un tono ligeramente distinto al fondo, para las tarjetas internas
    (respuesta anidada, documento, audio, etc.) — más claro sobre fondos
    oscuros, más oscuro sobre fondos claros, para que siempre se note."""
    if _text_is_light(bg):
        return tuple(max(0, c - 16) for c in bg)  # type: ignore[return-value]
    return tuple(min(255, c + 14) for c in bg)  # type: ignore[return-value]


def _row_accent(seed: int, bg: tuple[int, int, int]) -> tuple[int, int, int]:
    color = _accent_color_for(seed)
    if _text_is_light(bg):
        color = tuple(max(0, c - 60) for c in color)  # type: ignore[assignment]
    return color


def _render_quote_card(
    rows: list[_Row],
    bg: tuple[int, int, int],
    rounded: bool,
    reply_preview: Optional[_ReplyPreview],
) -> Image.Image:
    if not rows:
        rows = [_Row(name="Usuario", text="", seed=0)]

    text_color_main = (30, 30, 32) if _text_is_light(bg) else (245, 245, 245)
    text_color_ctx = tuple((m + b) // 2 for m, b in zip(text_color_main, bg))
    muted = tuple((m + b) // 2 for m, b in zip(text_color_ctx, bg))

    body_font_main = _load_font(_REGULAR_FONT_PATHS, _MAIN_TEXT_SIZE)
    body_font_ctx = _load_font(_REGULAR_FONT_PATHS, _CTX_TEXT_SIZE)
    name_font_main = _load_font(_BOLD_FONT_PATHS, _MAIN_NAME_SIZE)
    name_font_ctx = _load_font(_BOLD_FONT_PATHS, _CTX_NAME_SIZE)
    small_font = _load_font(_REGULAR_FONT_PATHS, _REPLY_PREVIEW_TEXT_SIZE)
    small_bold_font = _load_font(_BOLD_FONT_PATHS, _REPLY_PREVIEW_NAME_SIZE)
    italic_font = _load_font(_ITALIC_FONT_PATHS or _REGULAR_FONT_PATHS, _FORWARD_LABEL_SIZE)
    emoji_font = _load_color_emoji_font()

    text_left = _PADDING + _BAR_WIDTH + _TEXT_GAP
    max_text_width = _CANVAS_SIDE - text_left - _PADDING

    probe = Image.new("RGBA", (10, 10))
    probe_draw = ImageDraw.Draw(probe)

    line_h_ctx = int(_CTX_TEXT_SIZE * _LINE_SPACING)
    line_h_main = int(_MAIN_TEXT_SIZE * _LINE_SPACING)

    *context_rows, main_row = rows

    forward_h = (_FORWARD_LABEL_SIZE + 6) if main_row.forwarded_from else 0

    reply_box_h = 0
    if reply_preview is not None:
        reply_box_h = _REPLY_PREVIEW_NAME_SIZE + 6 + int(_REPLY_PREVIEW_TEXT_SIZE * 1.3) + 18

    media_h = 0
    media_render: Optional[Image.Image] = None
    if main_row.media is not None:
        fitted = fit_within(main_row.media.image, _CANVAS_SIDE - _PADDING * 2, _MEDIA_MAX)
        media_render = rounded_crop(fitted, fitted.size[0], fitted.size[1], 18)
        media_h = media_render.size[1] + 12

    attachment_h = 0
    if main_row.attachment is not None and media_render is None:
        attachment_h = 74

    def ctx_block_h(n_lines: int) -> int:
        return _CTX_NAME_SIZE + 8 + n_lines * line_h_ctx

    def main_block_h(n_lines: int) -> int:
        extra = forward_h + reply_box_h
        text_h = n_lines * line_h_main if main_row.text.strip() else 0
        return _MAIN_NAME_SIZE + 10 + extra + text_h + media_h + attachment_h

    available_h = _CANVAS_SIDE - _PADDING * 2
    min_main_h = main_block_h(1 if main_row.text.strip() else 0)

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
    fixed_main = forward_h + reply_box_h + media_h + attachment_h + _MAIN_NAME_SIZE + 10
    max_main_lines = max(0, (remaining_for_main - fixed_main) // line_h_main)
    max_main_lines = min(max_main_lines, _MAIN_MAX_LINES_CAP)
    main_lines_with_idx: list[tuple[str, int]] = []
    if main_row.text.strip() and max_main_lines > 0:
        main_lines_with_idx = _wrap_preserving_emoji_spans(
            probe_draw, main_row.text, body_font_main, max_text_width, max_main_lines
        )
    n_main_lines = len(main_lines_with_idx)

    content_h = used_h + main_block_h(n_main_lines)
    canvas_h = min(_CANVAS_SIDE, max(120, _PADDING * 2 + content_h))

    img = Image.new("RGBA", (_CANVAS_SIDE, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if rounded:
        draw.rounded_rectangle((0, 0, _CANVAS_SIDE - 1, canvas_h - 1), radius=40, fill=(*bg, 255))
    else:
        draw.rectangle((0, 0, _CANVAS_SIDE - 1, canvas_h - 1), fill=(*bg, 255))

    y = _PADDING
    for row, lines in kept:
        accent = _row_accent(row.seed, bg)
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

    accent = _row_accent(main_row.seed, bg)
    block_h = main_block_h(n_main_lines)
    draw.rounded_rectangle((_PADDING, y, _PADDING + _BAR_WIDTH, y + block_h), radius=_BAR_RADIUS, fill=(*accent, 255))

    content_left = text_left
    content_width = max_text_width

    if main_row.avatar is not None:
        avatar_x = _CANVAS_SIDE - _PADDING - _AVATAR_SIZE
        avatar_resized = main_row.avatar.resize((_AVATAR_SIZE, _AVATAR_SIZE), Image.LANCZOS)
        img.paste(avatar_resized, (avatar_x, y), avatar_resized)
        content_width = avatar_x - content_left - 12

    ty = y - 4
    if main_row.forwarded_from:
        draw.text((content_left, ty), f"↪ Reenviado de {main_row.forwarded_from}", font=italic_font, fill=muted)
        ty += forward_h

    draw.text((content_left, ty), main_row.name, font=name_font_main, fill=accent)
    ty += _MAIN_NAME_SIZE + 10

    if reply_preview is not None:
        box_w = content_width
        rp_accent = _row_accent(reply_preview.seed, bg)
        overlay_bg = _panel_color(bg)
        draw.rounded_rectangle((content_left, ty, content_left + box_w, ty + reply_box_h - 8), radius=10, fill=(*overlay_bg, 255))
        draw.rounded_rectangle((content_left + 6, ty + 6, content_left + 9, ty + reply_box_h - 14), radius=2, fill=(*rp_accent, 255))
        draw.text((content_left + 16, ty + 6), reply_preview.name, font=small_bold_font, fill=rp_accent)
        rp_lines = _wrap_and_truncate(draw, reply_preview.text.strip() or " ", small_font, box_w - 24, 1)
        draw.text((content_left + 16, ty + 6 + _REPLY_PREVIEW_NAME_SIZE + 4), rp_lines[0], font=small_font, fill=text_color_ctx)
        ty += reply_box_h

    for line, idx in main_lines_with_idx:
        _draw_rich_line(
            draw, (content_left, ty), line, idx, body_font_main, emoji_font,
            text_color_main, main_row.emoji_spans, main_row.emoji_images, img,
        )
        ty += line_h_main

    if media_render is not None:
        img.paste(media_render, (content_left, ty), media_render)
        ty += media_render.size[1] + 12
    elif main_row.attachment is not None:
        att = main_row.attachment
        overlay_bg = _panel_color(bg)
        card_w = content_width
        draw.rounded_rectangle((content_left, ty, content_left + card_w, ty + attachment_h - 8), radius=14, fill=(*overlay_bg, 255))
        icon_font = _load_font(_REGULAR_FONT_PATHS, 30)
        draw.text((content_left + 14, ty + 14), att.icon, font=icon_font, fill=text_color_main)
        title_lines = _wrap_and_truncate(draw, att.title, name_font_ctx, card_w - 70, 1)
        draw.text((content_left + 60, ty + 10), title_lines[0], font=name_font_ctx, fill=text_color_main)
        if att.subtitle:
            sub_lines = _wrap_and_truncate(draw, att.subtitle, body_font_ctx, card_w - 70, 1)
            draw.text((content_left + 60, ty + 10 + _CTX_NAME_SIZE + 4), sub_lines[0], font=body_font_ctx, fill=muted)

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
    bot = context.bot

    target = message.reply_to_message
    if not target:
        await message.reply_text(error("Responde a un mensaje con /q para citarlo."))
        return

    bg, as_image, reply_mode, count = _parse_command_args(context.args or [])

    try:
        await context.bot.send_chat_action(chat.id, "upload_photo" if as_image else "choose_sticker")
    except Exception:  # noqa: BLE001
        pass

    main_row = await _row_from_message(bot, target, with_avatar=True, with_media=True)
    if not main_row.text and main_row.attachment is None and main_row.media is None:
        await message.reply_text(
            error("Ese mensaje no tiene nada que pueda citar (texto, imagen, etc.).")
        )
        return

    rows: list[_Row] = []
    if count > 0:
        stubs = get_previous(chat.id, target.message_id, count)
        for stub in stubs:
            text = (stub.text or "").strip()
            if text:
                rows.append(_Row(name=_sanitize_text(stub.name) or "Usuario", text=_sanitize_text(text), seed=stub.user_id))
    rows.append(main_row)

    reply_preview: Optional[_ReplyPreview] = None
    if reply_mode and target.reply_to_message:
        parent = target.reply_to_message
        pname, pseed = _sender_name(parent)
        ptext = (parent.text or parent.caption or "").strip()
        ptext = _sanitize_text(ptext) if ptext else _sanitize_text(describe_media(parent))
        if ptext:
            reply_preview = _ReplyPreview(name=_sanitize_text(pname) or "Usuario", text=ptext, seed=pseed)

    try:
        card = _render_quote_card(rows, bg, rounded=not as_image, reply_preview=reply_preview)
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
