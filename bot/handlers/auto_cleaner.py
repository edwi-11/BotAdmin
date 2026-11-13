"""
handlers/auto_cleaner.py
Lado CEO de la "🧹 Eliminación automática" (CEO Cleaner):

- Menú por botones (callback pattern "^cl:"): estado, hora de inicio y frecuencia.
  Solo administradores (mismo requisito que el resto de paneles de /menu).
- Scheduler: revisa cada 30 s qué grupos tienen la limpieza vencida, comprueba
  que CEO Cleaner esté en el grupo con permiso de eliminar mensajes y, si todo
  está bien, encola un trabajo firmado en `cleaner_jobs`. CEO NO borra nada:
  la ejecución la hace el proceso independiente CEO Cleaner (cleaner_bot.py).
- Marcas: guarda el primer message_id visto por hora en los grupos con la
  función activa. Solo sirve de punto de partida de la PRIMERA limpieza
  (Telegram no permite a un bot leer el historial del chat).

Independiente de handlers/cleanup.py ("🧹 Auto-eliminar", prefijo "c:"), que
sigue funcionando igual.
"""
from __future__ import annotations

import asyncio
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, ContextTypes

from ceo_cleaner.shared import (
    FREQUENCIES,
    PENDING_TTL_SECONDS,
    RUNNING_TTL_SECONDS,
    advance_next_run,
    compute_next_run,
    fmt_local,
    fmt_time,
    frequency_label,
    late_grace_seconds,
    new_nonce,
    sign_job,
)
from config import settings
from database import CleanerConfig, Database
from utils.callbacks import safe_callback
from utils.formatting import escape_md
from utils.permissions import is_chat_admin

logger = logging.getLogger(__name__)

ENABLED_KEY = "cleaner_enabled"   # bot_data: set con los group_id con la limpieza activa
TICK_SECONDS = 30
_tick_lock = asyncio.Lock()
_last_bucket: dict[int, int] = {}


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _configured() -> bool:
    return bool(settings.cleaner_secret) and settings.cleaner_bot_id is not None


# --------------------------------------------------------------------- #
# Verificación de CEO Cleaner en el grupo
# --------------------------------------------------------------------- #
async def check_cleaner_ready(bot, group_id: int) -> tuple[bool, str]:
    cleaner_id = settings.cleaner_bot_id
    if cleaner_id is None or not settings.cleaner_secret:
        return False, "CEO Cleaner no está configurado en el servidor"
    try:
        member = await bot.get_chat_member(group_id, cleaner_id)
    except TelegramError:
        return False, "CEO Cleaner no está en el grupo"
    if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        return False, "CEO Cleaner no está en el grupo"
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        return False, "CEO Cleaner debe ser administrador del grupo"
    if not getattr(member, "can_delete_messages", False):
        return False, "CEO Cleaner no tiene el permiso «Eliminar mensajes»"
    return True, ""


# --------------------------------------------------------------------- #
# Menú
# --------------------------------------------------------------------- #
def _menu_text(cfg: CleanerConfig, ready: bool, ready_msg: str) -> str:
    state = "🟢 Activada" if cfg.enabled else "🔴 Desactivada"
    lines = [
        "🧹 *Eliminación automática*",
        "",
        "CEO Cleaner limpia el grupo a la hora y con la frecuencia que elijas\\.",
        "",
        f"Estado: {state}",
        f"Hora de inicio: {escape_md(fmt_time(cfg.start_hour, cfg.start_minute))}",
        f"Frecuencia: {escape_md(frequency_label(cfg.interval_minutes))}",
        f"Zona horaria: {escape_md(cfg.timezone)}",
    ]
    if cfg.enabled and cfg.next_run_at:
        lines.append(f"Próxima limpieza: {escape_md(fmt_local(cfg.next_run_at, cfg.timezone))}")
    if cfg.last_run_at:
        lines.append(f"Última limpieza: {escape_md(fmt_local(cfg.last_run_at, cfg.timezone))}")
    lines.append(f"CEO Cleaner: {'✅ Listo' if ready else '⚠️ ' + escape_md(ready_msg)}")
    lines += [
        "",
        "ℹ️ Solo se eliminan los mensajes que Telegram permite borrar\\. "
        "El mensaje fijado se conserva\\.",
    ]
    return "\n".join(lines)


def _build_menu(group_id: int, cfg: CleanerConfig) -> InlineKeyboardMarkup:
    state = "🟢 Activada" if cfg.enabled else "🔴 Desactivada"
    rows = [
        [InlineKeyboardButton(f"Estado: {state}", callback_data=f"cl:toggle:{group_id}")],
        [InlineKeyboardButton(f"🕐 Hora de inicio: {fmt_time(cfg.start_hour, cfg.start_minute)}", callback_data=f"cl:time:{group_id}")],
        [InlineKeyboardButton(f"🔁 Frecuencia: {frequency_label(cfg.interval_minutes)}", callback_data=f"cl:freq:{group_id}")],
        [InlineKeyboardButton("🔙 Volver", callback_data=f"m:main:{group_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def _build_hours(group_id: int, cfg: CleanerConfig) -> InlineKeyboardMarkup:
    rows, row = [], []
    for hour in range(24):
        mark = "✅ " if hour == cfg.start_hour else ""
        row.append(InlineKeyboardButton(f"{mark}{hour:02d}", callback_data=f"cl:h:{hour}:{group_id}"))
        if len(row) == 6:
            rows.append(row)
            row = []
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"cl:menu:{group_id}")])
    return InlineKeyboardMarkup(rows)


def _build_minutes(group_id: int, hour: int, cfg: CleanerConfig) -> InlineKeyboardMarkup:
    rows, row = [], []
    for minute in range(0, 60, 5):
        selected = hour == cfg.start_hour and minute == cfg.start_minute
        row.append(InlineKeyboardButton(
            f"{'✅ ' if selected else ''}{hour:02d}:{minute:02d}", callback_data=f"cl:m:{hour}:{minute}:{group_id}",
        ))
        if len(row) == 4:
            rows.append(row)
            row = []
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"cl:time:{group_id}")])
    return InlineKeyboardMarkup(rows)


def _build_freqs(group_id: int, cfg: CleanerConfig) -> InlineKeyboardMarkup:
    rows, row = [], []
    for minutes, label in FREQUENCIES:
        mark = "✅ " if minutes == cfg.interval_minutes else ""
        row.append(InlineKeyboardButton(f"{mark}{label}", callback_data=f"cl:f:{minutes}:{group_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"cl:menu:{group_id}")])
    return InlineKeyboardMarkup(rows)


async def _edit(query, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def _show_menu(query, context, group_id: int) -> tuple[bool, str]:
    cfg = await _db(context).get_cleaner_config(group_id, settings.cleaner_timezone)
    ready, msg = await check_cleaner_ready(context.bot, group_id)
    await _edit(query, _menu_text(cfg, ready, msg), _build_menu(group_id, cfg))
    return ready, msg


def _reschedule(cfg: CleanerConfig, **overrides) -> dict:
    """Si la función está activa, recalcula la próxima ejecución con los nuevos valores."""
    if not cfg.enabled:
        return {}
    hour = overrides.get("start_hour", cfg.start_hour)
    minute = overrides.get("start_minute", cfg.start_minute)
    interval = overrides.get("interval_minutes", cfg.interval_minutes)
    return {"next_run_at": compute_next_run(hour, minute, interval, cfg.timezone)}


@safe_callback
async def auto_cleaner_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) < 3 or not parts[-1].lstrip("-").isdigit():
        await query.answer()
        return

    action = parts[1]
    group_id = int(parts[-1])
    user = update.effective_user
    db = _db(context)

    if not await is_chat_admin(context.bot, group_id, user.id):
        await query.answer("Solo los administradores pueden cambiar esta configuración.", show_alert=True)
        return

    cfg = await db.get_cleaner_config(group_id, settings.cleaner_timezone)

    if action == "menu":
        await _show_menu(query, context, group_id)
        await query.answer()
        return

    if action == "toggle":
        enabled_set: set = context.application.bot_data.setdefault(ENABLED_KEY, set())
        if not cfg.enabled:
            if not _configured():
                await query.answer("CEO Cleaner no está configurado en el servidor (falta CLEANER_BOT_TOKEN o CLEANER_SHARED_SECRET).", show_alert=True)
                return
            next_run = compute_next_run(cfg.start_hour, cfg.start_minute, cfg.interval_minutes, cfg.timezone)
            await db.update_cleaner_config(
                group_id, settings.cleaner_timezone, enabled=1, next_run_at=next_run,
                last_cleaned_id=None, updated_by=user.id,
            )
            await db.clear_cleaner_marks(group_id)  # punto de partida nuevo
            _last_bucket.pop(group_id, None)
            enabled_set.add(group_id)
            logger.info("[grupo=%s] Eliminación automática ACTIVADA por %s", group_id, user.id)
        else:
            await db.update_cleaner_config(
                group_id, settings.cleaner_timezone, enabled=0, next_run_at=None, updated_by=user.id,
            )
            enabled_set.discard(group_id)
            logger.info("[grupo=%s] Eliminación automática DESACTIVADA por %s", group_id, user.id)
        ready, msg = await _show_menu(query, context, group_id)
        if not cfg.enabled and not ready:
            await query.answer(f"⚠️ {msg}. La limpieza se cancelará hasta que se corrija.", show_alert=True)
        else:
            await query.answer()
        return

    if action == "time":
        await _edit(query, "🕐 *Hora de inicio*\n\nElige la hora \\(formato 24 h\\)\\.", _build_hours(group_id, cfg))
        await query.answer()
        return

    if action == "h" and len(parts) == 4 and parts[2].isdigit() and 0 <= int(parts[2]) <= 23:
        hour = int(parts[2])
        await _edit(query, f"🕐 *Hora de inicio*\n\nElige los minutos para las {escape_md(f'{hour:02d}')} h\\.", _build_minutes(group_id, hour, cfg))
        await query.answer()
        return

    if (
        action == "m" and len(parts) == 5 and parts[2].isdigit() and parts[3].isdigit()
        and 0 <= int(parts[2]) <= 23 and 0 <= int(parts[3]) <= 59 and int(parts[3]) % 5 == 0
    ):
        hour, minute = int(parts[2]), int(parts[3])
        await db.update_cleaner_config(
            group_id, settings.cleaner_timezone, start_hour=hour, start_minute=minute,
            updated_by=user.id, **_reschedule(cfg, start_hour=hour, start_minute=minute),
        )
        await _show_menu(query, context, group_id)
        await query.answer(f"Hora de inicio: {fmt_time(hour, minute)}")
        return

    if action == "freq":
        await _edit(query, "🔁 *Frecuencia*\n\nCada cuánto se repite la limpieza\\.", _build_freqs(group_id, cfg))
        await query.answer()
        return

    if action == "f" and len(parts) == 4 and parts[2].isdigit() and int(parts[2]) in dict(FREQUENCIES):
        minutes = int(parts[2])
        await db.update_cleaner_config(
            group_id, settings.cleaner_timezone, interval_minutes=minutes,
            updated_by=user.id, **_reschedule(cfg, interval_minutes=minutes),
        )
        await _show_menu(query, context, group_id)
        await query.answer(frequency_label(minutes))
        return

    await query.answer()


# --------------------------------------------------------------------- #
# Marcas (punto de partida de la primera limpieza)
# --------------------------------------------------------------------- #
async def track_cleaner_marks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    chat = update.effective_chat
    if message is None or chat is None:
        return
    enabled = context.application.bot_data.get(ENABLED_KEY)
    if not enabled or chat.id not in enabled:
        return
    bucket = int(message.date.timestamp()) // 3600
    if _last_bucket.get(chat.id) == bucket:
        return
    _last_bucket[chat.id] = bucket
    try:
        await _db(context).touch_cleaner_mark(chat.id, bucket, message.message_id)
    except Exception:
        logger.exception("[grupo=%s] No se pudo guardar la marca de limpieza", chat.id)


# --------------------------------------------------------------------- #
# Scheduler
# --------------------------------------------------------------------- #
async def _notify_admin(context: ContextTypes.DEFAULT_TYPE, cfg: CleanerConfig, reason: str) -> None:
    bot = context.bot
    title = str(cfg.group_id)
    try:
        title = (await bot.get_chat(cfg.group_id)).title or title
    except TelegramError:
        pass
    text = (
        f"⚠️ La limpieza automática de «{title}» se canceló.\n\n"
        f"Motivo: {reason}.\n\n"
        "Un administrador debe agregar a CEO Cleaner al grupo, hacerlo administrador con el permiso "
        "«Eliminar mensajes» y revisar /menu → 🧹 Eliminación automática."
    )
    if cfg.updated_by:
        try:
            await bot.send_message(cfg.updated_by, text)
            return
        except TelegramError:
            pass  # el admin nunca abrió chat privado con CEO: avisamos en el grupo
    try:
        await bot.send_message(cfg.group_id, text)
    except TelegramError as exc:
        logger.warning("[grupo=%s] No se pudo avisar al administrador: %s", cfg.group_id, exc)


async def _process_due(context: ContextTypes.DEFAULT_TYPE, db: Database, cfg: CleanerConfig, now: int) -> None:
    gid = cfg.group_id
    due_at = cfg.next_run_at or now
    new_next = advance_next_run(due_at, cfg.interval_minutes, now)

    # Retraso excesivo (CEO apagado): se omite en vez de limpiar a deshoras.
    if now - due_at > late_grace_seconds(cfg.interval_minutes):
        await db.update_cleaner_config(gid, settings.cleaner_timezone, next_run_at=new_next, last_status="omitida (retraso)")
        logger.warning("[grupo=%s] Limpieza omitida por retraso de %ss", gid, now - due_at)
        return

    # Se adelanta la próxima ejecución antes de nada para no reintentar cada 30 s.
    await db.update_cleaner_config(gid, settings.cleaner_timezone, next_run_at=new_next)

    ready, reason = await check_cleaner_ready(context.bot, gid)
    if not ready:
        await db.update_cleaner_config(gid, settings.cleaner_timezone, last_run_at=now, last_status=f"cancelada: {reason}")
        logger.error("[grupo=%s] Limpieza cancelada: %s", gid, reason)
        await _notify_admin(context, cfg, reason)
        return

    nonce = new_nonce()
    signature = sign_job(settings.cleaner_secret, gid, now, nonce)
    job_id = await db.create_cleaner_job(gid, nonce, signature, now)
    if job_id is None:
        logger.warning("[grupo=%s] Ya hay una limpieza en curso; se omite esta ejecución", gid)
        return
    await db.update_cleaner_config(gid, settings.cleaner_timezone, last_status="en cola")
    logger.info("[grupo=%s job=%s] Limpieza encolada", gid, job_id)


async def cleaner_scheduler_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    if _tick_lock.locked():
        return
    async with _tick_lock:
        db = _db(context)
        try:
            expired = await db.expire_cleaner_jobs(PENDING_TTL_SECONDS, RUNNING_TTL_SECONDS)
            if expired:
                logger.warning("Trabajos de limpieza expirados/interrumpidos: %d", expired)
            now = int(time.time())
            for cfg in await db.get_due_cleaner_configs(now):
                try:
                    await _process_due(context, db, cfg, now)
                except Exception:
                    logger.exception("[grupo=%s] Error al programar la limpieza", cfg.group_id)
        except Exception:
            logger.exception("Error en el scheduler de eliminación automática")


async def schedule_auto_cleaner(application: Application, db: Database) -> None:
    application.bot_data[ENABLED_KEY] = {cfg.group_id for cfg in await db.get_enabled_cleaner_configs()}
    if not _configured():
        logger.warning(
            "Eliminación automática inactiva: faltan CLEANER_BOT_TOKEN (o CLEANER_BOT_ID) y/o CLEANER_SHARED_SECRET en .env."
        )
        return
    if application.job_queue is None:
        logger.error("JobQueue no disponible: la eliminación automática NO se programará.")
        return
    application.job_queue.run_repeating(
        cleaner_scheduler_tick, interval=TICK_SECONDS, first=20, name="cleaner_scheduler",
    )
    logger.info("Scheduler de eliminación automática listo (%d grupo(s) activos).", len(application.bot_data[ENABLED_KEY]))
