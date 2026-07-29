"""
utils/groups.py
Helper compartido para /menu, /owner y /grupos: toma la lista cruda de
known_groups y la verifica contra Telegram de verdad, en vez de confiar
ciegamente en lo que quedó guardado en la base.

Por qué hace falta:
- known_groups nunca se limpiaba cuando el bot era expulsado de un grupo,
  cuando alguien borraba el grupo, o cuando un grupo se migraba a
  supergrupo (Telegram le cambia el group_id y el viejo queda huérfano).
  Esas filas viejas se quedaban ahí para siempre.
- Para el propietario, is_chat_admin() devuelve True sin chequear si el
  bot sigue siendo miembro del grupo, así que esos grupos fantasma
  terminaban apareciendo en /menu y /owner con el nombre en blanco
  (fallback numérico, el group_id como texto).

Esta función pide cada chat a la API de Telegram; si ya no es accesible
(Forbidden, "chat not found", etc.) lo borra de known_groups en el momento
y no lo incluye en el resultado. De paso refresca el título guardado si
cambió. Así la lista se autolimpia sola cada vez que se usa.
"""
from __future__ import annotations

import logging

from telegram import Bot
from telegram.error import BadRequest, Forbidden, TelegramError

from database import Database

logger = logging.getLogger(__name__)


async def get_verified_groups(bot: Bot, db: Database) -> list[tuple[int, str]]:
    """Devuelve solo los grupos donde el bot sigue siendo miembro de verdad,
    sin duplicados y con el título actualizado. Limpia known_groups de paso."""
    raw_groups = await db.get_known_groups()

    verified: list[tuple[int, str]] = []
    seen_ids: set[int] = set()

    for group_id, stored_title in raw_groups:
        if group_id in seen_ids:
            continue
        seen_ids.add(group_id)

        try:
            chat = await bot.get_chat(group_id)
        except (Forbidden, BadRequest):
            # El bot ya no está ahí (lo sacaron, el grupo se borró, o
            # migró a supergrupo y este id quedó viejo). Lo limpiamos.
            await db.remove_group(group_id)
            logger.info("Grupo %s ya no accesible, eliminado de known_groups", group_id)
            continue
        except TelegramError as exc:
            # Error de red/rate-limit puntual: no lo borramos (podría ser
            # temporal), pero tampoco lo mostramos esta vez.
            logger.warning("No se pudo verificar el grupo %s (%s), se omite por ahora", group_id, exc)
            continue

        title = chat.title or stored_title or str(group_id)
        if chat.title and chat.title != stored_title:
            await db.upsert_group(group_id, chat.title)

        verified.append((group_id, title))

    verified.sort(key=lambda item: item[1].lower())
    return verified
