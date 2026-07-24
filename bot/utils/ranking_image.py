"""
utils/ranking_image.py
Genera la imagen dinámica (1200x1200) del ranking de mensajes para /top
(ver handlers/activity_ranking.py): dashboard oscuro tipo "glassmorphism"
con degradados, luces ambientales, panel de vidrio, avatares circulares,
barras proporcionales con brillo y el logo del bot.

Todo el trabajo de red (avatares, emojis) se hace ANTES, de forma
asíncrona, en `build_ranking_image`. El dibujo con Pillow en sí
(`generate_ranking_image` y las funciones `draw_*` que usa) es sync y
puro CPU, así que se corre en un hilo aparte (`asyncio.to_thread`) para
no bloquear el event loop del bot mientras se arma la imagen.

Fuentes y logo se cargan UNA SOLA VEZ al importar este módulo (no en
cada llamada), como pide la consigna de "optimizar para VPS".
"""
from __future__ import annotations

import asyncio
import io
import logging
import math
import time
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from telegram import Bot, Chat
from telegram.error import TelegramError

from database import ActivityEntry, Database
from utils.telegram_media import fetch_avatar, fetch_unicode_emoji_images

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Constantes de layout / paleta
# --------------------------------------------------------------------- #
SIZE = 1200
MARGIN = 46
PANEL_RADIUS = 46
PANEL_PADDING = 46
AVATAR_SIZE = 56
BAR_HEIGHT = 14
ROW_COUNT = 10
FOOTER_HEIGHT = 116

BG_COLOR = (9, 9, 11)
BG_TINT = (30, 25, 46)
PANEL_COLOR = (19, 19, 23)
PANEL_BORDER = (56, 56, 66)
TEXT_COLOR = (255, 255, 255)
TEXT_SECONDARY = (176, 176, 186)
TEXT_DIM = (120, 120, 130)

GOLD = (255, 209, 102)
GOLD_2 = (255, 170, 60)
SILVER = (222, 222, 230)
SILVER_2 = (150, 150, 168)
BRONZE = (224, 150, 96)
BRONZE_2 = (170, 96, 56)

ACCENT_A = (130, 96, 255)   # violeta
ACCENT_B = (68, 190, 255)   # celeste
ACCENT_C = (255, 92, 160)   # magenta

PERIOD_LABELS = {"today": "Hoy", "week": "Semana", "all": "Siempre"}

AVATAR_PALETTE = [
    ((130, 96, 255), (68, 190, 255)),
    ((255, 92, 160), (255, 176, 90)),
    ((68, 190, 255), (96, 255, 190)),
    ((255, 150, 62), (255, 92, 92)),
    ((150, 92, 255), (92, 190, 255)),
    ((255, 176, 60), (255, 96, 150)),
]

# --------------------------------------------------------------------- #
# Carga de fuentes y logo (una sola vez)
# --------------------------------------------------------------------- #
_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
_FONT_DIR = _ASSETS_DIR / "fonts" / "poppins"
_LOGO_PATH = _ASSETS_DIR / "branding" / "ceo_logo.png"
_FALLBACK_FONT_DIR = _ASSETS_DIR / "fonts"  # DejaVu Sans, por si falta Poppins


def _load_font(filename: str, size: int, fallback: str = "DejaVuSans.ttf") -> ImageFont.FreeTypeFont:
    path = _FONT_DIR / filename
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        logger.warning("No se encontró la fuente %s, usando %s como respaldo.", path, fallback)
        return ImageFont.truetype(str(_FALLBACK_FONT_DIR / fallback), size)


class _Fonts:
    title = _load_font("Poppins-ExtraBold.ttf", 52, "DejaVuSans-Bold.ttf")
    subtitle = _load_font("Poppins-Medium.ttf", 25, "DejaVuSans.ttf")
    pill = _load_font("Poppins-SemiBold.ttf", 20, "DejaVuSans-Bold.ttf")
    name = _load_font("Poppins-SemiBold.ttf", 27, "DejaVuSans-Bold.ttf")
    count = _load_font("Poppins-Bold.ttf", 23, "DejaVuSans-Bold.ttf")
    rank = _load_font("Poppins-Bold.ttf", 22, "DejaVuSans-Bold.ttf")
    initial = _load_font("Poppins-Bold.ttf", 28, "DejaVuSans-Bold.ttf")
    footer_label = _load_font("Poppins-Medium.ttf", 21, "DejaVuSans.ttf")
    credit = _load_font("Poppins-Medium.ttf", 19, "DejaVuSans.ttf")


_FONTS = _Fonts()

try:
    _LOGO = Image.open(_LOGO_PATH).convert("RGBA")
except OSError:
    logger.warning("No se encontró el logo del bot en %s.", _LOGO_PATH)
    _LOGO = None


# --------------------------------------------------------------------- #
# Helpers de dibujo
# --------------------------------------------------------------------- #
def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def _horizontal_gradient(size: tuple[int, int], c1: tuple[int, int, int], c2: tuple[int, int, int]) -> Image.Image:
    w, h = size
    grad = Image.new("RGB", (w, 1))
    px = grad.load()
    for x in range(w):
        px[x, 0] = _lerp(c1, c2, x / max(1, w - 1))
    return grad.resize((w, h))


@lru_cache(maxsize=None)
def _font_cmap(font_path: str) -> frozenset[int]:
    """Codepoints que la fuente realmente sabe dibujar, leídos de su tabla
    `cmap`. No usamos `ImageFont.getmask().getbbox()` para esto porque el
    glifo ".notdef" (el que se usa cuando falta un carácter) muchas veces
    ES un cuadrito visible con bbox no vacío -- por eso salían los
    cuadritos aunque el chequeo "tenga bbox" pareciera pasar."""
    try:
        tt = TTFont(font_path, lazy=True)
        cmap = tt.getBestCmap() or {}
        return frozenset(cmap.keys())
    except Exception:
        logger.warning("No se pudo leer la tabla cmap de %s", font_path)
        return frozenset()


def _has_glyph_cached(font: ImageFont.FreeTypeFont, ch: str) -> bool:
    if ch.isspace():
        return True
    return ord(ch) in _font_cmap(font.path)


# NFKC ya normaliza la mayoría de las "fuentes" unicode decorativas
# (negrita, cursiva, gótica, ancho completo, círculos, etc.), pero el
# estilo "small caps" (ᴀʙᴄᴅᴇ...), muy usado también para decorar nombres,
# no tiene descomposición de compatibilidad. Se mapea a mano.
_SMALLCAPS_MAP = str.maketrans(
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀᴛᴜᴠᴡʏᴢ",
    "ABCDEFGHIJKLMNOPQRTUVWYZ",
)


def _clean_text(text: str, font: ImageFont.FreeTypeFont) -> str:
    """Normaliza un nombre para que nunca salga con 'cuadritos':

    1) NFKC convierte letras de fuentes unicode 'estilizadas' (negrita,
       cursiva, gótica, doble raya, ancho completo, etc. -- lo que suele
       usar la gente para 'decorar' su nombre en Telegram) a su letra
       latina normal equivalente, así se ven consistentes con el resto
       del diseño en vez de con una tipografía random.
    2) Se descartan marcas combinantes / caracteres de formato invisibles.
    3) Se descarta cualquier carácter que la fuente del panel (Poppins)
       no tenga en su tabla de glifos (emojis raros, otros alfabetos,
       símbolos), que es justo lo que generaba los cuadritos.
    """
    if not text:
        return ""
    text = text.translate(_SMALLCAPS_MAP)
    text = unicodedata.normalize("NFKC", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) not in ("Mn", "Cf"))
    text = "".join(ch for ch in text if _has_glyph_cached(font, ch))
    return " ".join(text.split())


def _display_text(entry: ActivityEntry, font: ImageFont.FreeTypeFont) -> str:
    """Texto a mostrar en la fila: preferimos el @username (siempre en
    ASCII simple, sin riesgo de fuentes raras); si el usuario no tiene
    username, mostramos el nombre pero saneado con `_clean_text`."""
    if entry.username:
        return f"@{entry.username}"
    cleaned = _clean_text(entry.display_name, font)
    return cleaned or f"ID {entry.user_id}"


def _initials(entry: ActivityEntry) -> str:
    base = entry.username or entry.display_name
    base = _clean_text(base, _FONTS.initial).lstrip("@")
    return (base[0] if base else "?").upper()


def _draw_glow_blob(layer: Image.Image, cx: int, cy: int, r: int, color: tuple[int, int, int], alpha: int) -> None:
    ImageDraw.Draw(layer).ellipse((cx - r, cy - r, cx + r, cy + r), fill=color + (alpha,))


# --------------------------------------------------------------------- #
# 1) Fondo: degradado + luces ambientales + formas abstractas + viñeta
# --------------------------------------------------------------------- #
def draw_background() -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG_COLOR)

    grad_mask = Image.new("L", (1, SIZE), 0)
    for y in range(SIZE):
        t = y / SIZE
        grad_mask.putpixel((0, y), int(60 * math.sin(t * math.pi)))
    grad_mask = grad_mask.resize((SIZE, SIZE))
    tint = Image.new("RGB", (SIZE, SIZE), BG_TINT)
    img = Image.composite(tint, img, grad_mask)
    img = img.convert("RGBA")

    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    _draw_glow_blob(glow, 110, 70, 430, ACCENT_A, 68)
    _draw_glow_blob(glow, SIZE - 90, 140, 380, ACCENT_B, 52)
    _draw_glow_blob(glow, SIZE - 120, SIZE - 100, 470, ACCENT_C, 42)
    _draw_glow_blob(glow, 70, SIZE - 140, 360, ACCENT_A, 38)
    glow = glow.filter(ImageFilter.GaussianBlur(165))
    img = Image.alpha_composite(img, glow)

    rings = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    rd = ImageDraw.Draw(rings)
    rd.ellipse((-280, -280, 360, 360), outline=(255, 255, 255, 16), width=26)
    rd.ellipse((SIZE - 420, SIZE - 460, SIZE + 300, SIZE + 300), outline=(255, 255, 255, 12), width=22)
    rings = rings.filter(ImageFilter.GaussianBlur(2))
    img = Image.alpha_composite(img, rings)

    vign = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(vign).ellipse((-260, -260, SIZE + 260, SIZE + 260), fill=90)
    vign = vign.filter(ImageFilter.GaussianBlur(260))
    vign_inv = ImageOps.invert(vign).point(lambda p: int(p * 0.16))
    dark = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 255))
    img = Image.composite(img, Image.alpha_composite(img, dark), vign_inv)

    return img.convert("RGBA")


def _draw_glass_panel(img: Image.Image, box: tuple[int, int, int, int]) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0

    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((x0, y0 + 20, x1, y1 + 20), radius=PANEL_RADIUS, fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(36))
    img.alpha_composite(shadow)

    panel = Image.new("RGBA", (w, h), PANEL_COLOR + (230,))
    top_light = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    tld = ImageDraw.Draw(top_light)
    fade_h = h // 3
    for y in range(fade_h):
        a = max(0, int(24 * (1 - y / fade_h)))
        tld.line((0, y, w, y), fill=(255, 255, 255, a))
    panel = Image.alpha_composite(panel, top_light)
    panel.putalpha(Image.composite(panel.split()[3], Image.new("L", (w, h), 0), _rounded_mask((w, h), PANEL_RADIUS)))
    img.alpha_composite(panel, (x0, y0))

    border = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bd = ImageDraw.Draw(border)
    bd.rounded_rectangle(box, radius=PANEL_RADIUS, outline=PANEL_BORDER + (255,), width=2)
    bd.arc((x0, y0, x1, y0 + h // 2), start=195, end=345, fill=(255, 255, 255, 45), width=2)
    img.alpha_composite(border)


# --------------------------------------------------------------------- #
# 2) Header: icono + título + subtítulo + píldora de período + divisor
# --------------------------------------------------------------------- #
def _draw_header_icon(img: Image.Image, x: int, y: int, size: int) -> None:
    """Pictograma propio (3 barras ascendentes) en vez de un emoji, para
    que el header no dependa de ninguna descarga."""
    bars = 3
    gap = size * 0.14
    bar_w = (size - gap * (bars - 1)) / bars
    heights = [size * 0.45, size * 0.72, size]
    colors = [ACCENT_A, _lerp(ACCENT_A, ACCENT_B, 0.5), ACCENT_B]
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for i, (h, color) in enumerate(zip(heights, colors)):
        bx0 = i * (bar_w + gap)
        by0 = size - h
        ld.rounded_rectangle((bx0, by0, bx0 + bar_w, size), radius=bar_w / 2.4, fill=color + (255,))
    img.alpha_composite(layer, (int(x), int(y)))


def _draw_pill(img: Image.Image, draw: ImageDraw.ImageDraw, text: str, right_x: int, top_y: int) -> None:
    bbox = draw.textbbox((0, 0), text, font=_FONTS.pill)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 22, 12
    pw, ph = tw + pad_x * 2, th + pad_y * 2
    x0, y0 = right_x - pw, top_y
    pill = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    grad = _horizontal_gradient((pw, ph), ACCENT_A, ACCENT_B).convert("RGBA")
    grad.putalpha(60)
    mask = _rounded_mask((pw, ph), ph // 2)
    pill = Image.composite(grad, pill, mask)
    outline = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    ImageDraw.Draw(outline).rounded_rectangle((0, 0, pw - 1, ph - 1), radius=ph // 2, outline=(255, 255, 255, 70), width=2)
    pill = Image.alpha_composite(pill, outline)
    img.alpha_composite(pill, (x0, y0))
    draw.text((x0 + pad_x - bbox[0], y0 + pad_y - bbox[1]), text, font=_FONTS.pill, fill=TEXT_COLOR)


def draw_header(img: Image.Image, draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], period: str) -> int:
    """Dibuja el encabezado dentro de `box` (área de contenido del panel)
    y devuelve el `y` donde puede empezar el siguiente bloque (las filas)."""
    x0, y0, x1, _ = box
    icon_size = 46
    _draw_header_icon(img, x0, y0 + 6, icon_size)

    title_x = x0 + icon_size + 20
    draw.text((title_x, y0 - 6), "TOP ACTIVITY", font=_FONTS.title, fill=TEXT_COLOR)

    period_label = PERIOD_LABELS.get(period, "Siempre").upper()
    _draw_pill(img, draw, period_label, x1, y0 + 2)

    y = y0 + 64
    draw.text((title_x, y), "Ranking de mensajes del grupo", font=_FONTS.subtitle, fill=TEXT_SECONDARY)
    y += 48

    content_w = x1 - x0
    divider = Image.new("RGBA", (content_w, 3), (0, 0, 0, 0))
    dd = ImageDraw.Draw(divider)
    for i in range(content_w):
        t = i / content_w
        a = int(220 * (1 - abs(t - 0.5) * 2) ** 0.7)
        dd.line((i, 0, i, 2), fill=_lerp(ACCENT_A, ACCENT_B, t) + (a,))
    img.alpha_composite(divider, (x0, y))
    y += 30
    return y


# --------------------------------------------------------------------- #
# Avatares (foto real ya circular, o iniciales con degradado)
# --------------------------------------------------------------------- #
def _fallback_avatar(entry: ActivityEntry, rank: int, size: int) -> Image.Image:
    c1, c2 = AVATAR_PALETTE[rank % len(AVATAR_PALETTE)]
    grad = _horizontal_gradient((size, size), c1, c2).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    d = ImageDraw.Draw(out)
    letter = _initials(entry)
    bbox = d.textbbox((0, 0), letter, font=_FONTS.initial)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1]), letter, font=_FONTS.initial, fill=(255, 255, 255, 255))
    return out


# --------------------------------------------------------------------- #
# 3) Una fila del ranking: medalla/puesto, avatar, nombre, barra, conteo
# --------------------------------------------------------------------- #
def draw_user(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    rank: int,
    entry: ActivityEntry,
    count: int,
    max_count: int,
    avatar: Optional[Image.Image],
    medal_emoji: Optional[Image.Image],
    box: tuple[int, int, int, int],
    y: int,
    row_h: int,
) -> None:
    x0, _, x1, _ = box
    row_cy = y + row_h / 2

    rank_col_w = 46
    if rank < 3 and medal_emoji is not None:
        em = medal_emoji.resize((38, 38), Image.LANCZOS)
        img.alpha_composite(em, (int(x0 - 2), int(row_cy - 19)))
    else:
        label = f"#{rank + 1}"
        bbox = draw.textbbox((0, 0), label, font=_FONTS.rank)
        tw = bbox[2] - bbox[0]
        draw.text((x0 + (rank_col_w - tw) / 2, row_cy - 14), label, font=_FONTS.rank, fill=TEXT_DIM)

    av_x = x0 + rank_col_w + 12
    av = avatar if avatar is not None else _fallback_avatar(entry, rank, AVATAR_SIZE)
    ring = Image.new("RGBA", (AVATAR_SIZE + 6, AVATAR_SIZE + 6), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((0, 0, AVATAR_SIZE + 5, AVATAR_SIZE + 5), outline=(255, 255, 255, 55), width=2)
    img.alpha_composite(ring, (int(av_x - 3), int(row_cy - AVATAR_SIZE / 2 - 3)))
    img.alpha_composite(av, (int(av_x), int(row_cy - AVATAR_SIZE / 2)))

    text_x = av_x + AVATAR_SIZE + 20
    name = _display_text(entry, _FONTS.name)
    max_name_w = int((x1 - x0) * 0.5)
    while draw.textbbox((0, 0), name, font=_FONTS.name)[2] > max_name_w and len(name) > 3:
        name = name[:-2] + "…"
    draw.text((text_x, y + 1), name, font=_FONTS.name, fill=TEXT_COLOR)

    count_txt = f"{count:,}".replace(",", ".") + " mensajes"
    cb = draw.textbbox((0, 0), count_txt, font=_FONTS.count)
    cw = cb[2] - cb[0]
    draw.text((x1 - cw, y + 2), count_txt, font=_FONTS.count, fill=TEXT_SECONDARY)

    bar_x = text_x
    bar_w_max = x1 - bar_x
    bar_y = y + 38

    track = Image.new("RGBA", (bar_w_max, BAR_HEIGHT), (255, 255, 255, 16))
    track.putalpha(Image.composite(
        track.split()[3], Image.new("L", (bar_w_max, BAR_HEIGHT), 0), _rounded_mask((bar_w_max, BAR_HEIGHT), BAR_HEIGHT // 2),
    ))
    img.alpha_composite(track, (bar_x, bar_y))

    frac = max(0.05, count / max_count) if max_count else 0.05
    bw = max(BAR_HEIGHT, int(bar_w_max * frac))
    if rank == 0:
        c1, c2, glow = GOLD, GOLD_2, GOLD
    elif rank == 1:
        c1, c2, glow = SILVER, SILVER_2, SILVER
    elif rank == 2:
        c1, c2, glow = BRONZE, BRONZE_2, BRONZE
    else:
        c1, c2, glow = ACCENT_A, ACCENT_B, None
    _draw_bar(img, bar_x, bar_y, bw, BAR_HEIGHT, c1, c2, glow)


def _draw_bar(
    img: Image.Image, x: int, y: int, w: int, h: int,
    c1: tuple[int, int, int], c2: tuple[int, int, int], glow: Optional[tuple[int, int, int]],
) -> None:
    w = max(h, w)
    bar = _horizontal_gradient((w, h), c1, c2).convert("RGBA")
    mask = _rounded_mask((w, h), h // 2)
    bar.putalpha(mask)

    if glow is not None:
        glow_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        gm = Image.new("RGBA", (w, h), glow + (150,))
        gm.putalpha(Image.composite(gm.split()[3], Image.new("L", (w, h), 0), mask))
        glow_layer.alpha_composite(gm, (x, y))
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(9))
        img.alpha_composite(glow_layer)

    img.alpha_composite(bar, (x, y))

    sheen = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(sheen).rounded_rectangle((0, 0, w - 1, h // 2), radius=h // 2, fill=(255, 255, 255, 24))
    sheen.putalpha(Image.composite(sheen.split()[3], Image.new("L", (w, h), 0), mask))
    img.alpha_composite(sheen, (x, y))


# --------------------------------------------------------------------- #
# 4) Footer: miembros, mensajes totales, última actualización, crédito, logo
# --------------------------------------------------------------------- #
def draw_footer(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    panel_box: tuple[int, int, int, int],
    members_count: Optional[int],
    total_messages: int,
    updated_label: str,
    icons: dict[str, Optional[Image.Image]],
) -> None:
    x0, y0, x1, _ = box
    content_w = x1 - x0

    divider = Image.new("RGBA", (content_w, 2), (255, 255, 255, 22))
    img.alpha_composite(divider, (x0, y0))
    y = y0 + 26

    members_txt = f"{members_count:,}".replace(",", ".") + " miembros" if members_count is not None else "— miembros"
    total_txt = f"{total_messages:,}".replace(",", ".") + " mensajes"
    stats = [("members", members_txt), ("messages", total_txt), ("calendar", updated_label)]

    sx = x0
    for key, label in stats:
        icon = icons.get(key)
        if icon is not None:
            img.alpha_composite(icon.resize((24, 24), Image.LANCZOS), (sx, y + 2))
        draw.text((sx + 32, y), label, font=_FONTS.footer_label, fill=TEXT_SECONDARY)
        bbox = draw.textbbox((0, 0), label, font=_FONTS.footer_label)
        sx += 32 + (bbox[2] - bbox[0]) + 36

    credit = "Powered by CEO Bot by @Sky_lent"
    cb = draw.textbbox((0, 0), credit, font=_FONTS.credit)
    cw = cb[2] - cb[0]
    robot_icon = icons.get("robot")
    icon_gap = 28 if robot_icon is not None else 0
    if robot_icon is not None:
        img.alpha_composite(robot_icon.resize((22, 22), Image.LANCZOS), (x1 - cw - icon_gap, y + 1))
    draw.text((x1 - cw, y), credit, font=_FONTS.credit, fill=TEXT_DIM)

    # Logo del bot, esquina inferior izquierda del panel, con sombra suave.
    if _LOGO is not None:
        logo_size = 84
        logo = _LOGO.resize((logo_size, logo_size), Image.LANCZOS)
        lx, ly = panel_box[0] + 24, panel_box[3] - logo_size - 24
        shadow_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        silhouette = Image.new("RGBA", (logo_size, logo_size), (0, 0, 0, 0))
        silhouette.paste((0, 0, 0, 165), (0, 0, logo_size, logo_size), logo)
        shadow_layer.alpha_composite(silhouette, (lx, ly + 5))
        shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(7))
        img.alpha_composite(shadow_layer)
        img.alpha_composite(logo, (lx, ly))


# --------------------------------------------------------------------- #
# Orquestador SYNC (se ejecuta en un hilo aparte vía asyncio.to_thread)
# --------------------------------------------------------------------- #
def generate_ranking_image(
    period: str,
    entries: list[ActivityEntry],
    avatars: dict[int, Optional[Image.Image]],
    medal_images: dict[int, Optional[Image.Image]],
    footer_icons: dict[str, Optional[Image.Image]],
    members_count: Optional[int],
    total_messages: int,
    updated_label: str,
) -> Image.Image:
    img = draw_background()
    panel_box = (MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN)
    _draw_glass_panel(img, panel_box)

    draw = ImageDraw.Draw(img)
    content_box = (
        panel_box[0] + PANEL_PADDING, panel_box[1] + PANEL_PADDING,
        panel_box[2] - PANEL_PADDING, panel_box[3] - PANEL_PADDING,
    )

    rows_top = draw_header(img, draw, content_box, period)

    rows_bottom = content_box[3] - FOOTER_HEIGHT - 22
    row_h = 68
    available = rows_bottom - rows_top
    gap = max(4.0, (available - ROW_COUNT * row_h) / max(1, ROW_COUNT - 1))

    max_count = max((_count_for(e, period) for e in entries), default=0)
    y = float(rows_top)
    for rank in range(ROW_COUNT):
        if rank >= len(entries):
            break
        entry = entries[rank]
        count = _count_for(entry, period)
        draw_user(
            img, draw, rank, entry, count, max_count,
            avatars.get(entry.user_id), medal_images.get(rank),
            content_box, int(round(y)), row_h,
        )
        y += row_h + gap

    footer_box = (content_box[0], content_box[3] - FOOTER_HEIGHT, content_box[2], content_box[3])
    draw_footer(img, draw, footer_box, panel_box, members_count, total_messages, updated_label, footer_icons)

    return img.convert("RGB")


def _count_for(entry: ActivityEntry, period: str) -> int:
    if period == "today":
        return entry.today_messages
    if period == "week":
        return entry.week_messages
    return entry.total_messages


# --------------------------------------------------------------------- #
# Orquestador ASYNC: junta datos de Telegram/DB y despacha el dibujo
# --------------------------------------------------------------------- #
_MEDAL_EMOJIS = ["🥇", "🥈", "🥉"]
_FOOTER_EMOJIS = {"members": "👥", "messages": "💬", "calendar": "📅", "robot": "🤖"}


async def build_ranking_image(bot: Bot, db: Database, chat: Chat, period: str) -> Optional[io.BytesIO]:
    """Arma la imagen del ranking para `period` ('today' | 'week' | 'all').
    Devuelve None si todavía no hay ningún mensaje registrado en el grupo
    para ese período (el handler decide qué mostrar en ese caso)."""
    entries = await db.get_activity_ranking(chat.id, period, limit=ROW_COUNT)
    if not entries:
        return None

    avatar_tasks = [fetch_avatar(bot, e.user_id, AVATAR_SIZE) for e in entries]
    emoji_map_task = fetch_unicode_emoji_images(_MEDAL_EMOJIS + list(_FOOTER_EMOJIS.values()))

    avatars_list, emoji_map = await asyncio.gather(
        asyncio.gather(*avatar_tasks, return_exceptions=True), emoji_map_task,
    )
    avatars: dict[int, Optional[Image.Image]] = {}
    for entry, av in zip(entries, avatars_list):
        avatars[entry.user_id] = av if isinstance(av, Image.Image) else None

    medal_images = {i: emoji_map.get(_MEDAL_EMOJIS[i]) for i in range(3)}
    footer_icons = {key: emoji_map.get(emoji) for key, emoji in _FOOTER_EMOJIS.items()}

    try:
        members_count = await bot.get_chat_member_count(chat.id)
    except TelegramError:
        members_count = None
    _tracked_users, total_messages = await db.get_activity_group_totals(chat.id)

    updated_label = time.strftime("Hoy %H:%M")

    image = await asyncio.to_thread(
        generate_ranking_image, period, entries, avatars, medal_images, footer_icons,
        members_count, total_messages, updated_label,
    )

    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    buf.name = f"ranking_{period}.png"
    return buf
