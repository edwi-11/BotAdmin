"""
utils/logger.py
Configura el sistema de logging del bot: salida a consola y a archivo rotativo.
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from config import settings


def setup_logging() -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = settings.logs_dir / "bot.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    # Silenciar librerías demasiado verbosas
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
