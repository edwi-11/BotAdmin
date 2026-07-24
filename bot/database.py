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

-- Palabras/emojis prohibidos para la "Moderación extrema de usuarios":
-- a diferencia de banned_words (que filtra el TEXTO de los mensajes),
-- esta lista se usa para revisar el nombre, usuario, descripción (bio) y
-- foto de perfil de quien pide entrar al grupo, al momento de aceptar su
-- solicitud de ingreso (ver handlers/xmod.py).
CREATE TABLE IF NOT EXISTS xmod_words (
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

CREATE TABLE IF NOT EXISTS freed_users (
    group_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    freed_by   INTEGER,
    freed_at   INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS economy (
    group_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    balance       INTEGER NOT NULL DEFAULT 0,
    bank          INTEGER NOT NULL DEFAULT 0,
    xp            INTEGER NOT NULL DEFAULT 0,
    job           TEXT,
    shield_until  INTEGER NOT NULL DEFAULT 0,
    daily_streak  INTEGER NOT NULL DEFAULT 0,
    last_daily    INTEGER NOT NULL DEFAULT 0,
    updated_at    INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_economy_balance ON economy (group_id, balance);

CREATE TABLE IF NOT EXISTS economy_cooldowns (
    group_id  INTEGER NOT NULL,
    user_id   INTEGER NOT NULL,
    action    TEXT NOT NULL,
    last_ts   INTEGER NOT NULL,
    PRIMARY KEY (group_id, user_id, action)
);

CREATE TABLE IF NOT EXISTS broadcast_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_type    TEXT NOT NULL,
    text            TEXT,
    entities        TEXT NOT NULL DEFAULT '[]',
    file_id         TEXT,
    buttons         TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending | sent
    created_by      INTEGER,
    created_at      INTEGER NOT NULL,
    sent_at         INTEGER,
    sent_count      INTEGER NOT NULL DEFAULT 0,
    failed_count    INTEGER NOT NULL DEFAULT 0
);

-- Mensajes secretos enviados por modo en línea (@bot @usuario texto),
-- al estilo @mensajesecretobot. `targets` guarda los @usuarios permitidos
-- separados por comas (en minúsculas, sin @). `text` se pone en NULL en
-- cuanto un mensaje autodestructivo es leído por alguien autorizado.
CREATE TABLE IF NOT EXISTS secret_messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    inline_message_id TEXT,
    author_id         INTEGER NOT NULL,
    author_name       TEXT NOT NULL,
    author_username   TEXT,
    targets           TEXT NOT NULL,
    text              TEXT,
    self_destruct     INTEGER NOT NULL DEFAULT 0,
    created_at        INTEGER NOT NULL,
    read_at           INTEGER,
    read_by_id        INTEGER,
    read_by_name      TEXT
);

-- Donaciones reales en Telegram Stars (/donar).
CREATE TABLE IF NOT EXISTS donations (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                    INTEGER NOT NULL,
    name                       TEXT NOT NULL,
    username                   TEXT,
    chat_id                    INTEGER,
    amount                     INTEGER NOT NULL,
    telegram_payment_charge_id TEXT,
    created_at                 INTEGER NOT NULL
);

-- Solicitudes de ingreso pendientes (grupos con "Aprobar nuevos
-- miembros" activado). Telegram solo avisa una por una en vivo (update
-- chat_join_request) y no deja pedir la lista completa después, así que
-- las vamos guardando acá para poder aprobarlas en lote con /aceptar.
CREATE TABLE IF NOT EXISTS join_requests (
    chat_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    name         TEXT NOT NULL,
    username     TEXT,
    requested_at INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_join_requests_pending ON join_requests (chat_id, status, requested_at);

-- Estado del captcha de edad por usuario y grupo. Una fila por (grupo,
-- usuario): se crea en 'pending' apenas se detecta y borra su primer
-- mensaje, y pasa a 'passed' o 'rejected' cuando responde por privado (o
-- se queda en 'pending' para siempre si nunca contesta). Mientras exista
-- una fila para ese (grupo, usuario) no se lo vuelve a interceptar.
CREATE TABLE IF NOT EXISTS captcha_state (
    group_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | passed | rejected
    user_name    TEXT,
    username     TEXT,
    group_title  TEXT,
    age          INTEGER,
    created_at   INTEGER NOT NULL,
    resolved_at  INTEGER,
    PRIMARY KEY (group_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_captcha_state_user ON captcha_state (user_id, status);

-- Reportes creados con /reportar o escribiendo "@admin" (con o sin motivo).
-- Cada reporte se notifica por privado a todos los administradores del
-- grupo; "status" se alterna entre 'pending' y 'resolved' con el botón
-- del propio mensaje, y ese cambio se refleja en TODAS las notificaciones
-- ya enviadas (ver tabla report_notifications).
CREATE TABLE IF NOT EXISTS reports (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id       INTEGER NOT NULL,
    group_title    TEXT,
    reporter_id    INTEGER NOT NULL,
    reporter_name  TEXT NOT NULL,
    reported_id    INTEGER,
    reported_name  TEXT,
    message_id     INTEGER,
    message_link   TEXT,
    reason         TEXT,
    source         TEXT NOT NULL DEFAULT 'reportar',
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     INTEGER NOT NULL,
    resolved_by    INTEGER,
    resolved_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_reports_group ON reports (group_id, status);

-- Un registro por cada administrador al que se le mandó el reporte por
-- privado, guardando el message_id de SU copia para poder editarla
-- cuando alguien marque el reporte como resuelto/pendiente.
CREATE TABLE IF NOT EXISTS report_notifications (
    report_id   INTEGER NOT NULL,
    admin_id    INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    PRIMARY KEY (report_id, admin_id)
);

-- Estadísticas de actividad (mensajes) por grupo, usadas por /top (ver
-- handlers/activity_ranking.py). today_messages/week_messages se
-- reinician con un job programado (ver utils/activity_stats.py), no de
-- forma perezosa por fila, para que el ranking sea correcto también
-- para quienes no escribieron nada en el período nuevo.
CREATE TABLE IF NOT EXISTS activity_stats (
    chat_id            INTEGER NOT NULL,
    user_id            INTEGER NOT NULL,
    username           TEXT,
    first_name         TEXT NOT NULL,
    last_name          TEXT,
    total_messages     INTEGER NOT NULL DEFAULT 0,
    today_messages     INTEGER NOT NULL DEFAULT 0,
    week_messages      INTEGER NOT NULL DEFAULT 0,
    last_message_date  INTEGER NOT NULL DEFAULT 0,
    updated_at         INTEGER NOT NULL,
    PRIMARY KEY (chat_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_activity_total ON activity_stats (chat_id, total_messages);
CREATE INDEX IF NOT EXISTS idx_activity_today ON activity_stats (chat_id, today_messages);
CREATE INDEX IF NOT EXISTS idx_activity_week  ON activity_stats (chat_id, week_messages);

-- Pequeña tabla clave/valor para recordar cuándo se hizo el último
-- reinicio diario/semanal de activity_stats (para "ponerse al día" si
-- el bot estuvo apagado justo a la hora programada, ver
-- utils/activity_stats.py -> schedule_activity_resets).
CREATE TABLE IF NOT EXISTS activity_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
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
        ("delete_pin", "INTEGER NOT NULL DEFAULT 0"),
        ("delete_commands", "INTEGER NOT NULL DEFAULT 0"),
        ("filter_punishment", "TEXT NOT NULL DEFAULT 'none'"),
        ("filter_mute_seconds", "INTEGER NOT NULL DEFAULT 0"),
        ("filter_delete", "INTEGER NOT NULL DEFAULT 1"),
        ("warn_limit", "INTEGER NOT NULL DEFAULT 3"),
        ("warn_action", "TEXT NOT NULL DEFAULT 'mute'"),
        ("warn_mute_seconds", "INTEGER NOT NULL DEFAULT 3600"),
        # A dónde se manda el mensaje de bienvenida: 'group' | 'private' | 'both'.
        ("welcome_send_to", "TEXT NOT NULL DEFAULT 'group'"),
        # Imagen/adjunto y botones opcionales para el mensaje de bienvenida.
        # welcome_content_type: 'text' | 'photo' | 'video' | 'animation' | 'document'.
        ("welcome_content_type", "TEXT NOT NULL DEFAULT 'text'"),
        ("welcome_file_id", "TEXT"),
        ("welcome_buttons", "TEXT NOT NULL DEFAULT '[]'"),
        # --- Captcha de edad ---
        ("captcha_enabled", "INTEGER NOT NULL DEFAULT 0"),
        # Qué se hace con quien no cumple la edad permitida: 'mute' (se
        # queda silenciado permanentemente) o 'ban' (se lo expulsa).
        ("captcha_action", "TEXT NOT NULL DEFAULT 'mute'"),
        ("captcha_min_age", "INTEGER NOT NULL DEFAULT 18"),
        # 0 = sin límite superior.
        ("captcha_max_age", "INTEGER NOT NULL DEFAULT 0"),
        # Canal de registros del captcha (opcional). Se completa reenviando
        # un mensaje del canal al bot desde el menú de configuración.
        ("captcha_log_chat_id", "INTEGER"),
        ("captcha_log_title", "TEXT"),
        # --- Moderación extrema de usuarios (revisión al aceptar solicitudes) ---
        ("xmod_enabled", "INTEGER NOT NULL DEFAULT 0"),
        ("xmod_punishment", "TEXT NOT NULL DEFAULT 'ban'"),   # ban | mute
        ("xmod_mute_seconds", "INTEGER NOT NULL DEFAULT 0"),  # 0 = mute permanente
        ("xmod_check_name", "INTEGER NOT NULL DEFAULT 1"),
        ("xmod_check_bio", "INTEGER NOT NULL DEFAULT 1"),
        ("xmod_check_photo", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "known_groups": [
        # Grupo "activado" por el propietario mediante /activar. Mientras
        # esté en 0, el bot no responde a ningún comando en ese grupo.
        ("activated", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "users": [
        # 1 = le podemos mandar mensajes privados a este usuario (ya sea
        # porque nos escribió /start, o porque una bienvenida/broadcast le
        # llegó con éxito). Telegram prohíbe que un bot le escriba primero
        # a alguien que nunca abrió un chat con él, así que esta bandera es
        # la única forma confiable de saber a quién sí se le puede escribir.
        ("dm_ok", "INTEGER NOT NULL DEFAULT 0"),
        # Fecha de nacimiento (solo día y mes) para calcular el signo del
        # /horoscopo. NULL hasta que el usuario la indique la primera vez.
        ("birth_day", "INTEGER"),
        ("birth_month", "INTEGER"),
    ],
    "broadcast_queue": [
        # A quién se manda el anuncio: 'groups' (todos los grupos),
        # 'users' (privado, solo a quien nos pueda escribir) o
        # 'specific' (solo a los grupos elegidos en target_group_ids).
        ("target", "TEXT NOT NULL DEFAULT 'groups'"),
        # JSON con la lista de group_id elegidos, solo se usa cuando
        # target = 'specific'.
        ("target_group_ids", "TEXT NOT NULL DEFAULT '[]'"),
    ],
    "join_requests": [
        # 1 = ya le mandamos la bienvenida privada apenas mandó la
        # solicitud (para no repetírsela cuando entra de verdad).
        ("welcomed", "INTEGER NOT NULL DEFAULT 0"),
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
    delete_pin: bool = False
    delete_commands: bool = False
    filter_punishment: str = "none"     # none | mute | ban
    filter_mute_seconds: int = 0        # 0 = mute permanente
    filter_delete: bool = True
    warn_limit: int = 3                 # cantidad de advertencias antes de castigar
    warn_action: str = "mute"           # mute | kick | ban
    warn_mute_seconds: int = 3600       # 0 = mute permanente (solo si warn_action == mute)
    welcome_send_to: str = "group"      # group | private | both
    welcome_content_type: str = "text"  # text | photo | video | animation | document
    welcome_file_id: Optional[str] = None
    welcome_buttons: str = "[]"
    captcha_enabled: bool = False
    captcha_action: str = "mute"        # mute | ban
    captcha_min_age: int = 18
    captcha_max_age: int = 0            # 0 = sin límite superior
    captcha_log_chat_id: Optional[int] = None
    captcha_log_title: Optional[str] = None
    xmod_enabled: bool = False
    xmod_punishment: str = "ban"        # ban | mute
    xmod_mute_seconds: int = 0          # 0 = mute permanente
    xmod_check_name: bool = True
    xmod_check_bio: bool = True
    xmod_check_photo: bool = False


@dataclass(slots=True)
class CaptchaState:
    group_id: int
    user_id: int
    status: str  # pending | passed | rejected
    user_name: Optional[str]
    username: Optional[str]
    group_title: Optional[str]
    age: Optional[int]
    created_at: int
    resolved_at: Optional[int]


def _row_to_captcha_state(row: aiosqlite.Row) -> CaptchaState:
    return CaptchaState(
        group_id=row["group_id"], user_id=row["user_id"], status=row["status"],
        user_name=row["user_name"], username=row["username"], group_title=row["group_title"],
        age=row["age"], created_at=row["created_at"], resolved_at=row["resolved_at"],
    )


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


@dataclass(slots=True)
class EconomyProfile:
    group_id: int
    user_id: int
    balance: int = 0
    bank: int = 0
    xp: int = 0
    job: Optional[str] = None
    shield_until: int = 0
    daily_streak: int = 0
    last_daily: int = 0

    @property
    def level(self) -> int:
        return 1 + self.xp // 100

    @property
    def xp_into_level(self) -> int:
        return self.xp % 100


def _row_to_economy(row: aiosqlite.Row) -> EconomyProfile:
    return EconomyProfile(
        group_id=row["group_id"], user_id=row["user_id"], balance=row["balance"],
        bank=row["bank"], xp=row["xp"], job=row["job"], shield_until=row["shield_until"],
        daily_streak=row["daily_streak"], last_daily=row["last_daily"],
    )


@dataclass(slots=True)
class ActivityEntry:
    user_id: int
    username: Optional[str]
    first_name: str
    last_name: Optional[str]
    total_messages: int
    today_messages: int
    week_messages: int
    last_message_date: int

    @property
    def display_name(self) -> str:
        if self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.first_name


def _row_to_activity_entry(row: aiosqlite.Row) -> ActivityEntry:
    return ActivityEntry(
        user_id=row["user_id"], username=row["username"], first_name=row["first_name"],
        last_name=row["last_name"], total_messages=row["total_messages"],
        today_messages=row["today_messages"], week_messages=row["week_messages"],
        last_message_date=row["last_message_date"],
    )


@dataclass(slots=True)
class BroadcastMessage:
    id: int
    content_type: str
    text: Optional[str]
    entities: str
    file_id: Optional[str]
    buttons: str
    status: str
    created_by: Optional[int]
    created_at: int
    sent_at: Optional[int]
    sent_count: int
    failed_count: int
    target: str = "groups"  # groups | users | specific
    target_group_ids: str = "[]"


def _row_to_broadcast(row: aiosqlite.Row) -> BroadcastMessage:
    keys = row.keys()
    return BroadcastMessage(
        id=row["id"], content_type=row["content_type"], text=row["text"],
        entities=row["entities"], file_id=row["file_id"], buttons=row["buttons"],
        status=row["status"], created_by=row["created_by"], created_at=row["created_at"],
        sent_at=row["sent_at"], sent_count=row["sent_count"], failed_count=row["failed_count"],
        target=(row["target"] if "target" in keys and row["target"] else "groups"),
        target_group_ids=(row["target_group_ids"] if "target_group_ids" in keys and row["target_group_ids"] else "[]"),
    )


@dataclass(slots=True)
class SecretMessage:
    id: int
    inline_message_id: Optional[str]
    author_id: int
    author_name: str
    author_username: Optional[str]
    targets: list[str]
    text: Optional[str]
    self_destruct: bool
    created_at: int
    read_at: Optional[int]
    read_by_id: Optional[int]
    read_by_name: Optional[str]

    @property
    def is_read(self) -> bool:
        return self.read_at is not None

    @property
    def is_destroyed(self) -> bool:
        return self.self_destruct and self.is_read


def _row_to_secret_message(row: aiosqlite.Row) -> SecretMessage:
    raw_targets = row["targets"] or ""
    targets = [t for t in raw_targets.split(",") if t]
    return SecretMessage(
        id=row["id"], inline_message_id=row["inline_message_id"],
        author_id=row["author_id"], author_name=row["author_name"],
        author_username=row["author_username"], targets=targets, text=row["text"],
        self_destruct=bool(row["self_destruct"]), created_at=row["created_at"],
        read_at=row["read_at"], read_by_id=row["read_by_id"], read_by_name=row["read_by_name"],
    )


@dataclass(slots=True)
class Report:
    id: int
    group_id: int
    group_title: Optional[str]
    reporter_id: int
    reporter_name: str
    reported_id: Optional[int]
    reported_name: Optional[str]
    message_id: Optional[int]
    message_link: Optional[str]
    reason: Optional[str]
    source: str
    status: str
    created_at: int
    resolved_by: Optional[int]
    resolved_at: Optional[int]


def _row_to_report(row: aiosqlite.Row) -> Report:
    return Report(
        id=row["id"], group_id=row["group_id"], group_title=row["group_title"],
        reporter_id=row["reporter_id"], reporter_name=row["reporter_name"],
        reported_id=row["reported_id"], reported_name=row["reported_name"],
        message_id=row["message_id"], message_link=row["message_link"],
        reason=row["reason"], source=row["source"], status=row["status"],
        created_at=row["created_at"], resolved_by=row["resolved_by"], resolved_at=row["resolved_at"],
    )


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

    async def set_user_birthdate(self, user_id: int, day: int, month: int) -> None:
        await self.conn.execute(
            """
            INSERT INTO users (user_id, birth_day, birth_month, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                birth_day = excluded.birth_day,
                birth_month = excluded.birth_month,
                updated_at = excluded.updated_at
            """,
            (user_id, day, month, int(time.time())),
        )
        await self.conn.commit()

    async def get_user_birthdate(self, user_id: int) -> Optional[tuple[int, int]]:
        cursor = await self.conn.execute(
            "SELECT birth_day, birth_month FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row is None or row["birth_day"] is None or row["birth_month"] is None:
            return None
        return int(row["birth_day"]), int(row["birth_month"])

    async def set_dm_ok(self, user_id: int, ok: bool) -> None:
        """Marca si le podemos escribir por privado a este usuario o no.
        Se pone en True cuando nos manda /start o cuando le llega bien un
        mensaje privado (bienvenida, broadcast); se pone en False en cuanto
        un envío falla porque nos bloqueó o nunca abrió chat con nosotros."""
        await self.conn.execute(
            """
            INSERT INTO users (user_id, dm_ok, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET dm_ok = excluded.dm_ok, updated_at = excluded.updated_at
            """,
            (user_id, 1 if ok else 0, int(time.time())),
        )
        await self.conn.commit()

    async def get_dm_ok_users(self) -> list[tuple[int, str, Optional[str]]]:
        """Usuarios a los que sí se les puede escribir por privado: (user_id, first_name, username)."""
        cursor = await self.conn.execute(
            "SELECT user_id, first_name, username FROM users WHERE dm_ok = 1"
        )
        rows = await cursor.fetchall()
        return [(row["user_id"], row["first_name"] or "", row["username"]) for row in rows]

    async def count_dm_ok_users(self) -> int:
        cursor = await self.conn.execute("SELECT COUNT(*) AS c FROM users WHERE dm_ok = 1")
        row = await cursor.fetchone()
        return int(row["c"]) if row else 0

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
        "delete_join", "delete_leave", "delete_call", "delete_pin", "delete_commands",
        "filter_punishment", "filter_mute_seconds", "filter_delete",
        "warn_limit", "warn_action", "warn_mute_seconds", "welcome_send_to",
        "welcome_content_type", "welcome_file_id", "welcome_buttons",
        "captcha_enabled", "captcha_action", "captcha_min_age", "captcha_max_age",
        "captcha_log_chat_id", "captcha_log_title",
        "xmod_enabled", "xmod_punishment", "xmod_mute_seconds",
        "xmod_check_name", "xmod_check_bio", "xmod_check_photo",
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
            delete_pin=bool(row["delete_pin"]) if "delete_pin" in keys and row["delete_pin"] is not None else False,
            delete_commands=bool(row["delete_commands"]) if "delete_commands" in keys and row["delete_commands"] is not None else False,
            filter_punishment=(row["filter_punishment"] if "filter_punishment" in keys and row["filter_punishment"] else "none"),
            filter_mute_seconds=(row["filter_mute_seconds"] if "filter_mute_seconds" in keys and row["filter_mute_seconds"] is not None else 0),
            filter_delete=bool(row["filter_delete"]) if "filter_delete" in keys and row["filter_delete"] is not None else True,
            warn_limit=(row["warn_limit"] if "warn_limit" in keys and row["warn_limit"] is not None else 3),
            warn_action=(row["warn_action"] if "warn_action" in keys and row["warn_action"] else "mute"),
            warn_mute_seconds=(row["warn_mute_seconds"] if "warn_mute_seconds" in keys and row["warn_mute_seconds"] is not None else 3600),
            welcome_send_to=(row["welcome_send_to"] if "welcome_send_to" in keys and row["welcome_send_to"] else "group"),
            welcome_content_type=(
                row["welcome_content_type"] if "welcome_content_type" in keys and row["welcome_content_type"] else "text"
            ),
            welcome_file_id=(row["welcome_file_id"] if "welcome_file_id" in keys else None),
            welcome_buttons=(row["welcome_buttons"] if "welcome_buttons" in keys and row["welcome_buttons"] else "[]"),
            captcha_enabled=bool(row["captcha_enabled"]) if "captcha_enabled" in keys and row["captcha_enabled"] is not None else False,
            captcha_action=(row["captcha_action"] if "captcha_action" in keys and row["captcha_action"] else "mute"),
            captcha_min_age=(row["captcha_min_age"] if "captcha_min_age" in keys and row["captcha_min_age"] is not None else 18),
            captcha_max_age=(row["captcha_max_age"] if "captcha_max_age" in keys and row["captcha_max_age"] is not None else 0),
            captcha_log_chat_id=(row["captcha_log_chat_id"] if "captcha_log_chat_id" in keys else None),
            captcha_log_title=(row["captcha_log_title"] if "captcha_log_title" in keys else None),
            xmod_enabled=bool(row["xmod_enabled"]) if "xmod_enabled" in keys and row["xmod_enabled"] is not None else False,
            xmod_punishment=(row["xmod_punishment"] if "xmod_punishment" in keys and row["xmod_punishment"] else "ban"),
            xmod_mute_seconds=(row["xmod_mute_seconds"] if "xmod_mute_seconds" in keys and row["xmod_mute_seconds"] is not None else 0),
            xmod_check_name=bool(row["xmod_check_name"]) if "xmod_check_name" in keys and row["xmod_check_name"] is not None else True,
            xmod_check_bio=bool(row["xmod_check_bio"]) if "xmod_check_bio" in keys and row["xmod_check_bio"] is not None else True,
            xmod_check_photo=bool(row["xmod_check_photo"]) if "xmod_check_photo" in keys and row["xmod_check_photo"] is not None else False,
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

    async def is_group_activated(self, group_id: int) -> bool:
        cursor = await self.conn.execute(
            "SELECT activated FROM known_groups WHERE group_id = ?", (group_id,)
        )
        row = await cursor.fetchone()
        return bool(row["activated"]) if row else False

    async def set_group_activated(self, group_id: int, title: Optional[str], activated: bool) -> None:
        """Activa/desactiva el uso del bot en un grupo. Usa upsert por si el
        grupo aún no tenía fila en known_groups (por ejemplo, el owner activa
        el grupo antes de que cualquier otro handler lo haya registrado)."""
        await self.conn.execute(
            """
            INSERT INTO known_groups (group_id, title, activated, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                activated = excluded.activated,
                title = COALESCE(excluded.title, known_groups.title),
                updated_at = excluded.updated_at
            """,
            (group_id, title, int(activated), int(time.time())),
        )
        await self.conn.commit()

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
    # Palabras/emojis prohibidos — Moderación extrema de usuarios
    # ------------------------------------------------------------------ #
    async def add_xmod_word(self, group_id: int, word: str, added_by: int) -> bool:
        word = word.strip().lower()
        if not word:
            return False
        await self.conn.execute(
            """
            INSERT INTO xmod_words (group_id, word, added_by, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, word) DO NOTHING
            """,
            (group_id, word, added_by, int(time.time())),
        )
        await self.conn.commit()
        return True

    async def remove_xmod_word(self, group_id: int, word: str) -> bool:
        word = word.strip().lower()
        cursor = await self.conn.execute(
            "DELETE FROM xmod_words WHERE group_id = ? AND word = ?", (group_id, word)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def get_xmod_words(self, group_id: int) -> list[str]:
        cursor = await self.conn.execute(
            "SELECT word FROM xmod_words WHERE group_id = ? ORDER BY word", (group_id,)
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
    # /free — usuarios exentos del filtro de palabras (y de futuros
    # filtros automáticos) en un grupo puntual.
    # ------------------------------------------------------------------ #
    async def add_freed_user(self, group_id: int, user_id: int, freed_by: int) -> None:
        await self.conn.execute(
            """
            INSERT INTO freed_users (group_id, user_id, freed_by, freed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO UPDATE SET
                freed_by = excluded.freed_by,
                freed_at = excluded.freed_at
            """,
            (group_id, user_id, freed_by, int(time.time())),
        )
        await self.conn.commit()

    async def remove_freed_user(self, group_id: int, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "DELETE FROM freed_users WHERE group_id = ? AND user_id = ?", (group_id, user_id)
        )
        await self.conn.commit()
        return cursor.rowcount > 0

    async def is_user_freed(self, group_id: int, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "SELECT 1 FROM freed_users WHERE group_id = ? AND user_id = ?", (group_id, user_id)
        )
        return await cursor.fetchone() is not None

    async def get_freed_users(self, group_id: int) -> list[int]:
        cursor = await self.conn.execute(
            "SELECT user_id FROM freed_users WHERE group_id = ? ORDER BY freed_at", (group_id,)
        )
        rows = await cursor.fetchall()
        return [int(row["user_id"]) for row in rows]

    # ------------------------------------------------------------------ #
    # Economía: monedas, banco, XP/nivel, empleo, escudo, cooldowns
    # ------------------------------------------------------------------ #
    async def get_economy(self, group_id: int, user_id: int) -> EconomyProfile:
        cursor = await self.conn.execute(
            "SELECT * FROM economy WHERE group_id = ? AND user_id = ?", (group_id, user_id)
        )
        row = await cursor.fetchone()
        if row:
            return _row_to_economy(row)
        await self.conn.execute(
            "INSERT INTO economy (group_id, user_id, updated_at) VALUES (?, ?, ?)",
            (group_id, user_id, int(time.time())),
        )
        await self.conn.commit()
        return EconomyProfile(group_id=group_id, user_id=user_id)

    async def add_balance(self, group_id: int, user_id: int, amount: int) -> int:
        """Suma (o resta, si amount es negativo) monedas en efectivo. Nunca baja de 0."""
        await self.get_economy(group_id, user_id)  # asegura que exista la fila
        await self.conn.execute(
            """
            UPDATE economy SET balance = MAX(0, balance + ?), updated_at = ?
            WHERE group_id = ? AND user_id = ?
            """,
            (amount, int(time.time()), group_id, user_id),
        )
        await self.conn.commit()
        profile = await self.get_economy(group_id, user_id)
        return profile.balance

    async def add_xp(self, group_id: int, user_id: int, amount: int) -> EconomyProfile:
        await self.get_economy(group_id, user_id)
        await self.conn.execute(
            "UPDATE economy SET xp = xp + ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
            (amount, int(time.time()), group_id, user_id),
        )
        await self.conn.commit()
        return await self.get_economy(group_id, user_id)

    async def set_job(self, group_id: int, user_id: int, job: Optional[str]) -> None:
        await self.get_economy(group_id, user_id)
        await self.conn.execute(
            "UPDATE economy SET job = ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
            (job, int(time.time()), group_id, user_id),
        )
        await self.conn.commit()

    async def bank_deposit(self, group_id: int, user_id: int, amount: int) -> Optional[EconomyProfile]:
        profile = await self.get_economy(group_id, user_id)
        if amount <= 0 or amount > profile.balance:
            return None
        await self.conn.execute(
            """
            UPDATE economy SET balance = balance - ?, bank = bank + ?, updated_at = ?
            WHERE group_id = ? AND user_id = ?
            """,
            (amount, amount, int(time.time()), group_id, user_id),
        )
        await self.conn.commit()
        return await self.get_economy(group_id, user_id)

    async def bank_withdraw(self, group_id: int, user_id: int, amount: int) -> Optional[EconomyProfile]:
        profile = await self.get_economy(group_id, user_id)
        if amount <= 0 or amount > profile.bank:
            return None
        await self.conn.execute(
            """
            UPDATE economy SET balance = balance + ?, bank = bank - ?, updated_at = ?
            WHERE group_id = ? AND user_id = ?
            """,
            (amount, amount, int(time.time()), group_id, user_id),
        )
        await self.conn.commit()
        return await self.get_economy(group_id, user_id)

    async def set_shield(self, group_id: int, user_id: int, until_ts: int) -> None:
        await self.get_economy(group_id, user_id)
        await self.conn.execute(
            "UPDATE economy SET shield_until = ?, updated_at = ? WHERE group_id = ? AND user_id = ?",
            (until_ts, int(time.time()), group_id, user_id),
        )
        await self.conn.commit()

    async def set_daily(self, group_id: int, user_id: int, streak: int, ts: int) -> None:
        await self.get_economy(group_id, user_id)
        await self.conn.execute(
            """
            UPDATE economy SET daily_streak = ?, last_daily = ?, updated_at = ?
            WHERE group_id = ? AND user_id = ?
            """,
            (streak, ts, int(time.time()), group_id, user_id),
        )
        await self.conn.commit()

    async def get_leaderboard(self, group_id: int, limit: int = 10) -> list[EconomyProfile]:
        cursor = await self.conn.execute(
            """
            SELECT * FROM economy WHERE group_id = ?
            ORDER BY (balance + bank) DESC LIMIT ?
            """,
            (group_id, limit),
        )
        rows = await cursor.fetchall()
        return [_row_to_economy(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Actividad de mensajes por grupo (/top, ver handlers/activity_ranking.py)
    # ------------------------------------------------------------------ #
    _ACTIVITY_PERIOD_COLUMNS = {"today": "today_messages", "week": "week_messages", "all": "total_messages"}

    async def record_message_activity(
        self, chat_id: int, user_id: int, username: Optional[str], first_name: str, last_name: Optional[str],
    ) -> None:
        """Registra un mensaje nuevo: crea la fila si no existía o suma 1 a
        total/hoy/semana, y refresca el nombre/usuario por si cambiaron."""
        now = int(time.time())
        await self.conn.execute(
            """
            INSERT INTO activity_stats (
                chat_id, user_id, username, first_name, last_name,
                total_messages, today_messages, week_messages, last_message_date, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, 1, 1, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                total_messages = total_messages + 1,
                today_messages = today_messages + 1,
                week_messages = week_messages + 1,
                last_message_date = excluded.last_message_date,
                updated_at = excluded.updated_at
            """,
            (chat_id, user_id, username.lower() if username else None, first_name, last_name, now, now),
        )
        await self.conn.commit()

    async def get_activity_ranking(self, chat_id: int, period: str, limit: int = 10) -> list[ActivityEntry]:
        """Top `limit` usuarios del grupo para el período pedido
        ('today' | 'week' | 'all'), de mayor a menor."""
        column = self._ACTIVITY_PERIOD_COLUMNS.get(period, "total_messages")
        cursor = await self.conn.execute(
            f"""
            SELECT user_id, username, first_name, last_name,
                   total_messages, today_messages, week_messages, last_message_date
            FROM activity_stats
            WHERE chat_id = ? AND {column} > 0
            ORDER BY {column} DESC, user_id ASC
            LIMIT ?
            """,
            (chat_id, limit),
        )
        rows = await cursor.fetchall()
        return [_row_to_activity_entry(r) for r in rows]

    async def get_activity_group_totals(self, chat_id: int) -> tuple[int, int]:
        """(usuarios con actividad registrada, total de mensajes registrados) del grupo."""
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS users, COALESCE(SUM(total_messages), 0) AS total "
            "FROM activity_stats WHERE chat_id = ?",
            (chat_id,),
        )
        row = await cursor.fetchone()
        return int(row["users"]), int(row["total"])

    async def reset_daily_activity(self) -> int:
        cursor = await self.conn.execute("UPDATE activity_stats SET today_messages = 0 WHERE today_messages != 0")
        await self.conn.commit()
        return cursor.rowcount

    async def reset_weekly_activity(self) -> int:
        cursor = await self.conn.execute("UPDATE activity_stats SET week_messages = 0 WHERE week_messages != 0")
        await self.conn.commit()
        return cursor.rowcount

    async def get_meta(self, key: str) -> Optional[str]:
        cursor = await self.conn.execute("SELECT value FROM activity_meta WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        await self.conn.execute(
            "INSERT INTO activity_meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.conn.commit()

    # -- Cooldowns de economía (juegos, trabajo, robo, etc.) --
    async def get_cooldown(self, group_id: int, user_id: int, action: str) -> int:
        cursor = await self.conn.execute(
            "SELECT last_ts FROM economy_cooldowns WHERE group_id = ? AND user_id = ? AND action = ?",
            (group_id, user_id, action),
        )
        row = await cursor.fetchone()
        return int(row["last_ts"]) if row else 0

    async def set_cooldown(self, group_id: int, user_id: int, action: str, ts: Optional[int] = None) -> None:
        ts = ts if ts is not None else int(time.time())
        await self.conn.execute(
            """
            INSERT INTO economy_cooldowns (group_id, user_id, action, last_ts)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(group_id, user_id, action) DO UPDATE SET last_ts = excluded.last_ts
            """,
            (group_id, user_id, action, ts),
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

    # ------------------------------------------------------------------ #
    # Cola de anuncios (usada por el bot anunciador -> bot de moderación)
    # ------------------------------------------------------------------ #
    async def create_broadcast(
        self, content_type: str, text: Optional[str], entities: str,
        file_id: Optional[str], buttons: str, created_by: int, target: str = "groups",
        target_group_ids: str = "[]",
    ) -> int:
        cursor = await self.conn.execute(
            """
            INSERT INTO broadcast_queue
                (content_type, text, entities, file_id, buttons, status, created_by, created_at, target, target_group_ids)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (content_type, text, entities, file_id, buttons, created_by, int(time.time()), target, target_group_ids),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_pending_broadcasts(self) -> list["BroadcastMessage"]:
        cursor = await self.conn.execute(
            "SELECT * FROM broadcast_queue WHERE status = 'pending' ORDER BY id"
        )
        rows = await cursor.fetchall()
        return [_row_to_broadcast(row) for row in rows]

    async def mark_broadcast_sent(self, broadcast_id: int, sent_count: int, failed_count: int) -> None:
        await self.conn.execute(
            """
            UPDATE broadcast_queue
            SET status = 'sent', sent_at = ?, sent_count = ?, failed_count = ?
            WHERE id = ?
            """,
            (int(time.time()), sent_count, failed_count, broadcast_id),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # Mensajes secretos (modo en línea, estilo @mensajesecretobot)
    # ------------------------------------------------------------------ #
    async def create_secret_message(
        self, author_id: int, author_name: str, author_username: Optional[str],
        targets: list[str], text: str, self_destruct: bool,
    ) -> int:
        clean_targets = ",".join(sorted({t.lstrip("@").lower() for t in targets if t}))
        cursor = await self.conn.execute(
            """
            INSERT INTO secret_messages
                (inline_message_id, author_id, author_name, author_username,
                 targets, text, self_destruct, created_at)
            VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                author_id, author_name, author_username.lower() if author_username else None,
                clean_targets, text, int(self_destruct), int(time.time()),
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_secret_message(self, secret_id: int) -> Optional[SecretMessage]:
        cursor = await self.conn.execute(
            "SELECT * FROM secret_messages WHERE id = ?", (secret_id,)
        )
        row = await cursor.fetchone()
        return _row_to_secret_message(row) if row else None

    async def save_inline_message_id(self, secret_id: int, inline_message_id: str) -> None:
        """El modo en línea no nos dice dónde quedó el mensaje al enviarse;
        recién lo sabemos cuando llega el primer callback (botón tocado),
        así que lo guardamos ahí para poder editarlo más adelante (ej. desde
        el chat privado, al usar '✏️ Editar mensaje')."""
        await self.conn.execute(
            "UPDATE secret_messages SET inline_message_id = COALESCE(inline_message_id, ?) WHERE id = ?",
            (inline_message_id, secret_id),
        )
        await self.conn.commit()

    async def mark_secret_read(
        self, secret_id: int, reader_id: int, reader_name: str, wipe_text: bool,
    ) -> None:
        if wipe_text:
            await self.conn.execute(
                """
                UPDATE secret_messages
                SET read_at = ?, read_by_id = ?, read_by_name = ?, text = NULL
                WHERE id = ?
                """,
                (int(time.time()), reader_id, reader_name, secret_id),
            )
        else:
            await self.conn.execute(
                """
                UPDATE secret_messages
                SET read_at = ?, read_by_id = ?, read_by_name = ?
                WHERE id = ?
                """,
                (int(time.time()), reader_id, reader_name, secret_id),
            )
        await self.conn.commit()

    async def update_secret_text(self, secret_id: int, text: str) -> None:
        await self.conn.execute(
            "UPDATE secret_messages SET text = ? WHERE id = ?", (text, secret_id),
        )
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # Donaciones en Telegram Stars (/donar)
    # ------------------------------------------------------------------ #
    async def log_donation(
        self, user_id: int, name: str, username: Optional[str],
        chat_id: Optional[int], amount: int, charge_id: Optional[str],
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO donations (user_id, name, username, chat_id, amount, telegram_payment_charge_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, name, username, chat_id, amount, charge_id, int(time.time())),
        )
        await self.conn.commit()

    async def get_total_donated_by(self, user_id: int) -> int:
        cursor = await self.conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM donations WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return int(row["total"]) if row else 0

    # ------------------------------------------------------------------ #
    # Solicitudes de ingreso pendientes (/aceptar)
    # ------------------------------------------------------------------ #
    async def record_join_request(
        self, chat_id: int, user_id: int, name: str, username: Optional[str]
    ) -> None:
        await self.conn.execute(
            """
            INSERT INTO join_requests (chat_id, user_id, name, username, requested_at, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                name = excluded.name,
                username = excluded.username,
                requested_at = excluded.requested_at,
                status = 'pending'
            """,
            (chat_id, user_id, name, username, int(time.time())),
        )
        await self.conn.commit()

    async def mark_join_request_welcomed(self, chat_id: int, user_id: int) -> None:
        await self.conn.execute(
            "UPDATE join_requests SET welcomed = 1 WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        await self.conn.commit()

    async def was_join_request_welcomed(self, chat_id: int, user_id: int) -> bool:
        cursor = await self.conn.execute(
            "SELECT welcomed FROM join_requests WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        )
        row = await cursor.fetchone()
        return bool(row["welcomed"]) if row else False

    async def count_pending_join_requests(self, chat_id: int) -> int:
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS c FROM join_requests WHERE chat_id = ? AND status = 'pending'", (chat_id,)
        )
        row = await cursor.fetchone()
        return int(row["c"]) if row else 0

    async def get_pending_join_requests(
        self, chat_id: int, limit: Optional[int] = None
    ) -> list[tuple[int, str, Optional[str]]]:
        query = (
            "SELECT user_id, name, username FROM join_requests "
            "WHERE chat_id = ? AND status = 'pending' ORDER BY requested_at ASC"
        )
        params: list = [chat_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        cursor = await self.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [(row["user_id"], row["name"], row["username"]) for row in rows]

    async def set_join_requests_status_bulk(self, chat_id: int, user_ids: list[int], status: str) -> None:
        if not user_ids:
            return
        await self.conn.executemany(
            "UPDATE join_requests SET status = ? WHERE chat_id = ? AND user_id = ?",
            [(status, chat_id, uid) for uid in user_ids],
        )
        await self.conn.commit()

    # ------------------------------------------------------------------ #
    # Reportes (/reportar y "@admin")
    # ------------------------------------------------------------------ #
    async def add_report(
        self,
        group_id: int,
        group_title: Optional[str],
        reporter_id: int,
        reporter_name: str,
        reported_id: Optional[int],
        reported_name: Optional[str],
        message_id: Optional[int],
        message_link: Optional[str],
        reason: Optional[str],
        source: str,
    ) -> int:
        cursor = await self.conn.execute(
            """
            INSERT INTO reports
                (group_id, group_title, reporter_id, reporter_name, reported_id, reported_name,
                 message_id, message_link, reason, source, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                group_id, group_title, reporter_id, reporter_name, reported_id, reported_name,
                message_id, message_link, reason, source, int(time.time()),
            ),
        )
        await self.conn.commit()
        return cursor.lastrowid

    async def get_report(self, report_id: int) -> Optional[Report]:
        cursor = await self.conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,))
        row = await cursor.fetchone()
        return _row_to_report(row) if row else None

    async def set_report_status(self, report_id: int, status: str, resolved_by: Optional[int]) -> None:
        await self.conn.execute(
            "UPDATE reports SET status = ?, resolved_by = ?, resolved_at = ? WHERE id = ?",
            (status, resolved_by, int(time.time()) if status == "resolved" else None, report_id),
        )
        await self.conn.commit()

    async def add_report_notification(self, report_id: int, admin_id: int, message_id: int) -> None:
        await self.conn.execute(
            """
            INSERT INTO report_notifications (report_id, admin_id, message_id)
            VALUES (?, ?, ?)
            ON CONFLICT(report_id, admin_id) DO UPDATE SET message_id = excluded.message_id
            """,
            (report_id, admin_id, message_id),
        )
        await self.conn.commit()

    async def get_report_notifications(self, report_id: int) -> list[tuple[int, int]]:
        cursor = await self.conn.execute(
            "SELECT admin_id, message_id FROM report_notifications WHERE report_id = ?", (report_id,)
        )
        rows = await cursor.fetchall()
        return [(row["admin_id"], row["message_id"]) for row in rows]

    # ------------------------------------------------------------------ #
    # Captcha de edad
    # ------------------------------------------------------------------ #
    async def get_captcha_state(self, group_id: int, user_id: int) -> Optional[CaptchaState]:
        cursor = await self.conn.execute(
            "SELECT * FROM captcha_state WHERE group_id = ? AND user_id = ?", (group_id, user_id)
        )
        row = await cursor.fetchone()
        return _row_to_captcha_state(row) if row else None

    async def start_captcha(
        self, group_id: int, user_id: int, user_name: Optional[str],
        username: Optional[str], group_title: Optional[str],
    ) -> None:
        """Crea la fila 'pending' para este (grupo, usuario). Si ya existe
        una fila (pending/passed/rejected) no hace nada, para no pisar un
        estado ya resuelto."""
        await self.conn.execute(
            """
            INSERT INTO captcha_state (group_id, user_id, status, user_name, username, group_title, created_at)
            VALUES (?, ?, 'pending', ?, ?, ?, ?)
            ON CONFLICT(group_id, user_id) DO NOTHING
            """,
            (group_id, user_id, user_name, username, group_title, int(time.time())),
        )
        await self.conn.commit()

    async def get_pending_captcha_for_user(self, user_id: int) -> Optional[CaptchaState]:
        """Busca la verificación de edad pendiente más reciente de este
        usuario (en cualquier grupo), para poder resolverla apenas
        responda por privado."""
        cursor = await self.conn.execute(
            "SELECT * FROM captcha_state WHERE user_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        return _row_to_captcha_state(row) if row else None

    async def resolve_captcha(self, group_id: int, user_id: int, status: str, age: Optional[int]) -> None:
        await self.conn.execute(
            "UPDATE captcha_state SET status = ?, age = ?, resolved_at = ? WHERE group_id = ? AND user_id = ?",
            (status, age, int(time.time()), group_id, user_id),
        )
        await self.conn.commit()

    async def clear_captcha_state(self, group_id: int, user_id: int) -> None:
        """Permite volver a captarlo (ej. si un admin quiere reintentar)."""
        await self.conn.execute(
            "DELETE FROM captcha_state WHERE group_id = ? AND user_id = ?", (group_id, user_id)
        )
        await self.conn.commit()

