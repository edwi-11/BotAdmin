"""
ceo_cleaner/worker.py
Ejecución de la limpieza (solo la usa el proceso de CEO Cleaner).

Cómo funciona (Telegram no deja a un bot leer el historial, pero sí borrar por ID):
1. Toma un trabajo de `cleaner_jobs` y verifica su firma HMAC.
2. Comprueba sus propios permisos en el grupo; si faltan, avisa y cancela.
3. Envía «🧹 La limpieza del grupo ha comenzado». El message_id de ese aviso es
   el tope de la limpieza (lo posterior no se toca).
4. Borra por lotes de 100, del más nuevo al más viejo, el rango
   [último_limpiado + 1 .. tope]. En la primera limpieza el punto de partida
   es la primera marca que registró CEO. Lo que Telegram no permite borrar
   se omite. El mensaje fijado se conserva.
5. Envía «✅ La limpieza ha finalizado correctamente» (sin cantidades).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, ContextTypes

from ceo_cleaner.shared import JOB_CANCELLED, JOB_DONE, JOB_FAILED, verify_job
from config import settings
from database import CleanerJob, Database

logger = logging.getLogger(__name__)

MSG_STARTED = "🧹 La limpieza del grupo ha comenzado"
MSG_FINISHED = "✅ La limpieza ha finalizado correctamente"
MSG_NO_PERMS = (
    "⚠️ CEO Cleaner no tiene permiso para eliminar mensajes en este grupo, "
    "así que la limpieza se canceló. Un administrador debe darle el permiso «Eliminar mensajes»."
)
MSG_FAILED = "⚠️ La limpieza no pudo completarse. Se intentará de nuevo en la próxima hora programada."

BATCH_SIZE = 100          # máximo de deleteMessages
BATCH_DELAY = 1.0         # pausa entre lotes (límites de Telegram)
SINGLE_DELAY = 0.1        # pausa entre borrados individuales
MAX_RANGE = 30000         # tope de IDs por limpieza
MAX_EMPTY_BATCHES = 10    # lotes seguidos sin borrar nada -> el resto ya es inalcanzable
MAX_PARALLEL = 3          # grupos limpiándose a la vez
POLL_SECONDS = 5


class PermissionLost(Exception):
    pass


def _seconds(value) -> float:
    return value.total_seconds() if hasattr(value, "total_seconds") else float(value)


def _is_rights_error(exc: TelegramError) -> bool:
    text = str(exc).lower()
    return "rights" in text or "administrator" in text or "can_delete" in text


async def _send(bot, chat_id: int, text: str):
    for _ in range(3):
        try:
            return await bot.send_message(chat_id, text)
        except RetryAfter as exc:
            await asyncio.sleep(_seconds(exc.retry_after) + 1)
    return await bot.send_message(chat_id, text)


async def _delete_ids(bot, chat_id: int, ids: list[int]) -> bool:
    """Borra un lote. True si hubo progreso."""
    for _ in range(5):
        try:
            await bot.delete_messages(chat_id, ids)
            return True
        except RetryAfter as exc:
            await asyncio.sleep(_seconds(exc.retry_after) + 1)
        except Forbidden as exc:
            raise PermissionLost(str(exc)) from exc
        except BadRequest as exc:
            if _is_rights_error(exc):
                raise PermissionLost(str(exc)) from exc
            break  # algún ID no se puede borrar en bloque: se prueba uno por uno
        except TelegramError:
            await asyncio.sleep(2)

    deleted = 0
    for message_id in ids:
        try:
            await bot.delete_message(chat_id, message_id)
            deleted += 1
        except RetryAfter as exc:
            await asyncio.sleep(_seconds(exc.retry_after) + 1)
        except Forbidden as exc:
            raise PermissionLost(str(exc)) from exc
        except BadRequest as exc:
            if _is_rights_error(exc):
                raise PermissionLost(str(exc)) from exc
        except TelegramError:
            pass
        await asyncio.sleep(SINGLE_DELAY)
    return deleted > 0


async def _own_permissions_ok(bot, group_id: int, me_id: int) -> bool:
    try:
        member = await bot.get_chat_member(group_id, me_id)
    except TelegramError:
        return False
    return member.status == ChatMemberStatus.ADMINISTRATOR and bool(getattr(member, "can_delete_messages", False))


async def run_cleanup(application: Application, job: CleanerJob) -> None:
    bot = application.bot
    db: Database = application.bot_data["db"]
    me_id: int = application.bot_data["me_id"]
    gid = job.group_id
    tag = f"[grupo={gid} job={job.id}]"

    cfg = await db.get_cleaner_config(gid, settings.cleaner_timezone)
    if not cfg.enabled:
        await db.finish_cleaner_job(job.id, JOB_CANCELLED, "función desactivada")
        logger.info("%s Cancelada: la función está desactivada", tag)
        return

    if not await _own_permissions_ok(bot, gid, me_id):
        await db.finish_cleaner_job(job.id, JOB_CANCELLED, "sin permisos para eliminar mensajes")
        await db.update_cleaner_config(gid, last_run_at=int(time.time()), last_status="cancelada: sin permisos")
        logger.error("%s Cancelada: CEO Cleaner sin permisos", tag)
        try:
            await _send(bot, gid, MSG_NO_PERMS)
        except TelegramError:
            pass
        return

    try:
        started = await _send(bot, gid, MSG_STARTED)
    except TelegramError as exc:
        await db.finish_cleaner_job(job.id, JOB_FAILED, f"no se pudo enviar el aviso inicial: {exc}")
        logger.error("%s No se pudo enviar el aviso inicial: %s", tag, exc)
        return
    logger.info("%s Limpieza iniciada", tag)

    upper = started.message_id - 1
    try:
        lower: Optional[int] = (cfg.last_cleaned_id + 1) if cfg.last_cleaned_id else await db.get_cleaner_min_mark(gid)

        pinned_id = None
        try:
            pinned = (await bot.get_chat(gid)).pinned_message
            pinned_id = pinned.message_id if pinned else None
        except TelegramError:
            pass

        if lower is not None and lower <= upper:
            lower = max(lower, upper - MAX_RANGE + 1)
            empty = 0
            high = upper
            while high >= lower:
                low = max(lower, high - BATCH_SIZE + 1)
                ids = [i for i in range(low, high + 1) if i != pinned_id]
                high = low - 1
                if not ids:
                    continue
                empty = 0 if await _delete_ids(bot, gid, ids) else empty + 1
                if empty >= MAX_EMPTY_BATCHES:
                    break
                await asyncio.sleep(BATCH_DELAY)

        await _send(bot, gid, MSG_FINISHED)
        await db.update_cleaner_config(
            gid, settings.cleaner_timezone, last_run_at=int(time.time()), last_status="ok", last_cleaned_id=upper,
        )
        await db.clear_cleaner_marks(gid)
        await db.finish_cleaner_job(job.id, JOB_DONE)
        logger.info("%s Limpieza finalizada", tag)
    except PermissionLost as exc:
        await db.finish_cleaner_job(job.id, JOB_FAILED, f"permisos perdidos: {exc}")
        await db.update_cleaner_config(gid, last_run_at=int(time.time()), last_status="cancelada: sin permisos")
        logger.error("%s Permisos perdidos durante la limpieza: %s", tag, exc)
        try:
            await _send(bot, gid, MSG_NO_PERMS)
        except TelegramError:
            pass
    except Exception as exc:
        await db.finish_cleaner_job(job.id, JOB_FAILED, f"{type(exc).__name__}: {exc}")
        await db.update_cleaner_config(gid, last_run_at=int(time.time()), last_status="error")
        logger.exception("%s Error durante la limpieza", tag)
        try:
            await _send(bot, gid, MSG_FAILED)
        except TelegramError:
            pass


async def _execute(application: Application, job: CleanerJob) -> None:
    db: Database = application.bot_data["db"]
    try:
        if not verify_job(settings.cleaner_secret, job.group_id, job.requested_at, job.nonce, job.signature):
            await db.finish_cleaner_job(job.id, JOB_FAILED, "firma inválida")
            logger.warning("[grupo=%s job=%s] Trabajo rechazado: firma inválida", job.group_id, job.id)
            return
        await run_cleanup(application, job)
    except Exception:
        logger.exception("[grupo=%s job=%s] Fallo inesperado", job.group_id, job.id)
        try:
            await db.finish_cleaner_job(job.id, JOB_FAILED, "fallo inesperado")
        except Exception:
            pass
    finally:
        application.bot_data["active_jobs"] -= 1


async def poll_jobs(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    db: Database = application.bot_data["db"]
    while application.bot_data["active_jobs"] < MAX_PARALLEL:
        job = await db.claim_cleaner_job()
        if job is None:
            return
        application.bot_data["active_jobs"] += 1
        application.create_task(_execute(application, job), name=f"cleaner_{job.group_id}")
