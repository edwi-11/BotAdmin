"""
broadcast_bot.py
Bot ANUNCIADOR: proceso separado del bot de moderación, con su propio
token de BotFather. Solo lo usa el/los propietario(s) (OWNER_IDS en
.env, el mismo valor que usa el bot de moderación).

Qué hace:
- /start o /anuncio abre un editor por botones (foto, texto, botones,
  vista previa) para componer un anuncio.
- Al confirmar "📢 Enviar a todos los grupos", el anuncio se guarda en la
  tabla `broadcast_queue` de la MISMA base de datos que usa el bot de
  moderación (mismo archivo DATABASE_PATH).

Qué NO hace:
- No envía nada directamente a los grupos. No necesita ser miembro de
  ellos. El envío real lo hace el bot de moderación (main.py), que ya
  está presente y con permisos en todos los grupos, revisando la cola
  cada cierto tiempo (ver `_broadcast_dispatch_job` en main.py).

Cómo correrlo:
    1. Crea un segundo bot con @BotFather (token distinto al de moderación).
    2. En tu .env agrega:  BROADCAST_BOT_TOKEN=el_token_del_bot_anunciador
    3. Corre:  python broadcast_bot.py   (o su propio servicio systemd)
"""
from __future__ import annotations

import logging

from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import settings
from database import Database
from handlers.broadcast import broadcast_callback, broadcast_command, try_consume_broadcast_input
from utils.logger import setup_logging

logger = logging.getLogger(__name__)


async def post_init(application) -> None:
    db = Database(settings.database_path)
    await db.connect()
    application.bot_data["db"] = db
    logger.info("Bot anunciador inicializado. Usa la misma base de datos que el bot de moderación.")


async def post_shutdown(application) -> None:
    db: Database | None = application.bot_data.get("db")
    if db is not None:
        await db.close()


async def on_message(update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await try_consume_broadcast_input(update, context)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Excepción no controlada en el bot anunciador: %s", update, exc_info=context.error)


def main() -> None:
    setup_logging()
    if not settings.broadcast_bot_token:
        raise SystemExit(
            "❌ Falta BROADCAST_BOT_TOKEN en tu archivo .env. "
            "Crea un segundo bot con @BotFather, copia su token, y agrégalo así:\n"
            "BROADCAST_BOT_TOKEN=123456:ABC-tu-token"
        )

    application = (
        ApplicationBuilder()
        .token(settings.broadcast_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", broadcast_command))
    application.add_handler(CommandHandler("anuncio", broadcast_command))
    application.add_handler(CallbackQueryHandler(broadcast_callback, pattern=r"^b:"))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, on_message))
    application.add_error_handler(error_handler)

    logger.info("Iniciando bot anunciador en modo polling...")
    application.run_polling()


if __name__ == "__main__":
    main()
