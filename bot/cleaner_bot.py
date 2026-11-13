"""
cleaner_bot.py
Bot CEO CLEANER: proceso separado de CEO, con su propio token de BotFather.
Se queda permanentemente en los grupos (como administrador con el permiso
«Eliminar mensajes») y SOLO ejecuta limpiezas.

Qué NO hace: no tiene comandos ni menús, no decide horarios y no lee los
mensajes del grupo. CEO (main.py) guarda la configuración, vigila el horario y
le encola trabajos firmados en la tabla `cleaner_jobs` de la MISMA base de
datos (ver ceo_cleaner/shared.py y ceo_cleaner/worker.py).

Cómo correrlo:
    1. Crea un bot con @BotFather (token distinto al de CEO) y agrégalo a tus
       grupos como administrador con el permiso «Eliminar mensajes».
    2. En .env: CLEANER_BOT_TOKEN=... y CLEANER_SHARED_SECRET=... (ver LEEME_CEO_CLEANER.txt).
    3. python cleaner_bot.py   (o su propio servicio systemd)
"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from telegram import Update
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, ApplicationBuilder, ChatMemberHandler, ContextTypes

from ceo_cleaner.shared import PENDING_TTL_SECONDS
from ceo_cleaner.worker import POLL_SECONDS, poll_jobs
from config import settings
from database import Database

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        settings.logs_dir / "cleaner.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
    # httpx registra las URLs de la API (que contienen el token): se silencia.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def post_init(application: Application) -> None:
    db = Database(settings.database_path)
    await db.connect()
    application.bot_data["db"] = db
    application.bot_data["me_id"] = (await application.bot.get_me()).id
    application.bot_data["active_jobs"] = 0
    # Único ejecutor: todo trabajo "running" que quedó de antes se interrumpió.
    await db.expire_cleaner_jobs(PENDING_TTL_SECONDS, 0)
    application.job_queue.run_repeating(poll_jobs, interval=POLL_SECONDS, first=3, name="cleaner_poll")
    logger.info("CEO Cleaner inicializado (misma base de datos que CEO).")


async def post_shutdown(application: Application) -> None:
    db: Database | None = application.bot_data.get("db")
    if db is not None:
        await db.close()


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.my_chat_member
    if change is None:
        return
    new = change.new_chat_member
    can_delete = bool(getattr(new, "can_delete_messages", False))
    logger.info(
        "[grupo=%s] Estado de CEO Cleaner: %s (eliminar mensajes: %s)",
        change.chat.id, new.status, can_delete if new.status == ChatMemberStatus.ADMINISTRATOR else "no aplica",
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Excepción no controlada en CEO Cleaner", exc_info=context.error)


def main() -> None:
    _setup_logging()
    if not settings.cleaner_bot_token:
        raise SystemExit("❌ Falta CLEANER_BOT_TOKEN en .env (token del bot CEO Cleaner, de @BotFather).")
    if not settings.cleaner_secret:
        raise SystemExit("❌ Falta CLEANER_SHARED_SECRET en .env (mismo valor que usa CEO).")
    if settings.cleaner_bot_token == settings.bot_token:
        raise SystemExit("❌ CLEANER_BOT_TOKEN no puede ser el mismo token que BOT_TOKEN.")

    application = (
        ApplicationBuilder()
        .token(settings.cleaner_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    application.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_error_handler(error_handler)
    logger.info("Iniciando CEO Cleaner en modo polling...")
    # Solo cambios de su propia membresía: no procesa mensajes de los grupos.
    application.run_polling(allowed_updates=["my_chat_member"])


if __name__ == "__main__":
    main()
