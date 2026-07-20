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

Requiere Pillow (ya en requirements.txt). El texto se dibuja con las
fuentes DejaVu Sans que vienen EMPAQUETADAS en `bot/assets/fonts/` (no
dependen de que el servidor tenga fuentes instaladas — si a esa carpeta
le falta algún archivo, cae a buscarlas en el sistema, y si tampoco las
encuentra ahí, Pillow usa una fuente de emergencia minúscula que hace
que todo el texto/espaciado se vea roto; por eso es importante que la
carpeta `assets/fonts/` viaje junto con este archivo).
Para emojis a color de verdad hace falta una fuente de emoji-color
instalada en el servidor (opcional, ej. `apt install fonts-noto-color-emoji`)
— pero los emojis NORMALES ya no dependen de esto: se descargan como
imagen (ver utils/telegram_media.py) la primera vez que aparecen.
"""
from __future__ import annotations

import io
import logging
import random
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
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
    fetch_unicode_emoji_images,
    fit_within,
    rounded_crop,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Fuentes — primero se busca la que viaja EMPAQUETADA con el bot en
# bot/assets/fonts/ (así funciona igual sin importar qué tenga instalado
# el servidor); si por algo falta ese archivo, se prueban rutas típicas
# del sistema como respaldo.
# --------------------------------------------------------------------- #
_ASSETS_FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

_REGULAR_FONT_PATHS = [
    str(_ASSETS_FONTS_DIR / "DejaVuSans.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
_BOLD_FONT_PATHS = [
    str(_ASSETS_FONTS_DIR / "DejaVuSans-Bold.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
_ITALIC_FONT_PATHS = [
    str(_ASSETS_FONTS_DIR / "DejaVuSans-Oblique.ttf"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Italic.ttf",
]
# Fuente de emoji A COLOR (opcional, del sistema). Si no está, los emojis
# normales igual se ven bien porque se descargan como imagen (ver
# utils/telegram_media.fetch_unicode_emoji_images); esta fuente es solo
# un respaldo extra para el caso en que la descarga falle.
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
    logger.warning(
        "No se encontró ninguna fuente TrueType en %s (¿falta la carpeta bot/assets/fonts/?). "
        "Usando la fuente de respaldo de Pillow a tamaño %s.",
        paths, size,
    )
    try:
        # Pillow >= 10.1: load_default(size=...) devuelve una fuente
        # escalable de verdad, no el bitmap fijo diminuto de versiones
        # viejas — así que aunque falten TODAS las fuentes, el layout no
        # queda roto/desalineado.
        font = ImageFont.load_default(size=size)
    except TypeError:
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


_REGIONAL_INDICATOR = (0x1F1E6, 0x1F1FF)
_SKIN_TONES = (0x1F3FB, 0x1F3FF)


def _emoji_clusters(run: str) -> list[str]:
    """Divide un tramo YA sabido-todo-emoji en 'clusters' (cada uno es un
    solo emoji visual): agrupa banderas (2 indicadores regionales), y
    secuencias unidas con ZWJ (familias, profesiones con género, etc.) y
    sus modificadores de tono de piel / selectores de variación, para
    poder pedir/pegar UNA imagen por emoji visual en vez de una por
    carácter suelto."""
    clusters: list[str] = []
    i, n = 0, len(run)
    while i < n:
        code = ord(run[i])
        if _REGIONAL_INDICATOR[0] <= code <= _REGIONAL_INDICATOR[1] and i + 1 < n and (
            _REGIONAL_INDICATOR[0] <= ord(run[i + 1]) <= _REGIONAL_INDICATOR[1]
        ):
            clusters.append(run[i:i + 2])
            i += 2
            continue

        j = i + 1

        def _consume_modifiers(j: int) -> int:
            while j < n and (ord(run[j]) in _VARIATION_SELECTORS or _SKIN_TONES[0] <= ord(run[j]) <= _SKIN_TONES[1]):
                j += 1
            return j

        j = _consume_modifiers(j)
        while j < n and ord(run[j]) == 0x200D and j + 1 < n:
            j += 2  # ZWJ + el siguiente carácter base
            j = _consume_modifiers(j)

        clusters.append(run[i:j])
        i = j
    return clusters


_VARIATION_SELECTORS = {0xFE0E, 0xFE0F}


def _collect_emoji_clusters(*texts: str) -> set[str]:
    """Todos los clusters de emoji unicode 'normales' presentes en `texts`
    (nombre, mensaje, vista previa de respuesta, etc.), listos para
    pedirle sus imágenes a utils.telegram_media.fetch_unicode_emoji_images."""
    found: set[str] = set()
    for text in texts:
        if not text:
            continue
        for run_text, is_emoji in _split_runs(text):
            if is_emoji:
                found.update(_emoji_clusters(run_text))
    return found


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
# Dibuja texto (nombre O mensaje — se usa para ambos) sustituyendo:
#   1) emojis premium (custom_emoji) por su imagen real, si se descargó;
#   2) emojis unicode normales por su imagen real (estilo Twemoji), si se
#      descargó;
#   3) y solo si ninguna de las dos está disponible, cae a dibujar el
#      carácter con la fuente de emoji-color del sistema (si hay) o, en
#      último caso, con la fuente de texto normal (riesgo de "tofu").
# Devuelve el ancho total dibujado (útil si hace falta medir de nuevo).
# --------------------------------------------------------------------- #
def _draw_rich_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    line: str,
    line_start_idx: int,
    font: ImageFont.FreeTypeFont,
    emoji_font: Optional[ImageFont.FreeTypeFont],
    fill: tuple[int, int, int],
    custom_spans: list[tuple[int, int, str]],
    custom_images: dict[str, Optional[Image.Image]],
    unicode_images: dict[str, Optional[Image.Image]],
    canvas: Image.Image,
) -> int:
    x, y = xy
    start_x = x
    font_size = font.size
    pos = 0  # índice dentro de `line`
    while pos < len(line):
        abs_idx = line_start_idx + pos

        # 1) ¿Emoji premium con imagen ya descargada en este punto?
        custom = next(
            (s for s in custom_spans if s[0] <= abs_idx < s[1] and custom_images.get(s[2]) is not None), None
        )
        if custom:
            _, span_end, emoji_id = custom
            span_len_in_line = min(span_end, line_start_idx + len(line)) - abs_idx
            paste = custom_images[emoji_id].convert("RGBA").resize((font_size, font_size), Image.LANCZOS)
            canvas.paste(paste, (int(x), int(y)), paste)
            x += font_size
            pos += span_len_in_line
            continue

        rest = line[pos:]
        run_text, run_is_emoji = _split_runs(rest)[0]
        # No cruzar hacia un emoji premium ya cubierto más adelante en el run.
        for s in custom_spans:
            if s[0] > abs_idx and s[0] < abs_idx + len(run_text) and custom_images.get(s[2]) is not None:
                run_text = run_text[: s[0] - abs_idx]
                break

        if not run_is_emoji:
            draw.text((x, y), run_text, font=font, fill=fill)
            x += draw.textlength(run_text, font=font)
            pos += len(run_text)
            continue

        # 2) Emoji(s) unicode normales: un cluster (un emoji visual) a la vez.
        for cluster in _emoji_clusters(run_text):
            img = unicode_images.get(cluster)
            if img is not None:
                paste = img.convert("RGBA").resize((font_size, font_size), Image.LANCZOS)
                canvas.paste(paste, (int(x), int(y)), paste)
                x += font_size
            elif emoji_font is not None:
                try:
                    draw.text((x, y), cluster, font=emoji_font, embedded_color=True)
                    x += draw.textlength(cluster, font=emoji_font)
                except OSError:
                    draw.text((x, y), cluster, font=font, fill=fill)
                    x += draw.textlength(cluster, font=font)
            else:
                draw.text((x, y), cluster, font=font, fill=fill)
                x += draw.textlength(cluster, font=font)
        pos += len(run_text)

    return int(x - start_x)


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
# Render principal — burbuja de chat con "cola" (estilo Telegram), un
# único avatar semi-superpuesto en la esquina inferior izquierda de la
# ÚLTIMA burbuja, y (si /q <n> se usó) varias burbujas del mismo
# remitente apiladas y conectadas, tal como agrupa Telegram los mensajes
# consecutivos de una misma persona. Nada de tarjetas separadas ni
# barras de color: es el mismo diseño de siempre, solo que ahora también
# sabe dibujar avatar, miniaturas, adjuntos, reenvíos y vista previa de
# respuesta dentro de esa misma burbuja.
# --------------------------------------------------------------------- #
_CANVAS_SIDE = 512
_OUTER_MARGIN = 24
_AVATAR_SIZE = 72
_AVATAR_GAP = 12  # espacio libre entre el avatar y la burbuja (ya NO se superponen)
_BUBBLE_LEFT = _OUTER_MARGIN + _AVATAR_SIZE + _AVATAR_GAP
_BUBBLE_MAX_RIGHT = _CANVAS_SIDE - _OUTER_MARGIN
_BUBBLE_MAX_W = _BUBBLE_MAX_RIGHT - _BUBBLE_LEFT
_BUBBLE_RADIUS = 30
_BUBBLE_GAP = 7
_PAD_X = 26
_PAD_TOP = 18
_PAD_BOTTOM = 20
_MIN_BUBBLE_W = 150

_NAME_SIZE = 32
_TEXT_SIZE = 30
_LINE_SPACING = 1.3
_MAX_LINES = 8
_NAME_TEXT_GAP = 10

_REPLY_PREVIEW_NAME_SIZE = 20
_REPLY_PREVIEW_TEXT_SIZE = 20
_REPLY_PREVIEW_PAD = 12
_FORWARD_LABEL_SIZE = 18
_MEDIA_MAX_H = 230
_ATTACHMENT_H = 76


def _panel_color(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """Un tono ligeramente distinto al fondo, para paneles internos
    (vista previa de respuesta, tarjeta de adjunto) — más claro sobre
    fondos oscuros, más oscuro sobre fondos claros, para que se note."""
    if _text_is_light(bg):
        return tuple(max(0, c - 16) for c in bg)  # type: ignore[return-value]
    return tuple(min(255, c + 16) for c in bg)  # type: ignore[return-value]


def _row_accent(seed: int, bg: tuple[int, int, int]) -> tuple[int, int, int]:
    color = _accent_color_for(seed)
    if _text_is_light(bg):
        color = tuple(max(0, c - 60) for c in color)  # type: ignore[assignment]
    return color


@dataclass(slots=True)
class _Segment:
    """Una burbuja dentro de la pila (una por mensaje)."""
    name: str
    seed: int
    show_name: bool
    lines: list[tuple[str, int]]          # (línea, índice_en_texto_original) — para emojis premium
    emoji_spans: list[tuple[int, int, str]] = field(default_factory=list)
    emoji_images: dict[str, Optional[Image.Image]] = field(default_factory=dict)
    forwarded_from: Optional[str] = None
    reply_preview: Optional[_ReplyPreview] = None
    media: Optional[MediaThumb] = None
    attachment: Optional[_Attachment] = None
    is_last: bool = False


def _build_segments(
    rows: list[_Row],
    reply_preview: Optional[_ReplyPreview],
    draw: ImageDraw.ImageDraw,
    name_font: ImageFont.FreeTypeFont,
    text_font: ImageFont.FreeTypeFont,
    max_text_width: int,
) -> list[_Segment]:
    segments: list[_Segment] = []
    prev_seed: Optional[int] = None
    for i, row in enumerate(rows):
        is_last = i == len(rows) - 1
        show_name = prev_seed is None or row.seed != prev_seed
        prev_seed = row.seed

        lines = (
            _wrap_preserving_emoji_spans(draw, row.text, text_font, max_text_width, _MAX_LINES)
            if row.text.strip()
            else []
        )
        segments.append(
            _Segment(
                name=row.name,
                seed=row.seed,
                show_name=show_name,
                lines=lines,
                emoji_spans=row.emoji_spans if is_last else [],
                emoji_images=row.emoji_images if is_last else {},
                forwarded_from=row.forwarded_from if is_last else None,
                reply_preview=reply_preview if is_last else None,
                media=row.media if is_last else None,
                attachment=row.attachment if is_last else None,
                is_last=is_last,
            )
        )
    return segments


def _segment_metrics(
    seg: _Segment,
    draw: ImageDraw.ImageDraw,
    name_font: ImageFont.FreeTypeFont,
    text_font: ImageFont.FreeTypeFont,
    small_font: ImageFont.FreeTypeFont,
    small_bold_font: ImageFont.FreeTypeFont,
    italic_font: ImageFont.FreeTypeFont,
    line_h: int,
) -> tuple[int, int, Optional[Image.Image], list[str]]:
    """Devuelve (ancho_contenido, alto_total, miniatura_ya_recortada, líneas_de_preview)."""
    content_w = 0
    content_h = 0

    if seg.show_name:
        content_w = max(content_w, int(draw.textlength(seg.name, font=name_font)))
        content_h += _NAME_SIZE + _NAME_TEXT_GAP

    if seg.forwarded_from:
        label = f"↪ Reenviado de {seg.forwarded_from}"
        content_w = max(content_w, int(draw.textlength(label, font=italic_font)))
        content_h += _FORWARD_LABEL_SIZE + 6

    rp_lines: list[str] = []
    if seg.reply_preview is not None:
        rp = seg.reply_preview
        avail = _BUBBLE_MAX_W - _PAD_X * 2 - _REPLY_PREVIEW_PAD * 2
        rp_lines = _wrap_and_truncate(draw, rp.text.strip() or " ", small_font, avail, 1)
        rp_w = max(
            int(draw.textlength(rp.name, font=small_bold_font)),
            int(draw.textlength(rp_lines[0], font=small_font)),
        ) + _REPLY_PREVIEW_PAD * 2 + 10
        content_w = max(content_w, rp_w)
        content_h += _REPLY_PREVIEW_NAME_SIZE + 6 + int(_REPLY_PREVIEW_TEXT_SIZE * 1.3) + _REPLY_PREVIEW_PAD + 6

    for line, _idx in seg.lines:
        content_w = max(content_w, int(draw.textlength(line, font=text_font)))
    if seg.lines:
        content_h += len(seg.lines) * line_h

    media_render: Optional[Image.Image] = None
    if seg.media is not None:
        avail_w = _BUBBLE_MAX_W - _PAD_X * 2
        fitted = fit_within(seg.media.image, avail_w, _MEDIA_MAX_H)
        media_render = rounded_crop(fitted, fitted.size[0], fitted.size[1], 18)
        content_w = max(content_w, media_render.size[0])
        content_h += media_render.size[1] + 10

    if seg.attachment is not None and media_render is None:
        content_w = max(content_w, _BUBBLE_MAX_W - _PAD_X * 2)
        content_h += _ATTACHMENT_H

    if content_w == 0:
        content_w = int(draw.textlength(" ", font=text_font)) or 20

    content_w = min(content_w, _BUBBLE_MAX_W - _PAD_X * 2)
    return content_w, content_h, media_render, rp_lines


def _render_quote_card(
    rows: list[_Row],
    bg: tuple[int, int, int],
    rounded: bool,
    reply_preview: Optional[_ReplyPreview],
    unicode_images: Optional[dict[str, Optional[Image.Image]]] = None,
) -> Image.Image:
    if not rows:
        rows = [_Row(name="Usuario", text="", seed=0)]
    unicode_images = unicode_images or {}

    text_color = (30, 30, 32) if _text_is_light(bg) else (245, 245, 245)
    muted = tuple((m + b) // 2 for m, b in zip(text_color, bg))

    name_font = _load_font(_BOLD_FONT_PATHS, _NAME_SIZE)
    text_font = _load_font(_REGULAR_FONT_PATHS, _TEXT_SIZE)
    small_font = _load_font(_REGULAR_FONT_PATHS, _REPLY_PREVIEW_TEXT_SIZE)
    small_bold_font = _load_font(_BOLD_FONT_PATHS, _REPLY_PREVIEW_NAME_SIZE)
    italic_font = _load_font(_ITALIC_FONT_PATHS or _REGULAR_FONT_PATHS, _FORWARD_LABEL_SIZE)
    icon_font = _load_font(_REGULAR_FONT_PATHS, 30)
    emoji_font = _load_color_emoji_font()

    line_h = int(_TEXT_SIZE * _LINE_SPACING)
    max_text_width = _BUBBLE_MAX_W - _PAD_X * 2

    probe = Image.new("RGBA", (10, 10))
    probe_draw = ImageDraw.Draw(probe)

    segments = _build_segments(rows, reply_preview, probe_draw, name_font, text_font, max_text_width)

    metrics = []
    for seg in segments:
        content_w, content_h, media_render, rp_lines = _segment_metrics(
            seg, probe_draw, name_font, text_font, small_font, small_bold_font, italic_font, line_h
        )
        bubble_w = max(_MIN_BUBBLE_W, content_w + _PAD_X * 2)
        bubble_h = content_h + _PAD_TOP + _PAD_BOTTOM
        metrics.append((bubble_w, bubble_h, media_render, rp_lines))

    total_h = _OUTER_MARGIN + sum(m[1] for m in metrics) + _BUBBLE_GAP * (len(metrics) - 1) + _OUTER_MARGIN
    total_h += _AVATAR_SIZE // 2  # espacio para que el avatar no se corte abajo
    canvas_h = min(_CANVAS_SIDE, max(140, int(total_h)))

    img = Image.new("RGBA", (_CANVAS_SIDE, canvas_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    y = _OUTER_MARGIN
    last_bottom = y
    for i, (seg, (bubble_w, bubble_h, media_render, rp_lines)) in enumerate(zip(segments, metrics)):
        accent = _row_accent(seg.seed, bg)
        x0, y0 = _BUBBLE_LEFT, y
        x1, y1 = _BUBBLE_LEFT + bubble_w, y + bubble_h
        if rounded:
            draw.rounded_rectangle((x0, y0, x1, y1), radius=_BUBBLE_RADIUS, fill=(*bg, 255))
        else:
            draw.rectangle((x0, y0, x1, y1), fill=(*bg, 255))

        ty = y0 + _PAD_TOP
        tx = x0 + _PAD_X

        if seg.show_name:
            _draw_rich_text(
                draw, (tx, ty), seg.name, 0, name_font, emoji_font,
                accent, [], {}, unicode_images, img,
            )
            ty += _NAME_SIZE + _NAME_TEXT_GAP

        if seg.forwarded_from:
            draw.text((tx, ty), f"↪ Reenviado de {seg.forwarded_from}", font=italic_font, fill=muted)
            ty += _FORWARD_LABEL_SIZE + 6

        if seg.reply_preview is not None:
            rp = seg.reply_preview
            rp_accent = _row_accent(rp.seed, bg)
            panel_bg = _panel_color(bg)
            panel_h = _REPLY_PREVIEW_NAME_SIZE + 6 + int(_REPLY_PREVIEW_TEXT_SIZE * 1.3) + _REPLY_PREVIEW_PAD
            panel_w = bubble_w - _PAD_X * 2
            draw.rounded_rectangle((tx, ty, tx + panel_w, ty + panel_h), radius=10, fill=(*panel_bg, 255))
            draw.rounded_rectangle(
                (tx + 6, ty + 6, tx + 9, ty + panel_h - 6), radius=2, fill=(*rp_accent, 255)
            )
            _draw_rich_text(
                draw, (tx + 16, ty + 6), rp.name, 0, small_bold_font, emoji_font,
                rp_accent, [], {}, unicode_images, img,
            )
            rp_text = rp_lines[0] if rp_lines else ""
            _draw_rich_text(
                draw, (tx + 16, ty + 6 + _REPLY_PREVIEW_NAME_SIZE + 4), rp_text, 0, small_font, emoji_font,
                muted, [], {}, unicode_images, img,
            )
            ty += panel_h + 6

        for line, idx in seg.lines:
            _draw_rich_text(
                draw, (tx, ty), line, idx, text_font, emoji_font,
                text_color, seg.emoji_spans, seg.emoji_images, unicode_images, img,
            )
            ty += line_h

        if media_render is not None:
            img.paste(media_render, (tx, ty), media_render)
            ty += media_render.size[1] + 10
        elif seg.attachment is not None:
            att = seg.attachment
            panel_bg = _panel_color(bg)
            card_w = bubble_w - _PAD_X * 2
            draw.rounded_rectangle((tx, ty, tx + card_w, ty + _ATTACHMENT_H - 8), radius=14, fill=(*panel_bg, 255))
            draw.text((tx + 14, ty + 14), att.icon, font=icon_font, fill=text_color)
            title_lines = _wrap_and_truncate(draw, att.title, small_bold_font, card_w - 70, 1)
            draw.text((tx + 60, ty + 10), title_lines[0], font=small_bold_font, fill=text_color)
            if att.subtitle:
                sub_lines = _wrap_and_truncate(draw, att.subtitle, small_font, card_w - 70, 1)
                draw.text((tx + 60, ty + 10 + _REPLY_PREVIEW_NAME_SIZE + 4), sub_lines[0], font=small_font, fill=muted)

        last_bottom = y1
        y = y1 + _BUBBLE_GAP

    # avatar: un único círculo, apoyado por completo A LA IZQUIERDA de la
    # burbuja (sin superponerse), alineado por abajo con la ÚLTIMA burbuja.
    main_row = rows[-1]
    if main_row.avatar is not None:
        avatar_resized = main_row.avatar.resize((_AVATAR_SIZE, _AVATAR_SIZE), Image.LANCZOS)
        ax = _OUTER_MARGIN
        ay = last_bottom - _AVATAR_SIZE
        img.paste(avatar_resized, (ax, ay), avatar_resized)

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

    texts_to_scan = [t for row in rows for t in (row.name, row.text)]
    if reply_preview:
        texts_to_scan += [reply_preview.name, reply_preview.text]
    clusters = _collect_emoji_clusters(*texts_to_scan)
    try:
        unicode_images = await fetch_unicode_emoji_images(clusters) if clusters else {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("No pude traer las imágenes de emoji unicode: %s", exc)
        unicode_images = {}

    try:
        card = _render_quote_card(
            rows, bg, rounded=not as_image, reply_preview=reply_preview, unicode_images=unicode_images
        )
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
