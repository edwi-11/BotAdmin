"""
main.py
Punto de entrada del bot de moderación.

Comandos con "/" disponibles (a propósito, muy pocos): /start /menu para
abrir el panel de configuración por botones, y los comandos básicos de
moderación /admin /unadmin /warn /unwarn /ban /unban /kick /mute /unmute.
Todo lo demás (bienvenida, despedida, reglamento, palabras prohibidas,
mensajes recurrentes, auto-eliminar, advertencias) se configura desde el
menú de botones (/menu), sin necesidad de recordar ningún comando.
"""
from __future__ import annotations

import logging

from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import settings
from database import Database
from handlers.admin import admin_command, unadmin_command
from handlers.afk import brb_text_trigger, load_afk_cache, track_and_check_afk
from handlers.cleanup import (
    cleanup_menu_callback,
    on_call_started_cleanup,
    on_command_cleanup,
    on_join_cleanup,
    on_leave_cleanup,
)
from handlers.filters_words import check_banned_words, try_consume_pending_words, words_menu_callback
from handlers.menu import menu_callback, menu_command, try_consume_pending_input
from handlers.greetings import on_left_member, on_new_members
from handlers.moderation import (
    ban_command,
    kick_command,
    mute_command,
    unban_command,
    unmute_command,
    unwarn_command,
    warn_command,
)
from handlers.recurring import load_all_recurring_jobs, recurring_callback, try_consume_draft_input
from handlers.warnings import warnings_callback
from utils.logger import setup_logging

logger = logging.getLogger(__name__)

# Lista deliberadamente corta: solo lo esencial. El resto de funciones
# (bienvenida, despedida, reglas, recurrentes, palabras, auto-eliminar,
# advertencias) vive exclusivamente en el menú de botones (/menu).
BOT_COMMANDS = [
    BotCommand("start", "Abrir el menú de configuración"),
    BotCommand("menu", "Abrir el menú de configuración"),
    BotCommand("admin", "Otorgar administración"),
    BotCommand("unadmin", "Revocar administración"),
    BotCommand("warn", "Advertir a un usuario"),
    BotCommand("unwarn", "Quitar una advertencia"),
    BotCommand("ban", "Banear a un usuario"),
    BotCommand("unban", "Desbanear a un usuario"),
    BotCommand("kick", "Expulsar a un usuario"),
    BotCommand("mute", "Silenciar a un usuario (permanente o temporal)"),
    BotCommand("unmute", "Quitar el silencio a un usuario"),
]


async def post_init(application: Application) -> None:
    db = Database(settings.database_path)
    await db.connect()
    application.bot_data["db"] = db
    application.bot_data["afk_cache"] = await load_afk_cache(db)
    await application.bot.set_my_commands(BOT_COMMANDS)
    await load_all_recurring_jobs(application, db)
    logger.info("Bot inicializado correctamente. Propietarios: %s", list(settings.owner_ids))


async def post_shutdown(application: Application) -> None:
    db: Database | None = application.bot_data.get("db")
    if db is not None:
        await db.close()


async def on_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Router para mensajes sin comando (texto o media):
    1) Si el editor de mensajes recurrentes está esperando un campo
       (foto/texto/botones), se consume aquí.
    2) Si el usuario está agregando/eliminando palabras prohibidas, se consume aquí.
    3) Si el usuario tiene una edición pendiente desde el menú de botones
       (ej. estaba escribiendo el nuevo mensaje de bienvenida), se consume aquí.
    4) Si no, se comprueba si el mensaje es un disparador "brb" en texto plano.
    """
    if await try_consume_draft_input(update, context):
        return
    if await try_consume_pending_words(update, context):
        return
    if await try_consume_pending_input(update, context):
        return
    await brb_text_trigger(update, context)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Excepción no controlada al procesar un update: %s", update, exc_info=context.error)


def build_application() -> Application:
    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # --- Menú de configuración con botones (todo pasa por aquí) ---
    application.add_handler(CommandHandler("start", menu_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^m:"))
    application.add_handler(CallbackQueryHandler(recurring_callback, pattern=r"^r:"))
    application.add_handler(CallbackQueryHandler(words_menu_callback, pattern=r"^w:"))
    application.add_handler(CallbackQueryHandler(cleanup_menu_callback, pattern=r"^c:"))
    application.add_handler(CallbackQueryHandler(warnings_callback, pattern=r"^aw:"))

    # --- Moderación básica (los únicos comandos "/" que quedan) ---
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("kick", kick_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CommandHandler("unwarn", unwarn_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("unadmin", unadmin_command))

    # Router de mensajes libres (texto o media): editor de recurrentes,
    # wizard de palabras prohibidas, ediciones pendientes del menú y "brb".
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.ALL, on_message_router)
    )

    # Eventos de ingreso/salida de miembros (mensajes de servicio de Telegram)
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    application.add_handler(MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_left_member))

    # --- Auto-eliminar mensajes de servicio (grupo aparte para no pisar
    # los handlers de bienvenida/despedida, que publican el aviso propio) ---
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_join_cleanup), group=2
    )
    application.add_handler(
        MessageHandler(filters.StatusUpdate.LEFT_CHAT_MEMBER, on_leave_cleanup), group=2
    )
    application.add_handler(
        MessageHandler(filters.StatusUpdate.VIDEO_CHAT_STARTED, on_call_started_cleanup), group=2
    )

    # --- Auto-eliminar mensajes con comandos "/" (se ejecuta DESPUÉS de que
    # el comando ya fue procesado por su handler correspondiente) ---
    application.add_handler(
        MessageHandler(filters.COMMAND & filters.ChatType.GROUPS, on_command_cleanup), group=5
    )

    # --- Filtro de palabras prohibidas: revisa cada mensaje de texto/caption
    # de grupo antes que cualquier otra cosa (grupo -2, corre primero) ---
    application.add_handler(
        MessageHandler((filters.TEXT | filters.CAPTION) & filters.ChatType.GROUPS, check_banned_words),
        group=-2,
    )

    # Middleware AFK / caché de usuarios: se ejecuta en un grupo anterior (-1)
    # para procesar el estado AFK antes que cualquier comando.
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, track_and_check_afk),
        group=-1,
    )

    application.add_error_handler(error_handler)
    return application


def main() -> None:
    setup_logging()
    application = build_application()
    logger.info("Iniciando bot en modo polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
