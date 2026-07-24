"""
utils/activity_stats.py
Registra cuántos mensajes manda cada usuario en cada grupo (tabla
`activity_stats`) para alimentar el comando /top (ranking de mensajes,
ver handlers/activity_ranking.py y utils/ranking_image.py).

Los contadores `today_messages` y `week_messages` NO se reinician de
forma perezosa (al llegar el primer mensaje del nuevo período); se
reinician con un job programado (`schedule_activity_resets`) que corre
una vez por día. Esto es a propósito: si se reiniciara solo al mandar
un mensaje, alguien que estuvo activo toda la semana pero no escribió
nada HOY seguiría apareciendo con su contador de ayer en el ranking
"Hoy", cuando en realidad debería salir del todo (o en 0).

Además del job diario, al arrancar el bot se hace un chequeo inmediato
(`run_once`) comparando la fecha guardada en `activity_meta` contra la
fecha actual, por si el bot estuvo apagado justo a la hora programada
del reinicio y se lo hubiera perdido.
"""
from __future__ import annotations

import datetime as dt
import logging

from telegram import Update
from telegram.ext import Application, ContextTypes

from database import Database

logger = logging.getLogger(__name__)

# Hora local del servidor a la que corre el chequeo de reinicio diario.
# A esa misma hora también se revisa si además hay que reiniciar la
# semana (los lunes).
_RESET_HOUR = 0
_RESET_MINUTE = 5

_META_DAILY = "last_daily_reset"    # valor guardado: "YYYY-MM-DD"
_META_WEEKLY = "last_weekly_reset"  # valor guardado: "YYYY-Www" (semana ISO)


async def track_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler genérico: cuenta CUALQUIER mensaje de grupo (con o sin
    comando) hacia las estadísticas de actividad de quien lo mandó.
    Se registra en un `group` propio para no depender de que ningún
    otro handler lo procese primero (ver main.py)."""
    message = update.effective_message
    if message is None or message.chat is None or message.chat.type not in ("group", "supergroup"):
        return

    user = message.from_user
    if user is None or user.is_bot:
        return  # mensajes de canales anónimos / otros bots no cuentan para el ranking

    db: Database = context.application.bot_data["db"]
    await db.record_message_activity(
        chat_id=message.chat.id,
        user_id=user.id,
        username=user.username,
        first_name=user.first_name or "Usuario",
        last_name=user.last_name,
    )


async def _run_resets_if_needed(db: Database) -> None:
    today = dt.date.today()
    iso_year, iso_week, _ = today.isocalendar()
    day_key = today.isoformat()
    week_key = f"{iso_year}-W{iso_week:02d}"

    if await db.get_meta(_META_DAILY) != day_key:
        changed = await db.reset_daily_activity()
        await db.set_meta(_META_DAILY, day_key)
        logger.info("Ranking de actividad: reiniciados los mensajes de HOY (%d filas).", changed)

    if await db.get_meta(_META_WEEKLY) != week_key:
        changed = await db.reset_weekly_activity()
        await db.set_meta(_META_WEEKLY, week_key)
        logger.info("Ranking de actividad: reiniciados los mensajes de esta SEMANA (%d filas).", changed)


async def _reset_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.application.bot_data["db"]
    await _run_resets_if_needed(db)


def schedule_activity_resets(application: Application) -> None:
    """Programa el reinicio diario/semanal de `activity_stats` y hace un
    chequeo inmediato al arrancar (ver docstring del módulo). Se llama
    una vez desde main.py -> post_init, después de dejar `db` en
    application.bot_data."""
    if application.job_queue is None:
        logger.warning(
            "job_queue no está disponible (¿falta el extra 'job-queue' de "
            "python-telegram-bot?); los contadores de Hoy/Semana de /top no se reiniciarán solos."
        )
        return
    application.job_queue.run_daily(
        _reset_job, time=dt.time(hour=_RESET_HOUR, minute=_RESET_MINUTE), name="activity_stats_daily_reset",
    )
    application.job_queue.run_once(_reset_job, when=5, name="activity_stats_startup_check")
