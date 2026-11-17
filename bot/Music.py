"""
Music.py
Punto de entrada EXCLUSIVO de CEO Music: bot de Telegram de música, independiente del
bot original (main.py). Tiene su propio token (CEO_MUSIC_BOT_TOKEN), su propia
Application de python-telegram-bot (Bot + Dispatcher propios), sus handlers, su polling
y su proceso. Puede correr al mismo tiempo que main.py, o solo.

Cómo correrlo (desde la raíz del proyecto, con el mismo entorno virtual):
    python Music.py

Variables en el .env compartido:
    CEO_MUSIC_BOT_TOKEN=   AUDIUS_API_KEY=   AUDIUS_BEARER_TOKEN=   JAMENDO_CLIENT_ID=
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from telegram import BotCommand, LinkPreviewOptions
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, ContextTypes, Defaults

from handlers.music_bot import register_music_handlers
from services.music_config import MusicSettings, load_music_settings
from services.music_service import MusicService

logger = logging.getLogger("ceo_music")


def _setup_logging(settings: MusicSettings) -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        settings.logs_dir / "music.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
    # httpx registra las URLs de las peticiones (que pueden llevar el token o API keys): se silencia.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def post_init(application: Application) -> None:
    settings: MusicSettings = application.bot_data["music_settings"]
    application.bot_data["music_service"] = MusicService(settings)
    try:
        await application.bot.set_my_commands([
            BotCommand("buscar", "Buscar una canción o artista"),
            BotCommand("tendencias", "Lo más escuchado de la semana"),
            BotCommand("help", "Ayuda"),
        ])
    except Exception:  # No es crítico para el funcionamiento
        logger.warning("No se pudo registrar la lista de comandos del bot.", exc_info=True)
    me = await application.bot.get_me()
    logger.info("CEO Music inicializado como @%s", me.username)


async def post_shutdown(application: Application) -> None:
    service: MusicService | None = application.bot_data.get("music_service")
    if service is not None:
        await service.close()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Excepción no controlada en CEO Music", exc_info=context.error)


def main() -> None:
    settings = load_music_settings()
    _setup_logging(settings)
    try:
        settings.validate()
    except RuntimeError as exc:
        raise SystemExit(f"❌ {exc}")

    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .defaults(Defaults(
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        ))
        .concurrent_updates(True)  # Las descargas de audio no deben bloquear otras búsquedas
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.bot_data["music_settings"] = settings

    provider_names = [name for name, on in (("audius", settings.audius_enabled), ("jamendo", settings.jamendo_enabled)) if on]
    register_music_handlers(application, provider_names)
    application.add_error_handler(error_handler)

    logger.info("Iniciando CEO Music en modo polling (fuentes: %s)...", ", ".join(provider_names))
    application.run_polling(allowed_updates=["message", "callback_query"], drop_pending_updates=True)


if __name__ == "__main__":
    main()
