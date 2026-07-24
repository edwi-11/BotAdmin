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

--- Racha de actividad ---
Además de contar mensajes, `track_activity` actualiza la "racha" de
días seguidos que cada usuario escribió al menos un mensaje en el
grupo (columnas `streak_days` / `streak_last_day` de `activity_stats`).
La primera vez que alguien escribe en el día se le suma o reinicia la
racha y se le da una recompensa en monedas (ver `_streak_reward`); al
llegar a ciertos hitos (`STREAK_MILESTONES`) se le avisa en el grupo
con una recompensa extra.

--- Roles / insignias automáticas ---
`get_activity_title` traduce el total de mensajes de un usuario a un
"título" (Novato, Activo, Veterano, etc.), usado en /info
(handlers/utils_cmds.py) y en el texto del ranking de /top
(utils/ranking_image.py).
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

# Hora local del servidor a la que corre el chequeo de reinicio diario.
# A esa misma hora también se revisa si además hay que reiniciar la
# semana (los lunes).
_RESET_HOUR = 0
_RESET_MINUTE = 5

_META_DAILY = "last_daily_reset"    # valor guardado: "YYYY-MM-DD"
_META_WEEKLY = "last_weekly_reset"  # valor guardado: "YYYY-Www" (semana ISO)

# --------------------------------------------------------------------- #
# Racha de actividad: recompensa base + bono por día de racha (con tope),
# y una recompensa extra única al llegar a cada hito.
# --------------------------------------------------------------------- #
STREAK_BASE_REWARD = 10
STREAK_BONUS_PER_DAY = 2
STREAK_REWARD_CAP = 100
STREAK_MILESTONES: dict[int, int] = {
    3: 50,
    7: 100,
    14: 200,
    30: 400,
    60: 800,
    100: 1500,
    365: 6000,
}


def _streak_reward(streak_days: int) -> int:
    return min(STREAK_BASE_REWARD + STREAK_BONUS_PER_DAY * (streak_days - 1), STREAK_REWARD_CAP)


# --------------------------------------------------------------------- #
# Roles / insignias automáticas según el total de mensajes del usuario.
# --------------------------------------------------------------------- #
_ACTIVITY_TITLES: tuple[tuple[int, str], ...] = (
    (5000, "👑 Leyenda"),
    (2000, "💎 Élite"),
    (1000, "🏅 Veterano"),
    (500, "🔵 Comprometido"),
    (100, "🟢 Activo"),
    (0, "⚪ Novato"),
)


def get_activity_title(total_messages: int) -> str:
    """Título/insignia correspondiente a un total de mensajes (usado en
    /info y en el ranking de /top)."""
    for threshold, title in _ACTIVITY_TITLES:
        if total_messages >= threshold:
            return title
    return _ACTIVITY_TITLES[-1][1]


async def _handle_activity_streak(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, db: Database
) -> None:
    today = dt.date.today()
    new_streak = await db.bump_activity_streak(
        chat_id, user.id, today.isoformat(), (today - dt.timedelta(days=1)).isoformat(),
    )
    if new_streak is None:
        return  # ya se le había contado la racha de hoy

    milestone_bonus = STREAK_MILESTONES.get(new_streak, 0)
    reward = _streak_reward(new_streak) + milestone_bonus
    await db.add_balance(chat_id, user.id, reward)

    if milestone_bonus <= 0:
        return  # solo avisamos en el grupo cuando cae justo en un hito

    mention_html = f'<a href="tg://user?id={user.id}">{html.escape(user.first_name or "Usuario")}</a>'
    text = (
        f"🔥 {mention_html} lleva <b>{new_streak} días seguidos</b> activo en el grupo "
        f"y ganó <b>+{reward} monedas</b> por su racha 🎉"
    )
    try:
        await context.bot.send_message(chat_id, text, parse_mode="HTML")
    except TelegramError as exc:
        logger.warning("No se pudo avisar el hito de racha en el grupo %s: %s", chat_id, exc)


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
    await _handle_activity_streak(context, message.chat.id, user, db)


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
