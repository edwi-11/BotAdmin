"""
ceo_cleaner/shared.py
Piezas compartidas entre CEO (menú + scheduler) y CEO Cleaner (ejecución).
No importa nada de Telegram: son funciones puras y fáciles de probar.

Comunicación entre ambos bots: tabla `cleaner_jobs` de la MISMA base de datos
(igual que el bot anunciador usa `broadcast_queue`). Cada trabajo lleva una
firma HMAC-SHA256 calculada con CLEANER_SHARED_SECRET; CEO Cleaner solo
ejecuta trabajos cuya firma sea válida, así una fila inventada o alterada en
la base de datos nunca dispara una limpieza.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "America/Managua"
DEFAULT_START_HOUR = 3
DEFAULT_START_MINUTE = 0
DEFAULT_INTERVAL_MINUTES = 1440

# (minutos, etiqueta) — opciones del botón "Frecuencia".
FREQUENCIES: list[tuple[int, str]] = [
    (60, "Cada hora"),
    (180, "Cada 3 horas"),
    (360, "Cada 6 horas"),
    (720, "Cada 12 horas"),
    (1440, "Cada 24 horas"),
    (2880, "Cada 2 días"),
    (4320, "Cada 3 días"),
    (10080, "Cada semana"),
]

# Estados de un trabajo en `cleaner_jobs`.
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

# Si CEO Cleaner no toma un trabajo en este tiempo, se cancela (no se reintenta
# hasta la siguiente hora programada).
PENDING_TTL_SECONDS = 15 * 60
# Un trabajo "running" más viejo que esto se considera interrumpido.
RUNNING_TTL_SECONDS = 6 * 3600

_WEEKDAYS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")


# --------------------------------------------------------------------- #
# Zona horaria y formato
# --------------------------------------------------------------------- #
def get_tz(name: Optional[str]) -> tzinfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:
        # Managua no usa horario de verano: UTC-6 fijo si no hay base de zonas.
        if (name or DEFAULT_TIMEZONE) == DEFAULT_TIMEZONE:
            return timezone(timedelta(hours=-6))
        return timezone.utc


def fmt_time(hour: int, minute: int) -> str:
    return f"{hour:02d}:{minute:02d}"


def fmt_local(epoch: int, tz_name: Optional[str]) -> str:
    dt = datetime.fromtimestamp(epoch, get_tz(tz_name))
    return f"{_WEEKDAYS[dt.weekday()]} {dt.day} {_MONTHS[dt.month - 1]}, {dt:%H:%M}"


def frequency_label(minutes: int) -> str:
    for value, label in FREQUENCIES:
        if value == minutes:
            return label
    if minutes % 1440 == 0:
        return f"Cada {minutes // 1440} días"
    if minutes % 60 == 0:
        return f"Cada {minutes // 60} horas"
    return f"Cada {minutes} minutos"


# --------------------------------------------------------------------- #
# Programación
# --------------------------------------------------------------------- #
def compute_next_run(
    start_hour: int, start_minute: int, interval_minutes: int,
    tz_name: Optional[str] = None, now: Optional[int] = None,
) -> int:
    """Próxima ejecución (epoch UTC) tras activar o cambiar la configuración.

    - Frecuencia de 24 h o más: la próxima vez que el reloj marque la hora de
      inicio (hoy si todavía no pasó, si no mañana). Después se repite cada
      `interval_minutes` (ver advance_next_run).
    - Frecuencia menor a 24 h: las ejecuciones caen sobre la cuadrícula que
      arranca a la hora de inicio de cada día (ej. 03:00 cada 6 h -> 03:00,
      09:00, 15:00, 21:00).
    """
    tz = get_tz(tz_name)
    now_epoch = int(now if now is not None else time.time())
    now_local = datetime.fromtimestamp(now_epoch, tz)
    today = now_local.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    step = max(1, interval_minutes) * 60

    if interval_minutes >= 1440:
        target = today if today > now_local else today + timedelta(days=1)
        return int(target.timestamp())

    anchor = today if today <= now_local else today - timedelta(days=1)
    anchor_epoch = int(anchor.timestamp())
    n = (now_epoch - anchor_epoch) // step + 1
    return anchor_epoch + n * step


def advance_next_run(prev_next_run: int, interval_minutes: int, now: Optional[int] = None) -> int:
    """Siguiente ejecución a partir de la anterior, sin acumular desfase y sin
    'recuperar' ejecuciones perdidas (si CEO estuvo apagado, salta a la
    próxima hora futura de la cuadrícula)."""
    now_epoch = int(now if now is not None else time.time())
    step = max(1, interval_minutes) * 60
    if prev_next_run > now_epoch:
        return prev_next_run
    return prev_next_run + ((now_epoch - prev_next_run) // step + 1) * step


def late_grace_seconds(interval_minutes: int) -> int:
    """Cuánto se tolera de retraso (ej. CEO apagado a la hora programada)
    antes de omitir esa ejecución en vez de limpiar a deshoras."""
    return min(2 * 3600, max(60, interval_minutes * 60 // 2))


# --------------------------------------------------------------------- #
# Firma de trabajos (CEO -> CEO Cleaner)
# --------------------------------------------------------------------- #
def new_nonce() -> str:
    return secrets.token_hex(16)


def _payload(group_id: int, requested_at: int, nonce: str) -> bytes:
    return f"cleaner-job|{group_id}|{requested_at}|{nonce}".encode("utf-8")


def sign_job(secret: str, group_id: int, requested_at: int, nonce: str) -> str:
    return hmac.new(secret.encode("utf-8"), _payload(group_id, requested_at, nonce), hashlib.sha256).hexdigest()


def verify_job(secret: str, group_id: int, requested_at: int, nonce: str, signature: str) -> bool:
    if not secret or not signature:
        return False
    expected = sign_job(secret, group_id, requested_at, nonce)
    return hmac.compare_digest(expected, signature)
