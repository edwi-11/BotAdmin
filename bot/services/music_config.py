"""
services/music_config.py
Configuración propia de CEO Music. Lee el MISMO .env de la raíz del proyecto, pero no
importa config.py: así CEO Music no depende de las variables ni del arranque del bot
original (config.py exige BOT_TOKEN y OWNER_IDS al importarse).

Variables nuevas en .env:
    CEO_MUSIC_BOT_TOKEN, AUDIUS_API_KEY, AUDIUS_BEARER_TOKEN, JAMENDO_CLIENT_ID
(LOG_LEVEL, si existe, se reutiliza solo para leer el nivel de logging.)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class MusicSettings:
    bot_token: str
    audius_api_key: str
    audius_bearer_token: str
    jamendo_client_id: str
    log_level: str
    logs_dir: Path

    @property
    def audius_enabled(self) -> bool:
        return bool(self.audius_api_key or self.audius_bearer_token)

    @property
    def jamendo_enabled(self) -> bool:
        return bool(self.jamendo_client_id)

    def validate(self) -> None:
        if not self.bot_token or ":" not in self.bot_token:
            raise RuntimeError("Falta CEO_MUSIC_BOT_TOKEN en .env (token del bot CEO Music, de @BotFather).")
        # CEO Music debe tener un token propio, distinto al de los demás bots del proyecto.
        for var in ("BOT_TOKEN", "BROADCAST_BOT_TOKEN", "CLEANER_BOT_TOKEN"):
            other = os.getenv(var, "").strip()
            if other and other == self.bot_token:
                raise RuntimeError(f"CEO_MUSIC_BOT_TOKEN no puede ser el mismo token que {var}.")
        if not (self.audius_enabled or self.jamendo_enabled):
            raise RuntimeError(
                "No hay ningún proveedor musical configurado. Define AUDIUS_API_KEY / AUDIUS_BEARER_TOKEN "
                "y/o JAMENDO_CLIENT_ID en .env."
            )


def load_music_settings() -> MusicSettings:
    return MusicSettings(
        bot_token=os.getenv("CEO_MUSIC_BOT_TOKEN", "").strip(),
        audius_api_key=os.getenv("AUDIUS_API_KEY", "").strip(),
        audius_bearer_token=os.getenv("AUDIUS_BEARER_TOKEN", "").strip(),
        jamendo_client_id=os.getenv("JAMENDO_CLIENT_ID", "").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        logs_dir=BASE_DIR / "logs",
    )
