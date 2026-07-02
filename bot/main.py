"""
main.py
Punto de entrada del bot de moderación.
"""
from __future__ import annotations

import logging
import asyncio

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
from handlers.admin import admin_command, admins_command, staff_command, unadmin_command
from handlers.afk import brb_command, brb_text_trigger, load_afk_cache, track_and_check_afk
from handlers.cleanup import (
    cleanup_menu_callback,
    on_call_started_cleanup,
    on_command_cleanup,
    on_join_cleanup,
    on_leave_cleanup,
)
from handlers.filters_words import (
    add_word_command,
    check_banned_words,
    list_words_command,
    remove_word_command,
    try_consume_pending_words,
    words_menu_callback,
)
from handlers.menu import menu_callback, menu_command, try_consume_pending_input
from handlers.greetings import (
    goodbye_command,
    on_left_member,
    on_new_members,
    resetgoodbye_command,
    resetrules_command,
    resetwelcome_command,
    rules_command,
    setgoodbye_command,
    setrules_command,
    setwelcome_command,
    welcome_command,
    welcomeclean_command,
)
from handlers.moderation import (
    ban_command,
    kick_command,
    mute_command,
    unban_command,
    unmute_command,
)
from handlers.recurring import (
    addrecurring_command,
    load_all_recurring_jobs,
    recurring_callback,
    recurring_list_command,
    try_consume_pending_recurring,
)
from handlers.utils_cmds import del_command, id_command, info_command, ping_command
from utils.logger import setup_logging

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "Abrir el menú de configuración"),
    BotCommand("menu", "Abrir el menú de configuración"),
    BotCommand("ban", "Banear a un usuario"),
    BotCommand("kick", "Expulsar a un usuario"),
    BotCommand("mute", "Silenciar a un usuario (permanente o temporal)"),
    BotCommand("unmute", "Quitar el silencio a un usuario"),
    BotCommand("unban", "Desbanear a un usuario"),
    BotCommand("admin", "Otorgar administración"),
    BotCommand("unadmin", "Revocar administración"),
    BotCommand("del", "Eliminar un mensaje"),
    BotCommand("id", "Mostrar ID de usuario/grupo"),
    BotCommand("admins", "Listar administradores"),
    BotCommand("staff", "Mostrar staff del grupo"),
    BotCommand("ping", "Comprobar latencia del bot"),
    BotCommand("info", "Información de un usuario"),
    BotCommand("brb", "Activar AFK (o solo escribe: brb)"),
    BotCommand("setwelcome", "Definir mensaje de bienvenida"),
    BotCommand("welcome", "Activar/desactivar bienvenida"),
    BotCommand("resetwelcome", "Restablecer mensaje de bienvenida"),
    BotCommand("setgoodbye", "Definir mensaje de despedida"),
    BotCommand("goodbye", "Activar/desactivar despedida"),
    BotCommand("resetgoodbye", "Restablecer mensaje de despedida"),
    BotCommand("welcomeclean", "Auto-borrar bienvenida anterior"),
    BotCommand("setrules", "Definir el reglamento del grupo"),
    BotCommand("rules", "Mostrar el reglamento del grupo"),
    BotCommand("resetrules", "Borrar el reglamento configurado"),
    BotCommand("addrecurrente", "Crear un mensaje recurrente"),
    BotCommand("recurrentes", "Listar mensajes recurrentes"),
    BotCommand("agregarpalabra", "Agregar palabra(s) prohibida(s)"),
    BotCommand("eliminarpalabra", "Eliminar palabra(s) prohibida(s)"),
    BotCommand("palabras", "Listar palabras prohibidas"),
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
    1) Si el usuario está definiendo el contenido de un mensaje recurrente
       (texto, foto, video, etc.), se consume aquí.
    2) Si el usuario está agregando/eliminando palabras prohibidas, se consume aquí.
    3) Si el usuario tiene una edición pendiente desde el menú de botones
       (ej. estaba escribiendo el nuevo mensaje de bienvenida), se consume aquí.
    4) Si no, se comprueba si el mensaje es un disparador "brb" en texto plano.
    """
    if await try_consume_pending_recurring(update, context):
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

    # --- Menú de configuración con botones ---
    application.add_handler(CommandHandler("start", menu_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^m:"))
    application.add_handler(CallbackQueryHandler(recurring_callback, pattern=r"^r:"))
    application.add_handler(CallbackQueryHandler(words_menu_callback, pattern=r"^w:"))
    application.add_handler(CallbackQueryHandler(cleanup_menu_callback, pattern=r"^c:"))

    # --- Moderación ---
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("kick", kick_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("unban", unban_command))

    # --- Administración ---
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("unadmin", unadmin_command))
    application.add_handler(CommandHandler("admins", admins_command))
    application.add_handler(CommandHandler("staff", staff_command))

    # --- Utilidades ---
    application.add_handler(CommandHandler("del", del_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("info", info_command))

    # --- Mensajes recurrentes ---
    application.add_handler(CommandHandler("addrecurrente", addrecurring_command))
    application.add_handler(CommandHandler("recurrentes", recurring_list_command))

    # --- Palabras prohibidas ---
    application.add_handler(CommandHandler("agregarpalabra", add_word_command))
    application.add_handler(CommandHandler("eliminarpalabra", remove_word_command))
    application.add_handler(CommandHandler("palabras", list_words_command))

    # --- AFK ---
    # /brb sigue funcionando como alias, pero ya no hace falta la barra:
    # basta con escribir "brb" (opcionalmente seguido de un motivo).
    application.add_handler(CommandHandler("brb", brb_command))

    # Router de mensajes libres (texto o media): wizard de recurrentes,
    # wizard de palabras prohibidas, ediciones pendientes del menú y "brb".
    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.ALL, on_message_router)
    )

    # --- Bienvenida / Despedida / Reglamento ---
    application.add_handler(CommandHandler("setwelcome", setwelcome_command))
    application.add_handler(CommandHandler("welcome", welcome_command))
    application.add_handler(CommandHandler("resetwelcome", resetwelcome_command))
    application.add_handler(CommandHandler("setgoodbye", setgoodbye_command))
    application.add_handler(CommandHandler("goodbye", goodbye_command))
    application.add_handler(CommandHandler("resetgoodbye", resetgoodbye_command))
    application.add_handler(CommandHandler("welcomeclean", welcomeclean_command))
    application.add_handler(CommandHandler("setrules", setrules_command))
    application.add_handler(CommandHandler("rules", rules_command))
    application.add_handler(CommandHandler("resetrules", resetrules_command))

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
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    setup_logging()
    application = build_application()
    logger.info("Iniciando bot en modo polling...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
