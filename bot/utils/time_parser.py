"""
utils/time_parser.py
Convierte cadenas como '10m', '2h', '3d', '1w', '2mo', '1y' en timedelta.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Optional

# Importante: 'mo' se comprueba antes que 'm' para evitar ambigüedad.
_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "mo": 2592000,   # 30 días
    "y": 31536000,   # 365 días
}

_PATTERN = re.compile(r"^(\d+)(mo|[smhdwy])$", re.IGNORECASE)


def parse_duration(text: str) -> Optional[timedelta]:
    """
    Devuelve un timedelta si `text` coincide con el patrón de duración soportado.
    Devuelve None si no es una duración válida (ej. es parte del motivo).
    """
    if not text:
        return None
    match = _PATTERN.match(text.strip().lower())
    if not match:
        return None
    amount, unit = match.groups()
    seconds = int(amount) * _UNIT_SECONDS[unit]
    if seconds <= 0:
        return None
    return timedelta(seconds=seconds)
