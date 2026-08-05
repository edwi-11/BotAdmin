"""
utils/groups.py
Helper compartido para /menu, /owner y /grupos: toma la lista cruda de
known_groups y la verifica contra Telegram de verdad, en vez de confiar
ciegamente en lo que quedó guardado en la base.

Por qué hace falta:
- known_groups nunca se limpiaba cuando el bot era expulsado de un grupo
  o cuando alguien borraba el grupo. Esas filas viejas se quedaban ahí
  para siempre.
- Para el propietario, is_chat_admin() devuelve True sin chequear si el
  bot sigue siendo miembro del grupo, así que esos grupos fantasma
  terminaban apareciendo en /menu y /owner con el nombre en blanco
  (fallback numérico, el group_id como texto).

`get_groups_report()` es la función central: pide cada chat a la API de
Telegram y clasifica cada grupo en un estado (activo / expulsado /
sin_acceso), de paso refresca el título guardado si cambió y limpia
known_groups SOLO cuando Telegram confirma que el grupo ya no es
accesible (expulsado o borrado). Un error transitorio (rate limit,
timeout, etc.) NO borra el grupo ni lo hace desaparecer del listado: se
muestra igual, con el título/ID que ya teníamos guardado y el estado
"sin acceso", en vez de desaparecer en silencio como pasaba antes (ese
era el motivo de que a veces algunos grupos donde el bot seguía adentro
"no aparecieran" en /grupos, /owner o /menu — bastaba un error puntual de
red durante la verificación de ESE grupo para que se descartara del todo).

`get_verified_groups()` se mantiene por compatibilidad con /owner y
/menu (que solo necesitan la lista de grupos realmente activos, no el
reporte completo): es un filtro sobre `get_groups_report()`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from telegram import Bot
from telegram.error import BadRequest, Forbidden, TelegramError

from database import Database

logger = logging.getLogger(__name__)

# Fragmentos de mensaje de error que Telegram devuelve cuando el chat ya
# no existe de verdad (borrado, o el bot nunca fue miembro). Cualquier
# otro BadRequest se trata como falla transitoria, no como "grupo
# desaparecido", para no borrar de la base algo que podría ser recuperable.
_PERMANENTLY_GONE_HINTS = ("chat not found", "group chat was deactivated")


@dataclass
class GroupStatus:
    group_id: int
    title: str
    status: str  # "activo" | "expulsado" | "sin_acceso"
    member_count: Optional[int] = None

    @property
    def status_label(self) -> str:
        return {
            "activo": "✅ Activo",
            "expulsado": "🚫 Bot expulsado",
            "sin_acceso": "⚠️ Sin acceso",
        }[self.status]


async def _resolve_group(bot: Bot, db: Database, group_id: int, stored_title: Optional[str]) -> GroupStatus:
    fallback_title = stored_title or f"Grupo {group_id}"

    try:
        chat = await bot.get_chat(group_id)
    except Forbidden:
        # Nos expulsaron del grupo (o el usuario baneó al bot). Confirmado
        # por Telegram: ya no hace falta seguir guardándolo.
        await db.remove_group(group_id)
        logger.info("Grupo %s: expulsado, eliminado de known_groups", group_id)
        return GroupStatus(group_id, fallback_title, "expulsado")
    except BadRequest as exc:
        if any(hint in str(exc).lower() for hint in _PERMANENTLY_GONE_HINTS):
            await db.remove_group(group_id)
            logger.info("Grupo %s ya no existe (%s), eliminado de known_groups", group_id, exc)
            return GroupStatus(group_id, fallback_title, "expulsado")
        # Otro BadRequest (rate limit, chat temporalmente inaccesible,
        # etc.): no lo borramos, se muestra como "sin acceso" con los
        # datos que ya teníamos.
        logger.warning("Grupo %s: BadRequest transitorio (%s), se muestra como sin acceso", group_id, exc)
        return GroupStatus(group_id, fallback_title, "sin_acceso")
    except TelegramError as exc:
        # Error de red/rate-limit puntual: no lo borramos (podría ser
        # temporal) y tampoco lo ocultamos del listado.
        logger.warning("No se pudo verificar el grupo %s (%s), se muestra como sin acceso", group_id, exc)
        return GroupStatus(group_id, fallback_title, "sin_acceso")

    title = chat.title or fallback_title
    if chat.title and chat.title != stored_title:
        await db.upsert_group(group_id, chat.title)

    member_count: Optional[int] = None
    try:
        member_count = await bot.get_chat_member_count(group_id)
    except TelegramError as exc:
        logger.info("No pude obtener la cantidad de miembros de %s: %s", group_id, exc)

    return GroupStatus(group_id, title, "activo", member_count)


async def get_groups_report(bot: Bot, db: Database) -> list[GroupStatus]:
    """Devuelve el estado real (contra Telegram) de TODOS los grupos
    guardados, sin duplicados, ordenados con los activos primero y
    alfabéticamente por título dentro de cada grupo de estado."""
    raw_groups = await db.get_known_groups()

    seen_ids: set[int] = set()
    report: list[GroupStatus] = []
    for group_id, stored_title in raw_groups:
        if group_id in seen_ids:
            # No debería pasar (group_id es PRIMARY KEY en known_groups),
            # pero por las dudas nunca mostramos el mismo grupo dos veces.
            continue
        seen_ids.add(group_id)
        report.append(await _resolve_group(bot, db, group_id, stored_title))

    report.sort(key=lambda g: (g.status != "activo", g.title.lower()))
    return report


async def get_verified_groups(bot: Bot, db: Database) -> list[tuple[int, str]]:
    """Devuelve solo los grupos donde el bot sigue siendo miembro de verdad
    (para /owner y /menu, que necesitan elegir un grupo real, no un
    reporte). Usa get_groups_report() como única fuente de verdad."""
    report = await get_groups_report(bot, db)
    verified = [(g.group_id, g.title) for g in report if g.status == "activo"]
    verified.sort(key=lambda item: item[1].lower())
    return verified
