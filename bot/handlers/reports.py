"""
handlers/reports.py
Sistema de reportes: /reportar y la palabra "@admin" dentro de un mensaje
de grupo avisan por privado a TODOS los administradores del grupo (con
get_chat_administrators, igual que handlers/admin.py), armando una tarjeta
con:

    - Botón "👁 Ver mensaje reportado" (enlace directo al mensaje).
    - Usuario reportado (o "No identificado" si no aplica).
    - Usuario que reportó.
    - Motivo (opcional).
    - Botón para alternar el estado Pendiente / Resuelto, que se refleja
      en TODAS las notificaciones ya enviadas a los demás admins.

Uso:
    /reportar [motivo]   - respondiendo al mensaje que se quiere reportar.
    @admin [motivo]       - en cualquier parte del texto, respondiendo o no
                            a un mensaje. Si no se responde a nada, se
                            reporta el propio mensaje (sin atribuirle al
                            que escribió "@admin" ser el "usuario
                            reportado": solo se avisa a los admins con el
                            enlace al mensaje).

Callback pattern: "^rpt:"
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database, Report
from utils.callbacks import safe_callback
from utils.formatting import error, escape_md, mention, success
from utils.permissions import check_executor_is_admin

logger = logging.getLogger(__name__)

# Detecta "@admin" como palabra completa (no dispara con "@administracion",
# "correo@admin.com", etc.), sin importar mayúsculas/minúsculas.
ADMIN_MENTION_RE = re.compile(r"(?i)(?<![\w@])@admin(?!\w)")

STATUS_LABELS = {"pending": "🕓 Pendiente", "resolved": "✅ Resuelto"}


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _build_message_link(chat: Chat, message_id: Optional[int]) -> Optional[str]:
    """Arma el enlace directo al mensaje reportado, igual que el que genera
    Telegram al usar "Copiar enlace" (solo disponible en super-grupos)."""
    if message_id is None:
        return None
    if chat.username:
        return f"https://t.me/{chat.username}/{message_id}"
    chat_id_str = str(chat.id)
    if chat_id_str.startswith("-100"):
        return f"https://t.me/c/{chat_id_str[4:]}/{message_id}"
    return None  # grupos "básicos" (no super-grupo) no soportan enlaces directos


async def _report_text(db: Database, report: Report) -> str:
    reported = (
        mention(report.reported_id, report.reported_name or "Usuario")
        if report.reported_id
        else "No identificado"
    )
    lines = [
        "🚨 *Nuevo reporte*",
        f"📍 Grupo: {escape_md(report.group_title or 'Sin nombre')}",
        f"👤 Usuario reportado: {reported}",
        f"🗣 Reportado por: {mention(report.reporter_id, report.reporter_name)}",
        f"📝 Motivo: {escape_md(report.reason or 'No especificado')}",
        f"📌 Estado: *{escape_md(STATUS_LABELS.get(report.status, report.status))}*",
    ]
    if report.status == "resolved" and report.resolved_by:
        resolved_name = await db.get_user_display_name(report.resolved_by) or str(report.resolved_by)
        lines.append(f"✔️ Resuelto por: {mention(report.resolved_by, resolved_name)}")
    return "\n".join(lines)


def _report_keyboard(report: Report) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if report.message_link:
        rows.append([InlineKeyboardButton("👁 Ver mensaje reportado", url=report.message_link)])
    if report.status == "resolved":
        rows.append([InlineKeyboardButton("🔁 Marcar como pendiente", callback_data=f"rpt:pending:{report.id}")])
    else:
        rows.append([InlineKeyboardButton("✅ Marcar como resuelto", callback_data=f"rpt:done:{report.id}")])
    return InlineKeyboardMarkup(rows)


async def _notify_admins(
    context: ContextTypes.DEFAULT_TYPE, chat: Chat, report_id: int, text: str, keyboard: InlineKeyboardMarkup,
) -> tuple[int, int]:
    """Manda el reporte por privado a cada administrador real del grupo
    (reutiliza get_chat_administrators, igual que /admins y /staff en
    handlers/admin.py). Devuelve (enviados, fallidos)."""
    db = _get_db(context)
    try:
        admins = await context.bot.get_chat_administrators(chat.id)
    except TelegramError as exc:
        logger.warning("No pude obtener administradores de %s: %s", chat.id, exc)
        return 0, 0

    sent, failed = 0, 0
    for member in admins:
        admin_user = member.user
        if admin_user.is_bot:
            continue
        try:
            msg = await context.bot.send_message(
                admin_user.id, text, parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard, disable_web_page_preview=True,
            )
        except TelegramError as exc:
            # Casi siempre porque el admin nunca inició un chat con el bot.
            logger.info("No pude notificar por privado al admin %s: %s", admin_user.id, exc)
            failed += 1
            continue
        sent += 1
        await db.add_report_notification(report_id, admin_user.id, msg.message_id)
    return sent, failed


async def _create_report(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
    reported_message: Optional[Message], reason: Optional[str], source: str,
    allow_self: bool = False,
) -> None:
    chat = update.effective_chat
    reporter = update.effective_user
    message = update.effective_message
    db = _get_db(context)

    await db.upsert_user(reporter.id, reporter.username, reporter.first_name)

    reported_user = reported_message.from_user if reported_message and reported_message.from_user else None
    if reported_user is not None:
        await db.upsert_user(reported_user.id, reported_user.username, reported_user.first_name)

    if reported_user is not None and reported_user.id == reporter.id:
        if allow_self:
            reported_user = None  # no es un reporte contra sí mismo, solo un llamado de atención
        else:
            await message.reply_text(error("No puedes reportarte a ti mismo."))
            return

    if reported_user is not None and reported_user.id == context.bot.id:
        await message.reply_text(error("No puedes reportar los mensajes del bot."))
        return

    message_id = reported_message.message_id if reported_message else None
    message_link = _build_message_link(chat, message_id)

    report_id = await db.add_report(
        group_id=chat.id,
        group_title=chat.title,
        reporter_id=reporter.id,
        reporter_name=reporter.first_name,
        reported_id=(reported_user.id if reported_user else None),
        reported_name=(reported_user.first_name if reported_user else None),
        message_id=message_id,
        message_link=message_link,
        reason=reason,
        source=source,
    )

    report = await db.get_report(report_id)
    text = await _report_text(db, report)
    keyboard = _report_keyboard(report)

    sent, failed = await _notify_admins(context, chat, report_id, text, keyboard)

    if sent > 0:
        await message.reply_text(success("Tu reporte fue enviado a los administradores. ¡Gracias por avisar!"))
    elif failed > 0:
        await message.reply_text(
            error(
                "No pude notificar por privado a ningún administrador (deben iniciar un chat "
                "conmigo primero), pero tu reporte quedó registrado."
            )
        )
    else:
        await message.reply_text(error("No encontré administradores para notificar en este grupo."))


# --------------------------------------------------------------------- #
# /reportar [motivo]  (respondiendo al mensaje a reportar)
# --------------------------------------------------------------------- #
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message

    if chat is None or chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return

    reported_message = message.reply_to_message
    if reported_message is None:
        await message.reply_text(
            error(
                "Debes responder al mensaje que quieres reportar con /reportar [motivo].\n"
                "Ejemplo: /reportar spam"
            )
        )
        return

    reason = " ".join(context.args).strip() if context.args else None
    await _create_report(update, context, reported_message, reason, source="reportar")


# --------------------------------------------------------------------- #
# "@admin [motivo]" en cualquier mensaje de texto de un grupo
# --------------------------------------------------------------------- #
async def admin_mention_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if message is None or not message.text or chat is None or chat.type not in ("group", "supergroup"):
        return

    match = ADMIN_MENTION_RE.search(message.text)
    if not match:
        return

    has_reply = message.reply_to_message is not None
    reported_message = message.reply_to_message if has_reply else message
    reason = message.text[match.end():].strip(" ,:.-—") or None

    await _create_report(
        update, context, reported_message, reason, source="admin_mention", allow_self=not has_reply,
    )


# --------------------------------------------------------------------- #
# Callback: alternar Pendiente <-> Resuelto (actualiza TODAS las
# notificaciones que ya se le mandaron a los admins de ese reporte).
# --------------------------------------------------------------------- #
@safe_callback
async def report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    db = _get_db(context)

    _, action, report_id_str = query.data.split(":")
    report_id = int(report_id_str)

    report = await db.get_report(report_id)
    if report is None:
        await query.answer("Este reporte ya no existe.", show_alert=True)
        return

    check = await check_executor_is_admin(context.bot, report.group_id, query.from_user.id)
    if not check.allowed:
        await query.answer("Solo un administrador del grupo puede hacer esto.", show_alert=True)
        return

    new_status = "resolved" if action == "done" else "pending"
    resolved_by = query.from_user.id if new_status == "resolved" else None
    await db.set_report_status(report_id, new_status, resolved_by)

    report = await db.get_report(report_id)
    text = await _report_text(db, report)
    keyboard = _report_keyboard(report)

    for admin_id, message_id in await db.get_report_notifications(report_id):
        try:
            await context.bot.edit_message_text(
                chat_id=admin_id, message_id=message_id, text=text,
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            logger.info("No pude actualizar la notificación del reporte %s para %s: %s", report_id, admin_id, exc)

    await query.answer("Marcado como resuelto ✅" if new_status == "resolved" else "Marcado como pendiente 🕓")
