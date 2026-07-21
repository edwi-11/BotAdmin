"""
handlers/join_requests.py
/aceptar — aprueba en lote las solicitudes de ingreso pendientes de un
grupo (para grupos que tienen activado "Aprobar nuevos miembros").

Telegram no tiene ningún método para que un bot pida "la lista de
solicitudes pendientes" — solo avisa una por una, en vivo, mediante el
update `chat_join_request`, a medida que la gente las manda. Por eso las
vamos guardando en la base de datos apenas llegan (`on_chat_join_request`),
y `/aceptar` las aprueba desde ahí.

Si la bienvenida del grupo está configurada como "Privado" o "Ambos"
(ver menú de Bienvenida), `on_chat_join_request` también le manda ese
mensaje a la persona apenas manda la solicitud —no cuando ya entró—,
aprovechando la ventana de unos minutos que da Telegram para escribirle a
alguien con una solicitud pendiente.

Uso (dentro del grupo, solo administradores):
    /aceptar all      -> aprueba TODAS las solicitudes pendientes
    /aceptar 100      -> aprueba las 100 más antiguas
    /aceptar 500      -> aprueba las 500 más antiguas
    /aceptar 1000     -> etc. (sin límite máximo)

Requisito: el bot debe ser administrador del grupo con el permiso
"Invitar usuarios vía enlace" (can_invite_users), que es lo que Telegram
exige para poder llamar a approveChatJoinRequest.
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database
from utils.formatting import error, render_template
from utils.permissions import check_executor_is_admin

logger = logging.getLogger(__name__)

# Pausa entre cada aprobación para no pegarle demasiado rápido a la API de
# Telegram cuando son lotes grandes (500/1000+). ~20 aprobaciones/seg.
_BATCH_DELAY = 0.05
_PROGRESS_EVERY = 100


async def on_chat_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Se dispara cada vez que alguien manda una solicitud de ingreso a un
    grupo donde está el bot. La guarda (para /aceptar) y, si la bienvenida
    del grupo está configurada como "Privado" o "Ambos", se la manda AHORA
    —apenas pide entrar, no cuando ya esté adentro— aprovechando que
    Telegram deja que el bot le escriba por privado a alguien con una
    solicitud pendiente durante los primeros minutos, aunque nunca haya
    hablado con el bot. Pasado ese lapso (o si la aprueba/rechaza un admin
    antes), esa ventana se cierra y ya no se le puede volver a escribir así."""
    request = update.chat_join_request
    if request is None:
        return
    db: Database = context.application.bot_data["db"]
    user = request.from_user
    name = user.first_name or user.username or "Usuario"
    await db.record_join_request(request.chat.id, user.id, name, user.username)

    settings = await db.get_group_settings(request.chat.id)
    if not settings.welcome_enabled or settings.welcome_send_to == "group":
        # Sin bienvenida privada configurada: el saludo (si corresponde) se
        # manda al grupo recién cuando la solicitud se apruebe y la persona
        # entre de verdad (on_new_members la sigue cubriendo).
        return

    text = render_template(
        settings.welcome_text, user_id=user.id, first_name=user.first_name,
        username=user.username, group_title=request.chat.title,
    )
    try:
        await context.bot.send_message(request.user_chat_id, text, parse_mode=ParseMode.HTML)
        await db.upsert_user(user.id, user.username, user.first_name)
        await db.set_dm_ok(user.id, True)
        await db.mark_join_request_welcomed(request.chat.id, user.id)
    except TelegramError as exc:
        logger.info(
            "No pude mandar la bienvenida privada al pedir ingreso (%s en %s): %s",
            user.id, request.chat.id, exc,
        )


async def aceptar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await message.reply_text(
            error("Este comando se usa dentro del grupo donde quieres aceptar solicitudes de ingreso.")
        )
        return

    check = await check_executor_is_admin(context.bot, chat.id, user.id)
    if not check.allowed:
        await message.reply_text(error(check.reason))
        return

    args = context.args or []
    if not args or (args[0].lower() != "all" and not args[0].isdigit()):
        await message.reply_text(
            error(
                "Uso:\n"
                "/aceptar all — aprueba todas las solicitudes pendientes\n"
                "/aceptar 100 — aprueba las 100 más antiguas (también sirve con 500, 1000, etc.)"
            )
        )
        return

    limit = None if args[0].lower() == "all" else int(args[0])
    if limit is not None and limit <= 0:
        await message.reply_text(error("El número debe ser mayor a 0."))
        return

    db: Database = context.application.bot_data["db"]
    pending_total = await db.count_pending_join_requests(chat.id)
    if pending_total == 0:
        await message.reply_text(
            "No tengo registrada ninguna solicitud de ingreso pendiente para este grupo."
        )
        return

    # Verificación rápida de permisos del bot ANTES de intentar aprobar
    # cientos/miles de solicitudes una por una.
    try:
        me = await context.bot.get_me()
        bot_member = await context.bot.get_chat_member(chat.id, me.id)
    except TelegramError as exc:
        await message.reply_text(error(f"No pude verificar mis permisos en este grupo: {exc}"))
        return

    is_bot_owner_of_chat = bot_member.status == ChatMemberStatus.OWNER
    if not is_bot_owner_of_chat and not getattr(bot_member, "can_invite_users", False):
        await message.reply_text(
            error(
                "Necesito ser administrador con el permiso «Invitar usuarios vía enlace» "
                "en este grupo para poder aprobar solicitudes."
            )
        )
        return

    requests = await db.get_pending_join_requests(chat.id, limit)
    total = len(requests)

    status_msg = await message.reply_text(f"⏳ Aprobando {total} solicitud(es) de ingreso...")

    approved_ids: list[int] = []
    failed_ids: list[int] = []

    for i, (uid, _name, _username) in enumerate(requests, start=1):
        try:
            await context.bot.approve_chat_join_request(chat.id, uid)
            approved_ids.append(uid)
        except TelegramError as exc:
            logger.info("No se pudo aprobar la solicitud de %s en %s: %s", uid, chat.id, exc)
            failed_ids.append(uid)

        if i % _PROGRESS_EVERY == 0 and i != total:
            try:
                await status_msg.edit_text(f"⏳ Aprobando... {i}/{total}")
            except TelegramError:
                pass

        await asyncio.sleep(_BATCH_DELAY)

    if approved_ids:
        await db.set_join_requests_status_bulk(chat.id, approved_ids, "approved")
    if failed_ids:
        await db.set_join_requests_status_bulk(chat.id, failed_ids, "failed")

    remaining = await db.count_pending_join_requests(chat.id)
    summary = f"✅ {len(approved_ids)} solicitud(es) aprobada(s)."
    if failed_ids:
        summary += f"\n⚠️ {len(failed_ids)} ya no eran válidas (el usuario canceló o ya no está)."
    summary += f"\nQuedan {remaining} pendiente(s) registrada(s)."

    try:
        await status_msg.edit_text(summary)
    except TelegramError:
        await message.reply_text(summary)
