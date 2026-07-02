"""
utils/formatting.py
Helpers para escapar y construir mensajes en formato MarkdownV2 con emojis,
de forma consistente en todo el bot.
"""
from __future__ import annotations

import html
import re

_MDV2_SPECIAL = r"_*[]()~`>#+-=|{}.!"


def escape_md(text: str | int | None) -> str:
    """Escapa cualquier texto para que sea seguro en MarkdownV2."""
    if text is None:
        return ""
    text = str(text)
    return re.sub(f"([{re.escape(_MDV2_SPECIAL)}])", r"\\\1", text)


def mention(user_id: int, name: str) -> str:
    """Genera un enlace de mención MarkdownV2 seguro (funciona sin @username)."""
    return f"[{escape_md(name)}](tg://user?id={user_id})"


def humanize_seconds(seconds: int) -> str:
    """Convierte segundos a una duración legible (segundos, minutos, horas, días, semanas, meses, años)."""
    intervals = (
        ("año", "años", 31536000),
        ("mes", "meses", 2592000),
        ("semana", "semanas", 604800),
        ("día", "días", 86400),
        ("hora", "horas", 3600),
        ("minuto", "minutos", 60),
        ("segundo", "segundos", 1),
    )
    if seconds < 1:
        return "0 segundos"

    parts: list[str] = []
    remaining = seconds
    for singular, plural, unit in intervals:
        value, remaining = divmod(remaining, unit)
        if value > 0:
            label = singular if value == 1 else plural
            parts.append(f"{value} {label}")
        if len(parts) == 2:
            break
    return ", ".join(parts) if parts else "0 segundos"


def success(text: str) -> str:
    return f"✅ {text}"


def error(text: str) -> str:
    return f"❌ {text}"


def warning(text: str) -> str:
    return f"⚠️ {text}"


def info_box(title: str, lines: list[str]) -> str:
    body = "\n".join(lines)
    return f"*{escape_md(title)}*\n{body}"


# --------------------------------------------------------------------- #
# Plantillas personalizables (bienvenida / despedida / reglamento)
# --------------------------------------------------------------------- #
# Se usa parse_mode HTML para estos mensajes porque permite escapar el
# texto libre del usuario de forma segura y luego insertar menciones
# (enlaces tg://user) sin arriesgar romper el formato ni el escape de
# MarkdownV2, que es mucho más estricto con caracteres especiales.

TEMPLATE_PLACEHOLDERS = ("{name}", "{mention}", "{username}", "{id}", "{group}")


def render_template(template: str, *, user_id: int, first_name: str,
                     username: str | None, group_title: str | None) -> str:
    """
    Escapa el texto libre del usuario para HTML y sustituye los
    placeholders soportados: {name} {mention} {username} {id} {group}
    """
    escaped = html.escape(template)
    mention_html = f'<a href="tg://user?id={user_id}">{html.escape(first_name)}</a>'
    replacements = {
        "{name}": html.escape(first_name),
        "{mention}": mention_html,
        "{username}": f"@{username}" if username else "sin usuario",
        "{id}": str(user_id),
        "{group}": html.escape(group_title or ""),
    }
    for placeholder, value in replacements.items():
        escaped = escaped.replace(placeholder, value)
    return escaped
