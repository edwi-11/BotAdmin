"""
utils/weekly_summary.py
Resumen semanal automático: cada domingo a la noche (antes de que
`utils/activity_stats.py` reinicie los contadores de la semana el lunes
a las 00:05), se manda un mensaje a cada grupo con:
  - Los usuarios más activos de la semana (top 5, mismos datos que /top).
  - El total de mensajes de la semana en el grupo.
  - Cuántos miembros nuevos entraron esta semana.

El conteo de miembros nuevos se hace acá mismo (`count_new_members`, un
MessageHandler chiquito registrado en main.py sobre
StatusUpdate.NEW_CHAT_MEMBERS) para no tener que tocar handlers/greetings.py.
Ese contador se reinicia junto con `week_messages`
(ver Database.reset_weekly_activity), así que este resumen tiene que
correr ANTES de ese reinicio semanal.

Los grupos bloqueados por /canal (ver handlers/channel_lock.py) no
reciben el resumen mientras sigan bloqueados.
"""
from __future__ import annotations

import datetime as dt
import html
import logging

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

from database import Database

logger = logging.getLogger(__name__)

# Domingo a las 20:00 (hora del servidor), bien antes del reinicio semanal
# de activity_stats (lunes 00:05).
_SUMMARY_HOUR = 20
_SUMMARY_MINUTE = 0
_SUMMARY_WEEKDAY = (6,)  # run_daily: 0=lunes ... 6=domingo

_TOP_N = 5


async def count_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Suma 1 al contador semanal de miembros nuevos del grupo. No hace
    nada más (la bienvenida en sí la maneja handlers/greetings.py)."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or chat.type not in ("group", "supergroup"):
        return
    if not message.new_chat_members:
        return

    db: Database = context.application.bot_data["db"]
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        await db.increment_weekly_new_members(chat.id)


def _entry_name(entry) -> str:
    return f"@{entry.username}" if entry.username else html.escape(entry.display_name)


async def _send_group_summary(context: ContextTypes.DEFAULT_TYPE, db: Database, group_id: int, title: str) -> None:
    top_entries = await db.get_activity_ranking(group_id, "week", limit=_TOP_N)
    total_week = await db.get_activity_week_total(group_id)
    new_members = await db.get_weekly_new_members(group_id)

    if not top_entries and total_week == 0 and new_members == 0:
        return  # nada pasó esta semana, no molestamos con un mensaje vacío

    lines = [f"📊 <b>Resumen semanal — {html.escape(title)}</b>", ""]
    if top_entries:
        lines.append("🏆 Más activos de la semana:")
        for i, entry in enumerate(top_entries, start=1):
            lines.append(f"{i}. {_entry_name(entry)} — {entry.week_messages} mensajes")
        lines.append("")

    lines.append(f"💬 Mensajes totales esta semana: {total_week}")
    lines.append(f"🆕 Miembros nuevos esta semana: {new_members}")

    try:
        await context.bot.send_message(group_id, "\n".join(lines), parse_mode="HTML")
    except TelegramError as exc:
        logger.warning("No se pudo mandar el resumen semanal al grupo %s: %s", group_id, exc)


async def _weekly_summary_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    groups = await db.get_known_groups()
    for group_id, title in groups:
        if await db.is_channel_locked(group_id):
            continue
        await _send_group_summary(context, db, group_id, title)


def schedule_weekly_summary(application: Application) -> None:
    """Programa el resumen semanal automático de los domingos. Se llama
    una vez desde main.py -> post_init, después de dejar `db` en
    application.bot_data."""
    if application.job_queue is None:
        logger.warning(
            "job_queue no está disponible (¿falta el extra 'job-queue' de "
            "python-telegram-bot?); el resumen semanal no se mandará solo."
        )
        return
    application.job_queue.run_daily(
        _weekly_summary_job,
        time=dt.time(hour=_SUMMARY_HOUR, minute=_SUMMARY_MINUTE),
        days=_SUMMARY_WEEKDAY,
        name="weekly_summary",
    )
