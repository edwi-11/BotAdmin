"""
config.py
Carga y valida la configuración del bot a partir de variables de entorno (.env).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent


def _parse_owner_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk:
            try:
                ids.add(int(chunk))
            except ValueError:
                logging.getLogger(__name__).warning(
                    "OWNER_IDS contiene un valor no numérico ignorado: %s", chunk
                )
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    owner_ids: frozenset[int]
    database_path: str
    log_level: str
    del_notice_seconds: int
    logs_dir: Path = field(default_factory=lambda: BASE_DIR / "logs")
    broadcast_bot_token: str = ""  # Token del bot anunciador (opcional, ver broadcast_bot.py)

    def validate(self) -> None:
        if not self.bot_token or ":" not in self.bot_token:
            raise RuntimeError(
                "BOT_TOKEN no está configurado correctamente en el archivo .env"
            )
        if not self.owner_ids:
            raise RuntimeError(
                "OWNER_IDS no está configurado. Define al menos un propietario en .env"
            )


def load_settings() -> Settings:
    raw_owner_ids = os.getenv("OWNER_IDS", "")
    database_path = os.getenv("DATABASE_PATH", "database/bot.db")

    # Aseguramos que el directorio de la base de datos exista
    db_full_path = BASE_DIR / database_path
    db_full_path.parent.mkdir(parents=True, exist_ok=True)

    settings = Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        owner_ids=_parse_owner_ids(raw_owner_ids),
        database_path=str(db_full_path),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        del_notice_seconds=int(os.getenv("DEL_NOTICE_SECONDS", "10")),
        broadcast_bot_token=os.getenv("BROADCAST_BOT_TOKEN", "").strip(),
    )
    settings.validate()
    return settings


settings = load_settings()
