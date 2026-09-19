"""
utils/confession_image.py
Genera las dos imágenes del sistema de confesiones (handlers/confessions.py):

- `generate_announcement_image()`: el cartel que se manda al grupo cuando
  se activan las confesiones, explicando cómo mandar una.
- `generate_confession_image(text, numero)`: la tarjeta con el texto de
  una confesión ya enviada.

Reusa el fondo y la carga de fuentes de utils/ranking_image.py para que
todo el bot mantenga la misma identidad visual (mismo degradado oscuro
con destellos de color, misma tipografía Poppins con DejaVu de respaldo),
en vez de inventar un estilo aparte.

Igual que el generador del ranking, esto es código sincrónico y pesado
(Pillow): quien lo llame debe hacerlo con `asyncio.to_thread(...)` para
no bloquear el loop del bot.
"""
from __future__ import annotations

import logging
import textwrap

from PIL import Image, ImageDraw, ImageFont

from utils.ranking_image import (
    ACCENT_A,
    ACCENT_B,
    ACCENT_C,
    SIZE,
    TEXT_COLOR,
    TEXT_SECONDARY,
    _draw_glass_panel,
    _load_font,
    draw_background,
)

logger = logging.getLogger(__name__)

MARGIN = 56
PADDING = 64
# Máximo de caracteres que entran lindos en la tarjeta. Más largo que esto
# se recorta con "…" (handlers/confessions.py ya avisa al usuario antes de
# llegar acá, este es solo el corte defensivo final).
MAX_CHARS = 600


def _fit_font(
    text: str, max_width: int, max_height: int, draw: ImageDraw.ImageDraw,
    *, filename: str, start_size: int, min_size: int,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Busca el tamaño de fuente más grande con el que `text` entre en el
    espacio disponible, y devuelve (fuente, líneas ya cortadas). Así una
    confesión corta se ve grande y protagonista, y una larga se achica
    sola en vez de desbordarse."""
    size = start_size
    while size >= min_size:
        font = _load_font(filename, size)
        # Estimamos cuántos caracteres entran por línea a este tamaño
        # midiendo un carácter promedio, y después cortamos de verdad.
        avg_char = max(1, draw.textlength("x", font=font))
        chars_per_line = max(8, int(max_width / avg_char))
        lines = textwrap.wrap(text, width=chars_per_line) or [""]

        # Verificamos el ancho real línea por línea (textwrap trabaja con
        # cantidad de caracteres, no con píxeles).
        too_wide = any(draw.textlength(line, font=font) > max_width for line in lines)
        line_height = int(size * 1.45)
        total_height = line_height * len(lines)

        if not too_wide and total_height <= max_height:
            return font, lines
        size -= 4

    font = _load_font(filename, min_size)
    avg_char = max(1, draw.textlength("x", font=font))
    chars_per_line = max(8, int(max_width / avg_char))
    lines = textwrap.wrap(text, width=chars_per_line) or [""]
    max_lines = max(1, max_height // int(min_size * 1.45))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(0, len(lines[-1]) - 1)] + "…"
    return font, lines


def _draw_accent_bar(img: Image.Image, x: int, y: int, width: int, height: int) -> None:
    """Barrita de acento con degradado, como firma visual de la tarjeta."""
    bar = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    bd = ImageDraw.Draw(bar)
    for i in range(width):
        t = i / max(1, width - 1)
        if t < 0.5:
            u = t * 2
            color = tuple(int(ACCENT_A[c] + (ACCENT_C[c] - ACCENT_A[c]) * u) for c in range(3))
        else:
            u = (t - 0.5) * 2
            color = tuple(int(ACCENT_C[c] + (ACCENT_B[c] - ACCENT_C[c]) * u) for c in range(3))
        bd.line([(i, 0), (i, height)], fill=(*color, 255))
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, width - 1, height - 1), radius=height // 2, fill=255)
    img.paste(bar, (x, y), mask)


def generate_confession_image(text: str, numero: int) -> Image.Image:
    """Tarjeta con el texto de una confesión. `numero` es el correlativo
    de la confesión dentro de ese grupo (#1, #2, ...)."""
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 1].rstrip() + "…"

    img = draw_background()
    panel_box = (MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN)
    _draw_glass_panel(img, panel_box)
    draw = ImageDraw.Draw(img)

    left = panel_box[0] + PADDING
    right = panel_box[2] - PADDING
    content_width = right - left

    # --- Encabezado ---
    title_font = _load_font("Poppins-ExtraBold.ttf", 58)
    draw.text((left, panel_box[1] + PADDING), "CONFESIÓN", font=title_font, fill=TEXT_COLOR)

    num_font = _load_font("Poppins-SemiBold.ttf", 34)
    num_text = f"#{numero}"
    num_width = draw.textlength(num_text, font=num_font)
    draw.text((right - num_width, panel_box[1] + PADDING + 16), num_text, font=num_font, fill=TEXT_SECONDARY)

    bar_y = panel_box[1] + PADDING + 92
    _draw_accent_bar(img, left, bar_y, 180, 10)

    # --- Cuerpo: comillas + texto ---
    quote_font = _load_font("Poppins-ExtraBold.ttf", 130)
    quote_y = bar_y + 40
    draw.text((left - 8, quote_y), "“", font=quote_font, fill=(*ACCENT_A, 255))

    body_top = quote_y + 120
    footer_height = 90
    body_area_bottom = panel_box[3] - PADDING - footer_height
    body_max_height = body_area_bottom - body_top

    body_font, lines = _fit_font(
        text, content_width, body_max_height, draw,
        filename="Poppins-Medium.ttf", start_size=64, min_size=24,
    )
    line_height = int(body_font.size * 1.45)
    block_height = line_height * len(lines)

    # Centramos el bloque verticalmente en el espacio disponible: si no,
    # una confesión corta queda pegada arriba con medio cartel vacío
    # abajo, que se ve raro.
    y = body_top + max(0, (body_max_height - block_height) // 2)
    for line in lines:
        draw.text((left, y), line, font=body_font, fill=TEXT_COLOR)
        y += line_height

    # --- Pie ---
    foot_font = _load_font("Poppins-Medium.ttf", 26)
    foot_text = "Enviada de forma anónima"
    draw.text((left, panel_box[3] - PADDING - 34), foot_text, font=foot_font, fill=TEXT_SECONDARY)

    return img.convert("RGB")


def generate_announcement_image(group_title: str) -> Image.Image:
    """Cartel que se manda al grupo al activar las confesiones, con las
    instrucciones de cómo mandar una."""
    img = draw_background()
    panel_box = (MARGIN, MARGIN, SIZE - MARGIN, SIZE - MARGIN)
    _draw_glass_panel(img, panel_box)
    draw = ImageDraw.Draw(img)

    left = panel_box[0] + PADDING
    right = panel_box[2] - PADDING
    content_width = right - left

    title_font = _load_font("Poppins-ExtraBold.ttf", 66)
    draw.text((left, panel_box[1] + PADDING), "CONFESIONES", font=title_font, fill=TEXT_COLOR)

    bar_y = panel_box[1] + PADDING + 100
    _draw_accent_bar(img, left, bar_y, 200, 10)

    sub_font = _load_font("Poppins-Medium.ttf", 34)
    sub_avg_char = max(1, draw.textlength("x", font=sub_font))
    sub_text = f"Ya están activas en {group_title}"
    sub_lines = textwrap.wrap(sub_text, width=max(10, int(content_width / sub_avg_char))) or [""]
    y = bar_y + 44
    for line in sub_lines:
        draw.text((left, y), line, font=sub_font, fill=TEXT_SECONDARY)
        y += int(sub_font.size * 1.4)

    # --- Pasos ---
    step_num_font = _load_font("Poppins-ExtraBold.ttf", 40)
    step_font = _load_font("Poppins-Medium.ttf", 36)
    steps = [
        "Tocá el botón de abajo para abrir mi chat privado.",
        "Escribime ahí tu confesión, en un solo mensaje.",
        "La publico en el grupo de forma anónima: nadie va a saber que fuiste vos.",
    ]
    accents = [ACCENT_A, ACCENT_C, ACCENT_B]

    y += 46
    for idx, (step, accent) in enumerate(zip(steps, accents), start=1):
        circle_r = 26
        draw.ellipse((left, y, left + circle_r * 2, y + circle_r * 2), fill=(*accent, 255))
        num = str(idx)
        nw = draw.textlength(num, font=step_num_font)
        draw.text(
            (left + circle_r - nw / 2, y + circle_r - step_num_font.size * 0.62),
            num, font=step_num_font, fill=(15, 15, 18),
        )

        text_left = left + circle_r * 2 + 28
        text_width = right - text_left
        avg_char = max(1, draw.textlength("x", font=step_font))
        lines = textwrap.wrap(step, width=max(10, int(text_width / avg_char))) or [""]
        ty = y + 2
        for line in lines:
            draw.text((text_left, ty), line, font=step_font, fill=TEXT_COLOR)
            ty += int(step_font.size * 1.38)
        y = max(ty, y + circle_r * 2) + 34

    foot_font = _load_font("Poppins-Medium.ttf", 26)
    draw.text(
        (left, panel_box[3] - PADDING - 34),
        "Tu identidad nunca se muestra en el grupo",
        font=foot_font, fill=TEXT_SECONDARY,
    )

    return img.convert("RGB")
