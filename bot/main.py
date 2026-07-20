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

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from telegram import BotCommand, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from config import settings
from database import Database
from handlers.activation import (
    activar_command,
    desactivar_command,
    group_gate,
    on_bot_membership_change,
)
from handlers.admin import admin_command, unadmin_command
from handlers.afk import brb_text_trigger, load_afk_cache, track_and_check_afk
from handlers.economy import (
    baloncesto_command,
    bolos_command,
    cobrar_command,
    comprar_command,
    dardos_command,
    depositar_command,
    diario_command,
    futbol_command,
    ranking_command,
    renunciar_command,
    retirar_command,
    robar_command,
    saldo_command,
    tienda_command,
    trabajo_command,
    trabajos_command,
    tragamonedas_command,
    transferir_command,
)
from handlers.cleanup import (
    cleanup_menu_callback,
    on_call_started_cleanup,
    on_command_cleanup,
    on_join_cleanup,
    on_leave_cleanup,
    on_pin_cleanup,
)
from handlers.filters_words import check_banned_words, try_consume_pending_words, words_menu_callback
from handlers.free import free_command, freelist_command, unfree_command
from handlers.gemini_chat import ceo_trigger
from handlers.menu import menu_callback, menu_command, try_consume_pending_input
from utils.message_log import track_message
from handlers.greetings import on_left_member, on_new_members
from handlers.moderation import (
    ban_command,
    delban_command,
    delkick_command,
    delwarn_command,
    kick_command,
    mute_command,
    unban_command,
    unmute_command,
    unwarn_command,
    warn_command,
)
from handlers.recurring import LOCAL_FILE_PREFIX
from handlers.recurring import _send_content as _send_broadcast_content
from handlers.recurring import load_all_recurring_jobs, recurring_callback, try_consume_draft_input
from handlers.quote_sticker import q_command
from handlers.owner_groups import grupos_command
from handlers.remote_control import (
    owner_command,
    owner_select_callback,
    ready_command,
    remote_dispatch,
)
from handlers.join_requests import aceptar_command, on_chat_join_request
from handlers.donations import (
    donar_amount_callback,
    donar_command,
    donar_precheckout_callback,
    donar_successful_payment,
)
from handlers.secret_messages import (
    handle_secret_start_deeplink,
    secret_callback,
    secret_inline_query,
    try_consume_pending_secret_edit,
)
from handlers.utils_cmds import (
    del_command,
    id_command,
    info_command,
    npin_command,
    pin_command,
    ping_command,
    send_command,
)
from handlers.warnings import warnings_callback
from utils.logger import setup_logging

logger = logging.getLogger(__name__)

# Cada cuánto se revisa si hay anuncios nuevos encolados por el bot anunciador.
BROADCAST_POLL_SECONDS = 15
# Pequeña pausa entre grupo y grupo al difundir, para no saturar la API de Telegram.
BROADCAST_DELAY_BETWEEN_GROUPS = 0.05

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
    BotCommand("delwarn", "Advertir y borrar el mensaje respondido"),
    BotCommand("ban", "Banear a un usuario"),
    BotCommand("delban", "Banear y borrar el mensaje respondido"),
    BotCommand("unban", "Desbanear a un usuario"),
    BotCommand("kick", "Expulsar a un usuario"),
    BotCommand("delkick", "Expulsar y borrar el mensaje respondido"),
    BotCommand("mute", "Silenciar a un usuario (permanente o temporal)"),
    BotCommand("unmute", "Quitar el silencio a un usuario"),
    BotCommand("del", "Borrar el mensaje respondido"),
    BotCommand("id", "Ver tu ID o el de a quien respondas"),
    BotCommand("info", "Ver información de un usuario"),
    BotCommand("ping", "Ver la latencia del bot"),
    BotCommand("pin", "Fijar el mensaje respondido (sin notificar)"),
    BotCommand("npin", "Fijar el mensaje respondido (con notificación)"),
    BotCommand("send", "Enviar un mensaje o reenviar contenido como el bot"),
    BotCommand("q", "Citar el mensaje respondido como sticker"),
    BotCommand("free", "Eximir a un usuario de los filtros del grupo"),
    BotCommand("unfree", "Quitarle la exención a un usuario"),
    BotCommand("freelist", "Ver usuarios liberados en este grupo"),
    # --- Economía ---
    BotCommand("saldo", "Ver tu perfil económico (o el de otro usuario)"),
    BotCommand("diario", "Reclamar tu bono diario"),
    BotCommand("baloncesto", "Jugar baloncesto (1 vez al día)"),
    BotCommand("futbol", "Jugar fútbol (1 vez al día)"),
    BotCommand("dardos", "Jugar dardos (1 vez al día)"),
    BotCommand("bolos", "Jugar bolos (1 vez al día)"),
    BotCommand("tragamonedas", "Jugar tragamonedas (1 vez al día)"),
    BotCommand("trabajos", "Ver los empleos disponibles"),
    BotCommand("trabajo", "Elegir un empleo"),
    BotCommand("renunciar", "Renunciar a tu empleo actual"),
    BotCommand("cobrar", "Cobrar el sueldo de tu empleo"),
    BotCommand("robar", "Intentar robarle monedas a otro usuario"),
    BotCommand("transferir", "Enviar monedas a otro usuario"),
    BotCommand("depositar", "Guardar monedas en el banco"),
    BotCommand("retirar", "Sacar monedas del banco"),
    BotCommand("tienda", "Ver la tienda de objetos"),
    BotCommand("comprar", "Comprar un objeto de la tienda"),
    BotCommand("ranking", "Ver el top de más ricos del grupo"),
]


def _extract_sent_file_id(message, content_type: str) -> Optional[str]:
    """Toma el file_id que Telegram acaba de asignar (bajo el token de ESTE
    bot) al mensaje recién enviado, para reutilizarlo en los siguientes
    grupos sin tener que volver a subir el archivo desde disco cada vez."""
    if content_type == "photo" and message.photo:
        return message.photo[-1].file_id
    getter = {
        "video": lambda m: m.video, "animation": lambda m: m.animation,
        "document": lambda m: m.document, "audio": lambda m: m.audio,
        "voice": lambda m: m.voice,
    }.get(content_type)
    obj = getter(message) if getter else None
    return obj.file_id if obj else None


async def _broadcast_dispatch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revisa la cola de anuncios (llenada por el bot anunciador, si lo usas)
    y envía cada anuncio pendiente a todos los grupos conocidos."""
    db: Database = context.application.bot_data["db"]
    pending = await db.get_pending_broadcasts()
    if not pending:
        return

    groups = await db.get_known_groups()
    for broadcast in pending:
        sent_count = 0
        failed_count = 0
        # El file_id puede venir marcado como archivo local en disco (lo
        # dejó ahí el bot anunciador, porque su file_id no sirve para este
        # bot). Lo subimos una sola vez al primer grupo y, si funciona,
        # reutilizamos el file_id resultante (ya válido para este bot) en
        # el resto de los grupos en vez de volver a leer el disco.
        current_file_ref = broadcast.file_id
        is_local = bool(current_file_ref and current_file_ref.startswith(LOCAL_FILE_PREFIX))

        for group_id, _title in groups:
            try:
                sent = await _send_broadcast_content(
                    context, group_id, broadcast.content_type, broadcast.text,
                    broadcast.entities, current_file_ref, broadcast.buttons,
                )
                sent_count += 1
                if is_local:
                    reused = _extract_sent_file_id(sent, broadcast.content_type)
                    if reused:
                        current_file_ref = reused
                        is_local = False
            except TelegramError as exc:
                failed_count += 1
                logger.warning("No se pudo enviar el anuncio #%s al grupo %s: %s", broadcast.id, group_id, exc)
            except FileNotFoundError as exc:
                failed_count += 1
                logger.warning("Anuncio #%s: %s", broadcast.id, exc)
                break
            await asyncio.sleep(BROADCAST_DELAY_BETWEEN_GROUPS)

        if broadcast.file_id and broadcast.file_id.startswith(LOCAL_FILE_PREFIX):
            local_path = Path(broadcast.file_id[len(LOCAL_FILE_PREFIX):])
            try:
                if local_path.is_file():
                    os.remove(local_path)
            except OSError as exc:
                logger.warning("No se pudo borrar el archivo temporal del anuncio #%s: %s", broadcast.id, exc)

        await db.mark_broadcast_sent(broadcast.id, sent_count, failed_count)
        logger.info(
            "Anuncio #%s enviado: %d exitosos, %d fallidos de %d grupos.",
            broadcast.id, sent_count, failed_count, len(groups),
        )


async def post_init(application: Application) -> None:
    db = Database(settings.database_path)
    await db.connect()
    application.bot_data["db"] = db
    application.bot_data["afk_cache"] = await load_afk_cache(db)
    await application.bot.set_my_commands(BOT_COMMANDS)
    await load_all_recurring_jobs(application, db)
    if application.job_queue is not None:
        application.job_queue.run_repeating(
            _broadcast_dispatch_job, interval=BROADCAST_POLL_SECONDS, first=BROADCAST_POLL_SECONDS,
            name="broadcast_dispatch",
        )
    logger.info("Bot inicializado correctamente. Propietarios: %s", list(settings.owner_ids))


async def post_shutdown(application: Application) -> None:
    db: Database | None = application.bot_data.get("db")
    if db is not None:
        await db.close()


async def on_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Router para mensajes sin comando (texto o media):
    0) Si el autor de un mensaje secreto está mandando el texto editado
       (en el chat privado con el bot), se consume aquí.
    1) Si el editor de mensajes recurrentes está esperando un campo
       (foto/texto/botones), se consume aquí.
    2) Si el usuario está agregando/eliminando palabras prohibidas, se consume aquí.
    3) Si el usuario tiene una edición pendiente desde el menú de botones
       (ej. estaba escribiendo el nuevo mensaje de bienvenida), se consume aquí.
    4) Si no, se comprueba si el mensaje es un disparador "brb" en texto plano.
    """
    if await try_consume_pending_secret_edit(update, context):
        return
    if await try_consume_draft_input(update, context):
        return
    if await try_consume_pending_words(update, context):
        return
    if await try_consume_pending_input(update, context):
        return
    await brb_text_trigger(update, context)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start normal -> abre el menú. /start secedit_<id> (deep link usado
    por el botón "Editar mensaje" de un mensaje secreto) -> se desvía para
    pedir el texto nuevo en vez de mostrar el menú."""
    if await handle_secret_start_deeplink(update, context):
        return
    await menu_command(update, context)


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

    # --- Sistema de activación de grupos (solo el owner puede activar) ---
    # Debe registrarse ANTES (grupo -3) que cualquier otro handler de
    # comandos, para poder bloquear el update por completo si el grupo no
    # está activado o el usuario no tiene permisos.
    application.add_handler(
        MessageHandler(filters.COMMAND & filters.ChatType.GROUPS, group_gate), group=-3
    )

    # --- Historial en memoria para /q N y /q r ---
    # Se registra bien temprano (grupo -4) y para TODOS los mensajes de
    # grupo, sin filtrar por comando ni por texto, para que /q pueda citar
    # mensajes anteriores (incluida media sin texto) aunque otro handler ya
    # los haya procesado.
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, track_message), group=-4
    )
    application.add_handler(ChatMemberHandler(on_bot_membership_change, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(CommandHandler("activar", activar_command))
    application.add_handler(CommandHandler("desactivar", desactivar_command))

    # --- Menú de configuración con botones (todo pasa por aquí) ---
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^m:"))
    application.add_handler(CallbackQueryHandler(recurring_callback, pattern=r"^r:"))
    application.add_handler(CallbackQueryHandler(words_menu_callback, pattern=r"^w:"))
    application.add_handler(CallbackQueryHandler(cleanup_menu_callback, pattern=r"^c:"))
    application.add_handler(CallbackQueryHandler(warnings_callback, pattern=r"^aw:"))

    # --- Mensajes secretos (modo en línea, estilo @mensajesecretobot) ---
    application.add_handler(InlineQueryHandler(secret_inline_query))
    application.add_handler(CallbackQueryHandler(secret_callback, pattern=r"^sec:"))

    # --- Donaciones en Telegram Stars ---
    application.add_handler(CommandHandler("donar", donar_command))
    application.add_handler(CallbackQueryHandler(donar_amount_callback, pattern=r"^donar:"))
    application.add_handler(PreCheckoutQueryHandler(donar_precheckout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, donar_successful_payment))

    # --- Lista de grupos + links de invitación (solo el propietario) ---
    application.add_handler(CommandHandler("grupos", grupos_command))
    application.add_handler(CommandHandler("owner", owner_command))
    application.add_handler(CommandHandler("ready", ready_command))
    application.add_handler(CallbackQueryHandler(owner_select_callback, pattern=r"^remote:"))
    # Máxima prioridad: intercepta los comandos del propietario en privado
    # ANTES que cualquier otro handler, mientras el modo remoto (/owner)
    # esté activo, y los reenvía al grupo elegido.
    application.add_handler(
        MessageHandler(filters.COMMAND & filters.ChatType.PRIVATE, remote_dispatch), group=-10
    )

    # --- Solicitudes de ingreso (grupos con "Aprobar nuevos miembros") ---
    application.add_handler(ChatJoinRequestHandler(on_chat_join_request))
    application.add_handler(CommandHandler("aceptar", aceptar_command))

    # --- Moderación básica (los únicos comandos "/" que quedan) ---
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("delban", delban_command))
    application.add_handler(CommandHandler("kick", kick_command))
    application.add_handler(CommandHandler("delkick", delkick_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CommandHandler("unwarn", unwarn_command))
    application.add_handler(CommandHandler("delwarn", delwarn_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("unadmin", unadmin_command))
    application.add_handler(CommandHandler("del", del_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("info", info_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("pin", pin_command))
    application.add_handler(CommandHandler("npin", npin_command))
    application.add_handler(CommandHandler("send", send_command))
    application.add_handler(CommandHandler("q", q_command))
    application.add_handler(CommandHandler("free", free_command))
    application.add_handler(CommandHandler("unfree", unfree_command))
    application.add_handler(CommandHandler("freelist", freelist_command))

    # --- Economía: juegos, trabajos, robo, banco, tienda, ranking ---
    application.add_handler(CommandHandler("saldo", saldo_command))
    application.add_handler(CommandHandler("perfil", saldo_command))
    application.add_handler(CommandHandler("diario", diario_command))
    application.add_handler(CommandHandler("baloncesto", baloncesto_command))
    application.add_handler(CommandHandler("futbol", futbol_command))
    application.add_handler(CommandHandler("dardos", dardos_command))
    application.add_handler(CommandHandler("bolos", bolos_command))
    application.add_handler(CommandHandler("tragamonedas", tragamonedas_command))
    application.add_handler(CommandHandler("trabajos", trabajos_command))
    application.add_handler(CommandHandler("trabajo", trabajo_command))
    application.add_handler(CommandHandler("renunciar", renunciar_command))
    application.add_handler(CommandHandler("cobrar", cobrar_command))
    application.add_handler(CommandHandler("robar", robar_command))
    application.add_handler(CommandHandler("transferir", transferir_command))
    application.add_handler(CommandHandler("depositar", depositar_command))
    application.add_handler(CommandHandler("retirar", retirar_command))
    application.add_handler(CommandHandler("tienda", tienda_command))
    application.add_handler(CommandHandler("comprar", comprar_command))
    application.add_handler(CommandHandler("ranking", ranking_command))

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
    application.add_handler(
        MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, on_pin_cleanup), group=2
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

    # --- Integración con Gemini: mensajes que empiezan con "ceo" (cualquier
    # combinación de mayúsc/minúsc), solo en grupos activados ---
    application.add_handler(
        MessageHandler(filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND, ceo_trigger),
        group=2,
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
