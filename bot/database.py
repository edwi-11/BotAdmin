"""
database.py
Capa de acceso a datos usando SQLite de forma asíncrona (aiosqlite).
Crea automáticamente las tablas necesarias al iniciar.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);

CREATE TABLE IF NOT EXISTS bot_admins (
    group_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    granted_by  INTEGER NOT NULL,
    granted_at  INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS mod_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT NOT NULL,
    executor_id INTEGER NOT NULL,
    executor_name TEXT NOT NULL,
    target_id   INTEGER,
    target_name TEXT,
    group_id    INTEGER NOT NULL,
    group_name  TEXT,
    reason      TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mod_logs_group ON mod_logs (group_id);

CREATE TABLE IF NOT EXISTS afk (
    user_id     INTEGER PRIMARY KEY,
    first_name  TEXT NOT NULL,
    username    TEXT,
    group_id    INTEGER,
    group_name  TEXT,
    reason      TEXT,
    since       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS group_settings (
    group_id         INTEGER PRIMARY KEY,
    welcome_enabled  INTEGER NOT NULL DEFAULT 1,
    welcome_text     TEXT,
    goodbye_enabled  INTEGER NOT NULL DEFAULT 1,
    goodbye_text     TEXT,
    rules_text       TEXT,
    clean_welcome    INTEGER NOT NULL DEFAULT 1,
    afk_enabled      INTEGER NOT NULL DEFAULT 1,
    updated_at       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS known_groups (
    group_id    INTEGER PRIMARY KEY,
    title       TEXT,
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id          INTEGER NOT NULL,
    content_type      TEXT NOT NULL,
    text              TEXT,
    entities          TEXT NOT NULL DEFAULT '[]',
    file_id           TEXT,
    buttons           TEXT NOT NULL DEFAULT '[]',
    interval_seconds  INTEGER NOT NULL,
    pin               INTEGER NOT NULL DEFAULT 0,
    delete_previous   INTEGER NOT NULL DEFAULT 0,
    enabled           INTEGER NOT NULL DEFAULT 1,
    last_message_id   INTEGER,
    created_by        INTEGER NOT NULL,
    created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recurring_group ON recurring_messages (group_id);

CREATE TABLE IF NOT EXISTS banned_words (
    group_id  INTEGER NOT NULL,
    word      TEXT NOT NULL,
    added_by  INTEGER,
    added_at  INTEGER NOT NULL,
    PRIMARY KEY (group_id, word)
);

CREATE TABLE IF NOT EXISTS warnings (
    group_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    count       INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
"""

# Columnas que se añadieron después de la primera versión del esquema.
# Se agregan en caliente con ALTER TABLE para no romper bases de datos
# ya existentes que fueron creadas antes de que estas columnas existieran.
_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "group_settings": [
        ("afk_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("delete_join", "INTEGER NOT NULL DEFAULT 0"),
        ("delete_leave", "INTEGER NOT NULL DEFAULT 0"),
        ("delete_call", "INTEGER NOT NULL DEFAULT 0"),
        ("delete_commands", "INTEGER NOT NULL DEFAULT 0"),
        ("filter_punishment", "TEXT NOT NULL DEFAULT 'none'"),
        ("filter_mute_seconds", "INTEGER NOT NULL DEFAULT 0"),
        ("filter_delete", "INTEGER NOT NULL DEFAULT 1"),
        ("warn_limit", "INTEGER NOT NULL DEFAULT 3"),
        ("warn_action", "TEXT NOT NULL DEFAULT 'mute'"),
        ("warn_mute_seconds", "INTEGER NOT NULL DEFAULT 3600"),
    ],
}


@dataclass(slots=True)
class AfkRecord:
    user_id: int
    first_name: str
    username: Optional[str]
    group_id: Optional[int]
    group_name: Optional[str]
    reason: Optional[str]
    since: int


DEFAULT_WELCOME_TEXT = "👋 ¡Bienvenido/a {mention} a *{group}*! Nos alegra tenerte aquí."
DEFAULT_GOODBYE_TEXT = "👋 {name} ha salido del grupo. ¡Hasta pronto!"
DEFAULT_RULES_TEXT = "Aún no se han configurado reglas para este grupo."


@dataclass(slots=True)
class GroupSettings:
    group_id: int
    welcome_enabled: bool = True
    welcome_text: str = DEFAULT_WELCOME_TEXT
    goodbye_enabled: bool = True
    goodbye_text: str = DEFAULT_GOODBYE_TEXT
    rules_text: str = DEFAULT_RULES_TEXT
    clean_welcome: bool = True
    afk_enabled: bool = True
    delete_join: bool = False
    delete_leave: bool = False
    delete_call: bool = False
    delete_commands: bool = False
    filter_punishment: str = "none"     # none | mute | ban
    filter_mute_seconds: int = 0        # 0 = mute permanente
    filter_delete: bool = True
    warn_limit: int = 3                 # cantidad de advertencias antes de castigar
    warn_action: str = "mute"           # mute | kick | ban
    warn_mute_seconds: int = 3600       # 0 = mute permanente (solo si warn_action == mute)


@dataclass(slots=True)
class RecurringMessage:
    id: int
    group_id: int
    content_type: str
    text: Optional[str]
    entities: str
    file_id: Optional[str]
    buttons: str
    interval_seconds: int
    pin: bool
    delete_previous: bool
    enabled: bool
    last_message_id: Optional[int]
    created_by: int
    created_at: int


class Database:
    """Wrapper asíncrono sobre aiosqlite con métodos de dominio para el bot."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        await self._run_migrations()
        logger.info("Base de datos inicializada en %s", self._path)

    async def _run_migrations(self) -> None:
        """Añade columnas nuevas a tablas creadas por versiones anteriores del bot."""
        for table, columns in _MIGRATIONS.items():
            cursor = await self.conn.execute(f"PRAGMA table_info({table})")
            existing = {row["name"] for row in await cursor.fetchall()}
            for column, ddl in columns:
                if column not in existing:
                    await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                    logger.info("Migración: añadida columna %s.%s", table, column)
        await self.conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            logger.info("Conexión a base de datos cerrada.")

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("La base de datos no ha sido inicializada. Llama a connect() primero.")
        return self._conn

    # ------------------------------------------------------------------ #
    # Usuarios (cache para resolver @username -> user_id)
    # ------------------------------------------------------------------ #
    async def upsert_user(self, user_id: int, username: Optional[str], first_name: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO users (user_id, username, first_name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                updated_at = excluded.updated_at
            """,
            (user_id, username.lower() if username else None, first_name, int(time.time())),
        )
        await self.conn.commit()

    async def get_user_id_by_username(self, username: str) -> Optional[int]:
        username = username.lstrip("@").lower()
        cursor = await self.conn.execute(
            "SELECT user_id FROM users WHERE username = ?", (username,)
        )
        row = await cursor.fetchone()
        return int(row["user_id"]) if row else None

    async def get_user_display_name(self, user_id: int) -> Optional[str]:
        cursor = await self.conn.execute(
            "SELECT first_name FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row["first_name"] if row else None

    # ------------------------------------------------------------------ #
    # Administradores otorgados por el bot
    # ------------------------------------------------------------------ #
    async def add_bot_admin(self, group_id: int, user_id: int, granted_by: int) -> None:
        await self.conn.execute(
            """
            INSERT INTO bot_admins (group_id, user_id, granted_by, granted_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                granted_by = excluded.granted_by,
                granted_at = excluded.granted_at
            """,
            (group_id, user_id, granted_by, int(time.time())),
        )
        await self.conn.commit()

    async def remove_bot_admin(self, group_id: int, user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM bot_admins WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        await self.conn.commit()

    async def is_bot_granted_admin(self, group_id: int, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "SELECT 1 FROM bot_admins WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
        return (await cursor.fetchone()) is not None

    # ------------------------------------------------------------------ #
    # Logs de moderación
    # ------------------------------------------------------------------ #
    async def add_log(
        self,
        command: str,
        executor_id: int,
        executor_name: str,
        target_id: Optional[int],
        target_name: Optional[str],
        group_id: int,
        group_name: Optional[str],
        reason: Optional[str],
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO mod_logs
                (command, executor_id, executor_name, target_id, target_name,
                 group_id, group_name, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                command,
                executor_id,
                executor_name,
                target_id,
                target_name,
                group_id,
                group_name,
                reason,
                int(time.time()),
            ),
        )
        await self.conn.commit()
        logger.info(
            "CMD=%s executor=%s(%s) target=%s(%s) group=%s(%s) reason=%s",
            command, executor_name, executor_id, target_name, target_id,
            group_name, group_id, reason,
        )

    # ------------------------------------------------------------------ #
    # AFK / BRB
    # ------------------------------------------------------------------ #
    async def set_afk(
        self,
        user_id: int,
        first_name: str,
        username: Optional[str],
        group_id: Optional[int],
        group_name: Optional[str],
        reason: Optional[str],
    ) -> AfkRecord:
        since = int(time.time())
        await self.conn.execute(
            """
            INSERT INTO afk (user_id, first_name, username, group_id, group_name, reason, since)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = excluded.first_name,
                username = excluded.username,
                group_id = excluded.group_id,
                group_name = excluded.group_name,
                reason = excluded.reason,
                since = excluded.since
            """,
            (user_id, first_name, username, group_id, group_name, reason, since),
        )
        await self.conn.commit()
        return AfkRecord(user_id, first_name, username, group_id, group_name, reason, since)

    async def remove_afk(self, user_id: int) -> None:
        await self.conn.execute("DELETE FROM afk WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def get_all_afk(self) -> list[AfkRecord]:
        cursor = await self.conn.execute("SELECT * FROM afk")
        rows = await cursor.fetchall()
        return [
            AfkRecord(
                user_id=row["user_id"],
                first_name=row["first_name"],
                username=row["username"],
                group_id=row["group_id"],
                group_name=row["group_name"],
                reason=row["reason"],
                since=row["since"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # Bienvenida / Despedida / Reglamento
    # ------------------------------------------------------------------ #
    _SETTINGS_COLUMNS = {
        "welcome_enabled", "welcome_text", "goodbye_enabled",
        "goodbye_text", "rules_text", "clean_welcome", "afk_enabled",
        "delete_join", "delete_leave", "delete_call", "delete_commands",
        "filter_punishment", "filter_mute_seconds", "filter_delete",
        "warn_limit", "warn_action", "warn_mute_seconds",
    }

    async def get_group_settings(self, group_id: int) -> GroupSettings:
        cursor = await self.conn.execute(
            "SELECT * FROM group_settings WHERE group_id = ?", (group_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return GroupSettings(group_id=group_id)
        keys = row.keys()
        return GroupSettings(
            group_id=row["group_id"],
            welcome_enabled=bool(row["welcome_enabled"]),
            welcome_text=row["welcome_text"] or DEFAULT_WELCOME_TEXT,
            goodbye_enabled=bool(row["goodbye_enabled"]),
            goodbye_text=row["goodbye_text"] or DEFAULT_GOODBYE_TEXT,
            rules_text=row["rules_text"] or DEFAULT_RULES_TEXT,
            clean_welcome=bool(row["clean_welcome"]),
            afk_enabled=bool(row["afk_enabled"]) if row["afk_enabled"] is not None else True,
            delete_join=bool(row["delete_join"]) if "delete_join" in keys and row["delete_join"] is not None else False,
            delete_leave=bool(row["delete_leave"]) if "delete_leave" in keys and row["delete_leave"] is not None else False,
            delete_call=bool(row["delete_call"]) if "delete_call" in keys and row["delete_call"] is not None else False,
            delete_commands=bool(row["delete_commands"]) if "delete_commands" in keys and row["delete_commands"] is not None else False,
            filter_punishment=(row["filter_punishment"] if "filter_punishment" in keys and row["filter_punishment"] else "none"),
            filter_mute_seconds=(row["filter_mute_seconds"] if "filter_mute_seconds" in keys and row["filter_mute_seconds"] is not None else 0),
            filter_delete=bool(row["filter_delete"]) if "filter_delete" in keys and row["filter_delete"] is not None else True,
            warn_limit=(row["warn_limit"] if "warn_limit" in keys and row["warn_limit"] is not None else 3),
            warn_action=(row["warn_action"] if "warn_action" in keys and row["warn_action"] else "mute"),
            warn_mute_seconds=(row["warn_mute_seconds"] if "warn_mute_seconds" in keys and row["warn_mute_seconds"] is not None else 3600),
        )

    async def set_group_setting(self, group_id: int, column: str, value) -> None:
        if column not in self._SETTINGS_COLUMNS:
            raise ValueError(f"Columna de configuración no permitida: {column}")

        # Aseguramos que exista una fila para el grupo antes de actualizar.
        await self.conn.execute(
            """
            INSERT INTO group_settings (group_id, updated_at)
            VALUES (?, ?)
            ON CONFLICT(group_id) DO NOTHING
            """,
            (group_id, int(time.time())),
        )
        await self.conn.execute(
            f"UPDATE group_settings SET {column} = ?, updated_at = ? WHERE group_id = ?",
            (value, int(time.time()), group_id),
        )
        await self.conn.commit()

    async def reset_group_setting(self, group_id: int, text_column: str) -> None:
        """Restablece un campo de texto (welcome_text/goodbye_text/rules_text) a NULL (usa el valor por defecto)."""
        await self.set_group_setting(group_id, text_column, None)

    # ------------------------------------------------------------------ #
    # Grupos conocidos (usado por el menú de botones para saber en qué
    # grupos está el bot y poder ofrecerlos desde el chat privado).
    # ------------------------------------------------------------------ #
    async def upsert_group(self, group_id: int, title: Optional[str]) -> None:
        await self.conn.execute(
            """
            INSERT INTO known_groups (group_id, title, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                title = excluded.title,
                updated_at = excluded.updated_at
            """,
            (group_id, title, int(time.time())),
        )
        await self.conn.commit()

    async def get_known_groups(self) -> list[tuple[int, str]]:
        cursor = await self.conn.execute("SELECT group_id, title FROM known_groups ORDER BY title")
        rows = await cursor.fetchall()
        return [(row["group_id"], row["title"] or str(row["group_id"])) for row in rows]

    async def get_group_title(self, group_id: int) -> Optional[str]:
        cursor = await self.conn.execute(
            "SELECT title FROM known_groups WHERE group_id = ?", (group_id,)
        )
        row = await cursor.fetchone()
        return row["title"] if row else None

    # ------------------------------------------------------------------ #
    # Mensajes recurrentes
    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_recurring(row: aiosqlite.Row) -> RecurringMessage:
        return RecurringMessage(
            id=row["id"],
            group_id=row["group_id"],
            content_type=row["content_type"],
            text=row["text"],
            entities=row["entities"] or "[]",
            file_id=row["file_id"],
            buttons=row["buttons"] or "[]",
            interval_seconds=row["interval_seconds"],
            pin=bool(row["pin"]),
            delete_previous=bool(row["delete_previous"]),
            enabled=bool(row["enabled"]),
            last_message_id=row["last_message_id"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )

    async def add_recurring_message(
        self,
        group_id: int,
        content_type: str,
        text: Optional[str],
        entities: str,
        file_id: Optional[str],
        buttons: str,
        interval_seconds: int,
        pin: bool,
        delete_previous: bool,
        created_by: int,
    ) -> int:
        cursor = await self.conn.execute(
            """
            INSERT INTO recurring_messages
                (group_id, content_type, text, entities, file_id, buttons,
                 interval_seconds, pin, delete_previous, enabled, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                group_id, content_type, text, entities, file_id, buttons,
                interval_seconds, int(pin), int(delete_previous), created_by, int(time.time()),
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_recurring_message(self, rec_id: int) -> Optional[RecurringMessage]:
        cursor = await self.conn.execute("SELECT * FROM recurring_messages WHERE id = ?", (rec_id,))
        row = await cursor.fetchone()
        return self._row_to_recurring(row) if row else None

    async def get_recurring_messages(self, group_id: int) -> list[RecurringMessage]:
        cursor = await self.conn.execute(
            "SELECT * FROM recurring_messages WHERE group_id = ? ORDER BY id", (group_id,)
        )
        rows = await cursor.fetchall()
        return [self._row_to_recurring(r) for r in rows]

    async def get_all_enabled_recurring_messages(self) -> list[RecurringMessage]:
        cursor = await self.conn.execute("SELECT * FROM recurring_messages WHERE enabled = 1")
        rows = await cursor.fetchall()
        return [self._row_to_recurring(r) for r in rows]

    async def set_recurring_enabled(self, rec_id: int, enabled: bool) -> None:
        await self.conn.execute(
            "UPDATE recurring_messages SET enabled = ? WHERE id = ?", (int(enabled), rec_id)
        )
        await self.conn.commit()

    async def set_recurring_pin(self, rec_id: int, pin: bool) -> None:
        await self.conn.execute(
            "UPDATE recurring_messages SET pin = ? WHERE id = ?", (int(pin), rec_id)
        )
        await self.conn.commit()

    async def set_recurring_delete_previous(self, rec_id: int, delete_previous: bool) -> None:
        await self.conn.execute(
            "UPDATE recurring_messages SET delete_previous = ? WHERE id = ?", (int(delete_previous), rec_id)
        )
        await self.conn.commit()

    async def set_recurring_last_message(self, rec_id: int, message_id: Optional[int]) -> None:
        await self.conn.execute(
            "UPDATE recurring_messages SET last_message_id = ? WHERE id = ?", (message_id, rec_id)
        )
        await self.conn.commit()

    async def delete_recurring_message(self, rec_id: int) -> None:
        await self.conn.execute("DELETE FROM recurring_messages WHERE id = ?", (rec_id,))
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # Palabras prohibidas
    # ------------------------------------------------------------------ #
    async def add_banned_word(self, group_id: int, word: str, added_by: int) -> bool:
        word = word.strip().lower()
        if not word:
            return False
        await self.conn.execute(
            """
            INSERT INTO banned_words (group_id, word, added_by, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, word) DO NOTHING
            """,
            (group_id, word, added_by, int(time.time())),
        )
        await self.conn.commit()
        return True

    async def remove_banned_word(self, group_id: int, word: str) -> bool:
        word = word.strip().lower()
        cursor = await self.conn.execute(
            "DELETE FROM banned_words WHERE group_id = ? AND word = ?", (group_id, word)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_banned_words(self, group_id: int) -> list[str]:
        cursor = await self.conn.execute(
            "SELECT word FROM banned_words WHERE group_id = ? ORDER BY word", (group_id,)
        )
        rows = await cursor.fetchall()
        return [row["word"] for row in rows]

    # ------------------------------------------------------------------ #
    # Advertencias (warnings)
    # ------------------------------------------------------------------ #
    async def add_warning(self, group_id: int, user_id: int) -> int:
        """Suma 1 advertencia y devuelve el nuevo total."""
        await self.conn.execute(
            """
            INSERT INTO warnings (group_id, user_id, count, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                count = count + 1,
                updated_at = excluded.updated_at
            """,
            (group_id, user_id, int(time.time())),
        )
        await self.conn.commit()
        return await self.get_warning_count(group_id, user_id)

    async def remove_warning(self, group_id: int, user_id: int) -> int:
        """Resta 1 advertencia (sin bajar de 0) y devuelve el nuevo total."""
        current = await self.get_warning_count(group_id, user_id)
        new_count = max(0, current - 1)
        await self.conn.execute(
            """
            INSERT INTO warnings (group_id, user_id, count, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                count = excluded.count,
                updated_at = excluded.updated_at
            """,
            (group_id, user_id, new_count, int(time.time())),
        )
        await self.conn.commit()
        return new_count

    async def get_warning_count(self, group_id: int, user_id: int) -> int:
        cursor = await self.conn.execute(
            "SELECT count FROM warnings WHERE group_id = ? AND user_id = ?", (group_id, user_id)
        )
        row = await cursor.fetchone()
        return int(row["count"]) if row else 0

    async def reset_warnings(self, group_id: int, user_id: int) -> None:
        await self.conn.execute(
            "DELETE FROM warnings WHERE group_id = ? AND user_id = ?", (group_id, user_id)
        )
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # Actualización completa de un mensaje recurrente ya existente
    # (usado por el editor de un solo menú: foto/texto/botones/intervalo).
    # ------------------------------------------------------------------ #
    async def update_recurring_message(self, rec_id: int, **fields) -> None:
        allowed = {
            "content_type", "text", "entities", "file_id", "buttons",
            "interval_seconds", "pin", "delete_previous",
        }
        set_fields = {k: v for k, v in fields.items() if k in allowed}
        if not set_fields:
            return
        for bool_field in ("pin", "delete_previous"):
            if bool_field in set_fields:
                set_fields[bool_field] = int(set_fields[bool_field])
        columns = ", ".join(f"{k} = ?" for k in set_fields)
        values = list(set_fields.values()) + [rec_id]
        await self.conn.execute(
            f"UPDATE recurring_messages SET {columns} WHERE id = ?", values
        )
        await self.conn.commit()

