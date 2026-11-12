"""
anuncios_db.py
Base de datos SQLITE del sistema de anuncios (bot de moderación).
Archivo propio: database/anuncios.db (no toca bot.db ni lo usa el bot de broadcast).
NOTA: vive en la raíz (no en database/) porque ya existe database.py y un
paquete "database.anuncios_db" chocaría con él.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS anc_anuncios(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    tipo TEXT NOT NULL DEFAULT 'texto',
    fecha INTEGER NOT NULL,
    preview TEXT NOT NULL DEFAULT '',
    deleted INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 0,
    mode TEXT NOT NULL DEFAULT 'manual',
    interval_min INTEGER NOT NULL DEFAULT 0,
    every_msgs INTEGER NOT NULL DEFAULT 0,
    cooldown_min INTEGER NOT NULL DEFAULT 0,
    pin INTEGER NOT NULL DEFAULT 0,
    unpin_h INTEGER NOT NULL DEFAULT 0,
    silent INTEGER NOT NULL DEFAULT 0,
    del_prev INTEGER NOT NULL DEFAULT 0,
    end_ts INTEGER,
    next_run INTEGER,
    UNIQUE(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS anc_grupos(
    chat_id INTEGER PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    selected INTEGER NOT NULL DEFAULT 1,
    blacklisted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS anc_envios(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anuncio_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER,
    ts INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    unpin_at INTEGER,
    cleaned INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_anc_env_a ON anc_envios(anuncio_id, chat_id);
CREATE INDEX IF NOT EXISTS ix_anc_env_unpin ON anc_envios(unpin_at) WHERE unpin_at IS NOT NULL AND cleaned = 0;
CREATE TABLE IF NOT EXISTS anc_counters(
    anuncio_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    last_sent INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(anuncio_id, chat_id)
);
CREATE TABLE IF NOT EXISTS anc_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_ANUNCIO_FIELDS = {
    "active", "mode", "interval_min", "every_msgs", "cooldown_min", "pin",
    "unpin_h", "silent", "del_prev", "end_ts", "next_run", "deleted",
    "tipo", "preview",
}


class AnunciosDB:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    async def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _one(self, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def _run(self, sql: str, params: tuple = ()) -> None:
        async with self._lock:
            await self.conn.execute(sql, params)
            await self.conn.commit()

    # ---------------- Ajustes ----------------
    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self._one("SELECT value FROM anc_settings WHERE key=?", (key,))
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self._run(
            "INSERT INTO anc_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)),
        )

    # ---------------- Anuncios ----------------
    async def upsert_anuncio(self, chat_id: int, message_id: int, tipo: str, fecha: int, preview: str) -> None:
        await self._run(
            "INSERT INTO anc_anuncios(chat_id,message_id,tipo,fecha,preview) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id,message_id) DO UPDATE SET tipo=excluded.tipo, preview=excluded.preview",
            (chat_id, message_id, tipo, fecha, preview),
        )

    async def get_anuncio(self, aid: int) -> Optional[dict[str, Any]]:
        return await self._one("SELECT * FROM anc_anuncios WHERE id=?", (aid,))

    async def list_anuncios(self, offset: int, limit: int) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM anc_anuncios WHERE deleted=0 ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
        )

    async def count_anuncios(self) -> int:
        row = await self._one("SELECT COUNT(*) c FROM anc_anuncios WHERE deleted=0")
        return row["c"] if row else 0

    async def count_active(self) -> int:
        row = await self._one("SELECT COUNT(*) c FROM anc_anuncios WHERE deleted=0 AND active=1")
        return row["c"] if row else 0

    async def set_fields(self, aid: int, **fields: Any) -> None:
        cols = [k for k in fields if k in _ANUNCIO_FIELDS]
        if not cols:
            return
        sql = "UPDATE anc_anuncios SET " + ",".join(f"{c}=?" for c in cols) + " WHERE id=?"
        await self._run(sql, tuple(fields[c] for c in cols) + (aid,))

    async def mark_deleted_by_msg(self, chat_id: int, message_id: int) -> None:
        await self._run(
            "UPDATE anc_anuncios SET deleted=1, active=0 WHERE chat_id=? AND message_id=?", (chat_id, message_id)
        )

    async def active_by_mode(self, mode: str) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM anc_anuncios WHERE deleted=0 AND active=1 AND mode=?", (mode,)
        )

    async def due_time(self, now: int) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM anc_anuncios WHERE deleted=0 AND active=1 AND mode='time' AND interval_min>0 "
            "AND next_run IS NOT NULL AND next_run<=? AND (end_ts IS NULL OR end_ts>?)", (now, now),
        )

    async def fix_time_without_next(self, now: int) -> None:
        await self._run(
            "UPDATE anc_anuncios SET next_run=?+interval_min*60 WHERE deleted=0 AND active=1 "
            "AND mode='time' AND interval_min>0 AND next_run IS NULL", (now,),
        )

    async def expire_ended(self, now: int) -> None:
        await self._run(
            "UPDATE anc_anuncios SET active=0 WHERE active=1 AND end_ts IS NOT NULL AND end_ts<=?", (now,)
        )

    # ---------------- Grupos ----------------
    async def ensure_group(self, chat_id: int, title: str) -> None:
        await self._run(
            "INSERT OR IGNORE INTO anc_grupos(chat_id,title) VALUES(?,?)", (chat_id, title or str(chat_id))
        )

    async def upsert_group(self, chat_id: int, title: str, active: int) -> None:
        await self._run(
            "INSERT INTO anc_grupos(chat_id,title,active) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, active=excluded.active",
            (chat_id, title or str(chat_id), active),
        )

    async def set_group_active(self, chat_id: int, active: int) -> None:
        await self._run("UPDATE anc_grupos SET active=? WHERE chat_id=?", (active, chat_id))

    async def migrate_group(self, old_id: int, new_id: int) -> None:
        async with self._lock:
            await self.conn.execute("UPDATE anc_grupos SET active=0 WHERE chat_id=?", (old_id,))
            await self.conn.execute(
                "INSERT OR IGNORE INTO anc_grupos(chat_id,title,selected,blacklisted) "
                "SELECT ?,title,selected,blacklisted FROM anc_grupos WHERE chat_id=?", (new_id, old_id),
            )
            await self.conn.commit()

    async def all_group_ids(self) -> list[int]:
        rows = await self._all("SELECT chat_id FROM anc_grupos")
        return [r["chat_id"] for r in rows]

    async def delete_group(self, chat_id: int) -> None:
        async with self._lock:
            await self.conn.execute("DELETE FROM anc_grupos WHERE chat_id=?", (chat_id,))
            await self.conn.execute("DELETE FROM anc_counters WHERE chat_id=?", (chat_id,))
            await self.conn.commit()

    async def update_group(self, chat_id: int, title: str, active: int) -> None:
        await self._run("UPDATE anc_grupos SET title=?, active=? WHERE chat_id=?", (title or str(chat_id), active, chat_id))

    async def get_group(self, chat_id: int) -> Optional[dict[str, Any]]:
        return await self._one("SELECT * FROM anc_grupos WHERE chat_id=?", (chat_id,))

    async def list_groups(self, offset: int, limit: int) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM anc_grupos WHERE active=1 ORDER BY title COLLATE NOCASE LIMIT ? OFFSET ?",
            (limit, offset),
        )

    async def group_counts(self) -> dict[str, int]:
        row = await self._one(
            "SELECT COUNT(*) total, SUM(active=1) act, SUM(active=1 AND selected=1 AND blacklisted=0) sel, "
            "SUM(active=1 AND blacklisted=1) bl FROM anc_grupos"
        )
        return {k: (row[k] or 0) for k in ("total", "act", "sel", "bl")} if row else {"total": 0, "act": 0, "sel": 0, "bl": 0}

    async def toggle_group(self, chat_id: int, col: str) -> None:
        if col not in ("selected", "blacklisted"):
            return
        await self._run(f"UPDATE anc_grupos SET {col}=1-{col} WHERE chat_id=?", (chat_id,))

    async def set_all_selected(self, val: int) -> None:
        await self._run("UPDATE anc_grupos SET selected=? WHERE active=1", (val,))

    async def target_groups(self) -> list[dict[str, Any]]:
        """Destinos de los envíos programados: activos + seleccionados + fuera de lista negra."""
        return await self._all(
            "SELECT * FROM anc_grupos WHERE active=1 AND selected=1 AND blacklisted=0 ORDER BY title COLLATE NOCASE"
        )

    async def all_groups_sendable(self) -> list[dict[str, Any]]:
        """Para /enviar 'todos': activos y fuera de lista negra (ignora el check ✅/⬜)."""
        return await self._all(
            "SELECT * FROM anc_grupos WHERE active=1 AND blacklisted=0 ORDER BY title COLLATE NOCASE"
        )

    # ---------------- Envíos ----------------
    async def add_envio(self, aid: int, chat_id: int, message_id: Optional[int], ok: bool,
                        pinned: bool = False, unpin_at: Optional[int] = None, error: str = "") -> None:
        await self._run(
            "INSERT INTO anc_envios(anuncio_id,chat_id,message_id,ts,ok,pinned,unpin_at,error) VALUES(?,?,?,?,?,?,?,?)",
            (aid, chat_id, message_id, int(time.time()), int(ok), int(pinned), unpin_at, error[:200]),
        )

    async def last_envio(self, aid: int, chat_id: int) -> Optional[dict[str, Any]]:
        return await self._one(
            "SELECT * FROM anc_envios WHERE anuncio_id=? AND chat_id=? AND ok=1 AND cleaned=0 "
            "AND message_id IS NOT NULL ORDER BY id DESC LIMIT 1", (aid, chat_id),
        )

    async def mark_cleaned(self, envio_id: int) -> None:
        await self._run("UPDATE anc_envios SET cleaned=1 WHERE id=?", (envio_id,))

    async def due_unpins(self, now: int, limit: int = 50) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM anc_envios WHERE unpin_at IS NOT NULL AND unpin_at<=? AND cleaned=0 LIMIT ?",
            (now, limit),
        )

    async def stats(self, offset: int, limit: int) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT a.id, a.tipo, a.preview, "
            "(SELECT COUNT(DISTINCT chat_id) FROM anc_envios e WHERE e.anuncio_id=a.id AND e.ok=1) recibidos, "
            "(SELECT COUNT(*) FROM anc_envios e WHERE e.anuncio_id=a.id AND e.ok=1) envios_ok, "
            "(SELECT COUNT(*) FROM anc_envios e WHERE e.anuncio_id=a.id AND e.ok=0) fallidos, "
            "(SELECT MAX(ts) FROM anc_envios e WHERE e.anuncio_id=a.id AND e.ok=1) ultimo "
            "FROM anc_anuncios a WHERE a.deleted=0 ORDER BY a.id DESC LIMIT ? OFFSET ?", (limit, offset),
        )

    # ---------------- Contadores (modo por mensajes) ----------------
    async def bump(self, aid: int, chat_id: int) -> tuple[int, int]:
        async with self._lock:
            await self.conn.execute(
                "INSERT INTO anc_counters(anuncio_id,chat_id,count) VALUES(?,?,1) "
                "ON CONFLICT(anuncio_id,chat_id) DO UPDATE SET count=count+1", (aid, chat_id),
            )
            cur = await self.conn.execute(
                "SELECT count,last_sent FROM anc_counters WHERE anuncio_id=? AND chat_id=?", (aid, chat_id)
            )
            row = await cur.fetchone()
            await self.conn.commit()
        return (row["count"], row["last_sent"]) if row else (0, 0)

    async def reset_counter(self, aid: int, chat_id: int, ts: int) -> None:
        await self._run(
            "INSERT INTO anc_counters(anuncio_id,chat_id,count,last_sent) VALUES(?,?,0,?) "
            "ON CONFLICT(anuncio_id,chat_id) DO UPDATE SET count=0, last_sent=excluded.last_sent",
            (aid, chat_id, ts),
        )
