"""
utils/emoji_db.py
Persistencia en SQLite de los packs de emojis personalizados (/emoji, /emojis):
guarda a qué usuario pertenece cada pack que creó el bot.

Está en un archivo aparte (y no dentro de database.py) a propósito: así
agregar esta función NO obliga a reemplazar database.py. Usa la misma
conexión aiosqlite del bot (`db.conn`) y crea su propia tabla sola
(CREATE TABLE IF NOT EXISTS), por lo que no hace falta migrar nada a mano.

Tabla emoji_packs:
    kind = 'personal'  -> pack del usuario donde /emoji va agregando de a uno
                          (uno por formato y volumen).
    kind = 'converted' -> pack generado por /emojis a partir de un pack de
                          stickers (source_set guarda el pack original).
"""
from __future__ import annotations

import time
from typing import Optional

import aiosqlite

from database import Database

_SQL_TABLA = """
CREATE TABLE IF NOT EXISTS emoji_packs (
    pack_name      TEXT PRIMARY KEY,
    user_id        INTEGER NOT NULL,
    kind           TEXT NOT NULL,   -- 'personal' | 'converted'
    format         TEXT NOT NULL,   -- 'static' | 'video' | 'animated'
    volume         INTEGER NOT NULL DEFAULT 1,
    title          TEXT,
    source_set     TEXT,
    sticker_count  INTEGER NOT NULL DEFAULT 0,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emoji_packs_user ON emoji_packs (user_id, kind, format, volume);
"""


async def asegurar_tabla(db: Database) -> None:
    """Crea la tabla si todavía no existe (es barato e idempotente: se
    puede llamar al inicio de cada tarea)."""
    await db.conn.executescript(_SQL_TABLA)
    await db.conn.commit()


async def get_latest_emoji_pack(db: Database, user_id: int, fmt: str) -> Optional[aiosqlite.Row]:
    """Pack personal (el de /emoji) de mayor volumen que tiene el usuario
    para ese formato, o None si todavía no tiene ninguno."""
    cursor = await db.conn.execute(
        "SELECT * FROM emoji_packs WHERE user_id = ? AND kind = 'personal' AND format = ? "
        "ORDER BY volume DESC LIMIT 1",
        (user_id, fmt),
    )
    return await cursor.fetchone()


async def create_emoji_pack(
    db: Database, pack_name: str, user_id: int, kind: str, fmt: str, volume: int,
    title: Optional[str], source_set: Optional[str], sticker_count: int = 0,
) -> None:
    """Registra un pack nuevo. Si ya estaba registrado se ignora
    (idempotente: sirve cuando se "adopta" un pack que ya existía en Telegram)."""
    now = int(time.time())
    await db.conn.execute(
        "INSERT OR IGNORE INTO emoji_packs "
        "(pack_name, user_id, kind, format, volume, title, source_set, sticker_count, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pack_name, user_id, kind, fmt, volume, title, source_set, sticker_count, now, now),
    )
    await db.conn.commit()


async def bump_emoji_pack_count(db: Database, pack_name: str, amount: int = 1) -> None:
    await db.conn.execute(
        "UPDATE emoji_packs SET sticker_count = sticker_count + ?, updated_at = ? WHERE pack_name = ?",
        (amount, int(time.time()), pack_name),
    )
    await db.conn.commit()


async def set_emoji_pack_count(db: Database, pack_name: str, count: int) -> None:
    await db.conn.execute(
        "UPDATE emoji_packs SET sticker_count = ?, updated_at = ? WHERE pack_name = ?",
        (count, int(time.time()), pack_name),
    )
    await db.conn.commit()


async def list_emoji_packs(db: Database, user_id: int) -> list[aiosqlite.Row]:
    """Todos los packs de emojis que el bot creó para este usuario."""
    cursor = await db.conn.execute(
        "SELECT * FROM emoji_packs WHERE user_id = ? ORDER BY created_at",
        (user_id,),
    )
    return list(await cursor.fetchall())
