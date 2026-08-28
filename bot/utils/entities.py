"""
utils/entities.py
Serializa y reconstruye `MessageEntity` de Telegram (negritas, cursivas,
enlaces y también emojis premium `custom_emoji`) para poder guardarlos en
la base de datos y reenviarlos exactamente igual más adelante.

Emojis premium ("custom_emoji"): Telegram permite que un bot envíe
entidades `custom_emoji` sin restricciones siempre que la cuenta que creó
el bot en BotFather tenga Telegram Premium. Como el bot NO vuelve a
"interpretar" el texto (no usa parse_mode), sino que reenvía las entidades
tal cual llegaron en el mensaje original del administrador, cualquier
emoji premium que el owner (con Premium) haya usado al definir un mensaje
recurrente se conserva perfectamente.

También incluye la sintaxis simple de botones en línea usada en el menú
de mensajes recurrentes y en el compositor de anuncios:

    Texto del botón - https://enlace.com
    Botón A - https://a.com | Botón B - https://b.com

Cada línea es una fila del teclado; separar varios botones de la misma
fila con " | ".

Colores de botón (desde Bot API 9.4, feb 2026): se puede anteponer una
etiqueta de color entre corchetes antes del texto del botón:

    [rojo] Cancelar - https://a.com
    [verde] Confirmar - https://b.com | [azul] Info - https://c.com

Si no se pone ninguna etiqueta, el botón queda con el estilo por defecto
de Telegram (gris/celeste, según el cliente).
"""
from __future__ import annotations

import json
import re
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.constants import MessageEntityType


# --------------------------------------------------------------------- #
# Entities (formato de texto + emojis premium)
# --------------------------------------------------------------------- #
def entities_to_json(entities: Optional[list[MessageEntity]]) -> str:
    if not entities:
        return "[]"
    data = []
    for e in entities:
        d = {"type": e.type, "offset": e.offset, "length": e.length}
        if e.url:
            d["url"] = e.url
        if e.language:
            d["language"] = e.language
        if e.custom_emoji_id:
            d["custom_emoji_id"] = e.custom_emoji_id
        data.append(d)
    return json.dumps(data, ensure_ascii=False)


def json_to_entities(raw: Optional[str]) -> list[MessageEntity]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    entities: list[MessageEntity] = []
    for d in data:
        try:
            entities.append(
                MessageEntity(
                    type=d["type"],
                    offset=d["offset"],
                    length=d["length"],
                    url=d.get("url"),
                    language=d.get("language"),
                    custom_emoji_id=d.get("custom_emoji_id"),
                )
            )
        except (KeyError, TypeError):
            continue
    return entities


def count_premium_emojis(entities: Optional[list[MessageEntity]]) -> int:
    if not entities:
        return 0
    return sum(1 for e in entities if e.type == MessageEntityType.CUSTOM_EMOJI)


# --------------------------------------------------------------------- #
# Botones en línea
# --------------------------------------------------------------------- #
# Alias en español/inglés -> valor real que espera la Bot API (Bot API
# 9.4+): 'primary' (azul), 'success' (verde), 'danger' (rojo).
BUTTON_COLOR_ALIASES = {
    "rojo": "danger", "red": "danger", "danger": "danger",
    "verde": "success", "green": "success", "success": "success",
    "azul": "primary", "blue": "primary", "primary": "primary",
}
BUTTON_COLOR_EMOJI = {"danger": "🔴", "success": "🟢", "primary": "🔵"}
_COLOR_TAG_RE = re.compile(r"^\[\s*(\w+)\s*\]\s*")


def parse_buttons_text(text: str) -> tuple[list[list[dict]], list[str]]:
    """Convierte texto plano en filas de botones. Devuelve (filas, errores).
    Acepta un color opcional al principio de cada botón, entre corchetes:
    '[rojo] Texto - url'. Ver BUTTON_COLOR_ALIASES para las palabras
    válidas (rojo/red/danger, verde/green/success, azul/blue/primary)."""
    rows: list[list[dict]] = []
    errors: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        row: list[dict] = []
        for raw_btn in line.split("|"):
            raw_btn = raw_btn.strip()
            if not raw_btn:
                continue

            style = None
            match = _COLOR_TAG_RE.match(raw_btn)
            if match:
                tag = match.group(1).lower()
                if tag in BUTTON_COLOR_ALIASES:
                    style = BUTTON_COLOR_ALIASES[tag]
                    raw_btn = raw_btn[match.end():].strip()
                else:
                    errors.append(
                        f"«[{match.group(1)}]» no es un color válido. Usá rojo, verde o azul."
                    )
                    continue

            if " - " not in raw_btn:
                errors.append(f"«{raw_btn}» — falta \" - \" entre el texto y el enlace.")
                continue
            label, url = raw_btn.split(" - ", 1)
            label, url = label.strip(), url.strip()
            if not label:
                errors.append("Uno de los botones no tiene texto.")
                continue
            if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
                errors.append(f"«{label}» — el enlace debe empezar con http://, https:// o tg://.")
                continue
            btn = {"text": label, "url": url}
            if style:
                btn["style"] = style
            row.append(btn)
        if row:
            rows.append(row)
    return rows, errors


def buttons_to_json(rows: list[list[dict]]) -> str:
    return json.dumps(rows, ensure_ascii=False)


def json_to_buttons(raw: Optional[str]) -> list[list[dict]]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def build_inline_keyboard(rows: list[list[dict]]) -> Optional[InlineKeyboardMarkup]:
    if not rows:
        return None
    keyboard = [
        [InlineKeyboardButton(b["text"], url=b["url"], style=b.get("style")) for b in row]
        for row in rows
    ]
    return InlineKeyboardMarkup(keyboard)


def describe_buttons(rows: list[list[dict]]) -> str:
    if not rows:
        return "Sin botones."
    lines = []
    for row in rows:
        parts = []
        for b in row:
            emoji = BUTTON_COLOR_EMOJI.get(b.get("style"), "")
            prefix = f"{emoji} " if emoji else ""
            parts.append(f"{prefix}{b['text']} → {b['url']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)
