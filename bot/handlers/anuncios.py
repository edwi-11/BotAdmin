"""
handlers/anuncios.py
Sistema de anuncios del BOT DE MODERACIÓN (no toca el bot de broadcast).

- Canal -> BD: cada channel_post (y sus ediciones) se guarda como anuncio.
- Envío por forward_message a los grupos registrados.
- /anuncios (privado, dueño y admins): menú 100% por botones.
- /enviar (dueño y admins): privado (anuncio + destino) o dentro de un grupo.
- Planificador: un tick (JobQueue = APScheduler) cuyo estado vive en SQLite,
  así sobrevive a los reinicios.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Kb, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, ChatMigrated, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from anuncios_db import AnunciosDB
from config import settings
from utils.callbacks import safe_callback

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Configuración (.env)
# ----------------------------------------------------------------------------
def _ids(raw: str) -> frozenset[int]:
    out: set[int] = set()
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.add(int(chunk))
    return frozenset(out)


ADMIN_IDS = _ids(os.getenv("ANUNCIOS_ADMIN_IDS", ""))
CHANNEL_ID = int(os.getenv("ANUNCIOS_CHANNEL_ID", "0").strip() or 0)
try:
    TZ = ZoneInfo(os.getenv("ANUNCIOS_TZ", "America/Managua").strip() or "America/Managua")
except Exception:  # zona inválida -> UTC
    TZ = ZoneInfo("UTC")
DB_PATH = os.path.join(os.path.dirname(settings.database_path), "anuncios.db")

TICK_SECONDS = 20
PAUSE_BETWEEN_GROUPS = 0.05
PAGE_ANN = 6
PAGE_GRP = 8
CB_BOT_DATA = "anuncios_db"

_inflight: set[tuple[int, int]] = set()   # (anuncio, grupo) con envío por mensajes en curso
_pin_warned: set[tuple[int, int]] = set()  # para no avisar mil veces el mismo fallo de fijado
_seen_groups: set[int] = set()  # grupos ya registrados en esta ejecución (evita un INSERT por mensaje)
_send_lock = asyncio.Lock()


def autorizado(user_id: Optional[int]) -> bool:
    return bool(user_id) and (user_id in settings.owner_ids or user_id in ADMIN_IDS)


def _adb(app: Application) -> AnunciosDB:
    return app.bot_data[CB_BOT_DATA]


# ----------------------------------------------------------------------------
# Utilidades de formato
# ----------------------------------------------------------------------------
def h(s: Any) -> str:
    return html.escape(str(s))


def fts(ts: Optional[int]) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d/%m %H:%M") if ts else "—"


def fmin(m: int) -> str:
    if m <= 0:
        return "—"
    return f"{m} min" if m < 60 else (f"{m // 60} h" if m % 60 == 0 else f"{m // 60} h {m % 60} min")


def chk(b: Any) -> str:
    return "✅" if b else "⬜"


def short(s: str, n: int) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def nav(back: Optional[str]) -> list[Btn]:
    row = []
    if back:
        row.append(Btn("⬅️ Atrás", callback_data=back))
    row.append(Btn("🏠 Menú", callback_data="an:m"))
    row.append(Btn("❌ Cerrar", callback_data="an:x"))
    return row


def pager(prefix: str, page: int, total: int, per: int) -> list[Btn]:
    pages = max(1, (total + per - 1) // per)
    row = []
    if page > 0:
        row.append(Btn("◀️", callback_data=f"{prefix}:{page - 1}"))
    row.append(Btn(f"{page + 1}/{pages}", callback_data="an:noop"))
    if page < pages - 1:
        row.append(Btn("▶️", callback_data=f"{prefix}:{page + 1}"))
    return row


async def is_quiet(adb: AnunciosDB) -> bool:
    if await adb.get_setting("quiet_on", "0") != "1":
        return False
    s = int(await adb.get_setting("quiet_start", "23"))
    e = int(await adb.get_setting("quiet_end", "7"))
    hr = datetime.now(TZ).hour
    if s == e:
        return False
    return (hr >= s or hr < e) if s > e else (s <= hr < e)


async def is_paused(adb: AnunciosDB) -> bool:
    return await adb.get_setting("paused", "0") == "1"


def mode_text(a: dict) -> str:
    if a["mode"] == "time":
        return f"⏱ por tiempo, cada {fmin(a['interval_min'])}"
    if a["mode"] == "msgs":
        cd = f", cooldown {fmin(a['cooldown_min'])}" if a["cooldown_min"] else ""
        return f"💬 cada {a['every_msgs']} mensajes{cd}"
    return "🖐 manual"


# ----------------------------------------------------------------------------
# Captura del canal y registro de grupos
# ----------------------------------------------------------------------------
def _tipo(m) -> str:
    for attr, label in (
        ("photo", "foto"), ("video", "video"), ("animation", "gif"), ("document", "archivo"),
        ("audio", "audio"), ("voice", "voz"), ("video_note", "videonota"), ("sticker", "sticker"),
        ("poll", "encuesta"), ("location", "ubicación"), ("contact", "contacto"),
    ):
        if getattr(m, attr, None):
            return label
    return "texto"


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    m = update.channel_post or update.edited_channel_post
    if m is None:
        return
    try:
        tipo = _tipo(m)
        prev = short((m.text or m.caption or ""), 60) or f"[{tipo}]"
        await _adb(context.application).upsert_anuncio(
            m.chat_id, m.message_id, tipo, int(m.date.timestamp()) if m.date else int(time.time()), prev
        )
    except Exception:
        logger.exception("No se pudo guardar el anuncio del canal")


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ev = update.my_chat_member
    if ev is None or ev.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    try:
        new = ev.new_chat_member
        adb = _adb(context.application)
        ok = new.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR)
        if new.status == ChatMemberStatus.RESTRICTED and getattr(new, "can_send_messages", True):
            ok = True
        await adb.upsert_group(ev.chat.id, ev.chat.title or str(ev.chat.id), 1 if ok else 0)
    except Exception:
        logger.exception("Error registrando grupo (my_chat_member)")


# ----------------------------------------------------------------------------
# Motor de envío
# ----------------------------------------------------------------------------
def _ra(e: RetryAfter) -> float:
    ra = e.retry_after
    return ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)


async def _retry(fn, *args, **kw):
    for _ in range(3):
        try:
            return await fn(*args, **kw)
        except RetryAfter as e:
            await asyncio.sleep(_ra(e) + 1)
    return await fn(*args, **kw)


async def _notify_owners(bot, text: str, only: Optional[int] = None) -> None:
    for uid in ([only] if only else list(settings.owner_ids)):
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        except TelegramError:
            pass


_DEAD = ("not enough rights", "have no rights", "chat_write_forbidden", "chat not found",
         "bot was kicked", "bot is not a member", "chat_admin_required", "group chat was deactivated")


async def enviar_anuncio(bot, adb: AnunciosDB, aid: int, chat_ids: list[int]) -> dict[str, Any]:
    """Reenvía el anuncio a cada grupo. Devuelve resumen."""
    res: dict[str, Any] = {"ok": 0, "fail": [], "pin_fail": [], "pinned": 0, "gone": False}
    async with _send_lock:
        a = await adb.get_anuncio(aid)
        if not a or a["deleted"]:
            res["gone"] = True
            return res
        silent = bool(a["silent"])
        for chat_id in chat_ids:
            g = await adb.get_group(chat_id)
            title = (g or {}).get("title") or str(chat_id)
            try:
                prev = await adb.last_envio(aid, chat_id) if a["del_prev"] else None
                try:
                    msg = await _retry(
                        bot.forward_message, chat_id=chat_id, from_chat_id=a["chat_id"],
                        message_id=a["message_id"], disable_notification=silent,
                    )
                except ChatMigrated as e:
                    await adb.migrate_group(chat_id, e.new_chat_id)
                    chat_id = e.new_chat_id
                    msg = await _retry(
                        bot.forward_message, chat_id=chat_id, from_chat_id=a["chat_id"],
                        message_id=a["message_id"], disable_notification=silent,
                    )
            except Forbidden as e:
                await adb.set_group_active(chat_id, 0)
                await adb.add_envio(aid, chat_id, None, False, error=str(e))
                res["fail"].append((title, "sin permiso/expulsado"))
                continue
            except BadRequest as e:
                s = str(e).lower()
                if "message to forward not found" in s or "message_id_invalid" in s:
                    await adb.set_fields(aid, deleted=1, active=0)
                    res["gone"] = True
                    break
                if any(k in s for k in _DEAD):
                    await adb.set_group_active(chat_id, 0)
                await adb.add_envio(aid, chat_id, None, False, error=str(e))
                res["fail"].append((title, short(str(e), 40)))
                continue
            except TelegramError as e:
                await adb.add_envio(aid, chat_id, None, False, error=str(e))
                res["fail"].append((title, short(str(e), 40)))
                continue
            except Exception as e:  # nunca tumbar el envío completo
                logger.exception("Error inesperado enviando anuncio %s a %s", aid, chat_id)
                res["fail"].append((title, "error interno"))
                continue

            pinned, unpin_at = 0, None
            if a["pin"]:
                try:
                    await _retry(bot.pin_chat_message, chat_id, msg.message_id, disable_notification=silent)
                    pinned = 1
                    res["pinned"] += 1
                    if a["unpin_h"]:
                        unpin_at = int(time.time()) + a["unpin_h"] * 3600
                except TelegramError:
                    res["pin_fail"].append((chat_id, title))
            await adb.add_envio(aid, chat_id, msg.message_id, True, bool(pinned), unpin_at)
            res["ok"] += 1

            if prev:  # borrar el envío anterior DESPUÉS de enviar el nuevo
                try:
                    await _retry(bot.delete_message, chat_id, prev["message_id"])
                except TelegramError:
                    pass
                await adb.mark_cleaned(prev["id"])
            await asyncio.sleep(PAUSE_BETWEEN_GROUPS)
    return res


async def _avisar_fijado(bot, aid: int, res: dict, only: Optional[int] = None) -> None:
    nuevos = [(c, t) for c, t in res["pin_fail"] if (aid, c) not in _pin_warned or only]
    if not nuevos:
        return
    for c, _ in nuevos:
        _pin_warned.add((aid, c))
    lista = "\n".join(f"• {h(short(t, 40))}" for _, t in nuevos[:15])
    await _notify_owners(
        bot,
        f"⚠️ Anuncio #{aid}: se envió pero NO pude fijarlo en {len(nuevos)} grupo(s) "
        f"(falta el permiso «Fijar mensajes»):\n{lista}",
        only,
    )


def fmt_result(aid: int, res: dict) -> str:
    if res["gone"] and not res["ok"]:
        return f"🗑 El anuncio #{aid} ya no existe en el canal (fue borrado). Lo marqué como borrado."
    t = f"📨 <b>Anuncio #{aid}</b>\n✅ Enviados: <b>{res['ok']}</b>\n⚠️ Fallidos: <b>{len(res['fail'])}</b>"
    if res["pinned"] or res["pin_fail"]:
        t += f"\n📌 Fijados: {res['pinned']}" + (f" (sin permiso en {len(res['pin_fail'])})" if res["pin_fail"] else "")
    if res["fail"]:
        t += "\n\n" + "\n".join(f"• {h(short(n, 28))}: {h(e)}" for n, e in res["fail"][:10])
        if len(res["fail"]) > 10:
            t += f"\n… y {len(res['fail']) - 10} más"
    return t


# ----------------------------------------------------------------------------
# Planificador (tick) y modo por mensajes
# ----------------------------------------------------------------------------
async def anuncios_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        adb = _adb(context.application)
        bot = context.bot
        now = int(time.time())
        # 1) desfijar vencidos (aunque esté en pausa u horario nocturno)
        for e in await adb.due_unpins(now):
            try:
                await _retry(bot.unpin_chat_message, e["chat_id"], e["message_id"])
            except TelegramError:
                pass
            await adb.mark_cleaned(e["id"])
            await asyncio.sleep(PAUSE_BETWEEN_GROUPS)
        # 2) fin de vigencia
        await adb.expire_ended(now)
        if await is_paused(adb) or await is_quiet(adb):
            return
        # 3) anuncios por tiempo
        await adb.fix_time_without_next(now)
        for a in await adb.due_time(now):
            await adb.set_fields(a["id"], next_run=now + a["interval_min"] * 60)  # avanza primero
            ids = [g["chat_id"] for g in await adb.target_groups()]
            if not ids:
                continue
            res = await enviar_anuncio(bot, adb, a["id"], ids)
            logger.info("Anuncio #%s (tiempo): %d ok, %d fallidos", a["id"], res["ok"], len(res["fail"]))
            await _avisar_fijado(bot, a["id"], res)
    except Exception:
        logger.exception("Error en el tick de anuncios")


async def contar_mensajes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cuenta mensajes de usuarios reales por grupo y dispara el modo 'por mensajes'."""
    try:
        m, u, chat = update.effective_message, update.effective_user, update.effective_chat
        if m is None or u is None or chat is None or u.is_bot or u.id == 777000:
            return
        if m.text and m.text.startswith("/"):
            return
        adb = _adb(context.application)
        if chat.id not in _seen_groups:
            await adb.ensure_group(chat.id, chat.title or str(chat.id))
            _seen_groups.add(chat.id)
        cfgs = await adb.active_by_mode("msgs")
        if not cfgs:
            return
        g = await adb.get_group(chat.id)
        if not g or not g["active"] or not g["selected"] or g["blacklisted"]:
            return
        now = int(time.time())
        blocked = await is_paused(adb) or await is_quiet(adb)
        for a in cfgs:
            if a["end_ts"] and a["end_ts"] <= now:
                continue
            cnt, last = await adb.bump(a["id"], chat.id)
            if blocked or a["every_msgs"] <= 0 or cnt < a["every_msgs"]:
                continue
            if now - last < a["cooldown_min"] * 60:
                continue
            key = (a["id"], chat.id)
            if key in _inflight:
                continue
            _inflight.add(key)
            await adb.reset_counter(a["id"], chat.id, now)
            context.application.create_task(_send_count(context.application, a["id"], chat.id))
    except Exception:
        logger.exception("Error contando mensajes para anuncios")


async def _send_count(app: Application, aid: int, chat_id: int) -> None:
    try:
        res = await enviar_anuncio(app.bot, _adb(app), aid, [chat_id])
        await _avisar_fijado(app.bot, aid, res)
    except Exception:
        logger.exception("Error enviando anuncio por mensajes")
    finally:
        _inflight.discard((aid, chat_id))


# ----------------------------------------------------------------------------
# Pantallas (devuelven (texto, teclado))
# ----------------------------------------------------------------------------
async def scr_menu(adb: AnunciosDB):
    paused = await is_paused(adb)
    gc = await adb.group_counts()
    t = (
        "📣 <b>Panel de anuncios</b>\n\n"
        f"Anuncios: {await adb.count_anuncios()} ({await adb.count_active()} activos)\n"
        f"Grupos: {gc['act']} activos, {gc['sel']} seleccionados, {gc['bl']} en lista negra\n"
        f"Estado: {'⏸ <b>EN PAUSA</b>' if paused else '▶️ funcionando'}"
    )
    kb = Kb([
        [Btn("📣 Anuncios", callback_data="an:l:0"), Btn("👥 Grupos", callback_data="an:g:0")],
        [Btn("⚙️ Ajustes", callback_data="an:s"),
         Btn("▶️ Reanudar todo" if paused else "⏸ Pausar todo", callback_data="an:p")],
        [Btn("📊 Estadísticas", callback_data="an:st:0")],
        [Btn("❌ Cerrar", callback_data="an:x")],
    ])
    return t, kb


async def scr_list(adb: AnunciosDB, page: int):
    total = await adb.count_anuncios()
    rows_db = await adb.list_anuncios(page * PAGE_ANN, PAGE_ANN)
    t = "📣 <b>Anuncios</b> (✅ activo / ⬜ inactivo)\nToca el nombre para configurarlo."
    if not rows_db:
        t += "\n\nTodavía no hay anuncios. Publica algo en el canal."
    rows = [[
        Btn(chk(a["active"]), callback_data=f"an:ta:{a['id']}:{page}"),
        Btn(f"#{a['id']} {a['tipo']} · {short(a['preview'], 22)}", callback_data=f"an:a:{a['id']}"),
    ] for a in rows_db]
    if total > PAGE_ANN:
        rows.append(pager("an:l", page, total, PAGE_ANN))
    rows.append(nav("an:m"))
    return t, Kb(rows)


async def scr_detail(adb: AnunciosDB, aid: int):
    a = await adb.get_anuncio(aid)
    if not a or a["deleted"]:
        return "🗑 Ese anuncio ya no existe.", Kb([nav("an:l:0")])
    pin = "Sí" + (f" · desfijar {a['unpin_h']} h" if a["unpin_h"] else " · nunca desfijar") if a["pin"] else "No"
    t = (
        f"📣 <b>Anuncio #{aid}</b> ({h(a['tipo'])}, {fts(a['fecha'])})\n"
        f"<i>{h(short(a['preview'], 80))}</i>\n\n"
        f"Estado: {chk(a['active'])} {'activo' if a['active'] else 'inactivo'}\n"
        f"Modo: {mode_text(a)}\n"
        f"Fijar: {pin}\n"
        f"Notificación: {'🔕 silencioso' if a['silent'] else '🔔 con notificación'}\n"
        f"Borrar envío anterior: {'Sí' if a['del_prev'] else 'No'}\n"
        f"Fin: {fts(a['end_ts']) if a['end_ts'] else 'sin fin'}"
    )
    if a["mode"] == "time" and a["active"] and a["next_run"]:
        t += f"\nPróximo envío: {fts(a['next_run'])}"
    kb = Kb([
        [Btn(f"{chk(a['active'])} {'Activo' if a['active'] else 'Inactivo'} (tocar para cambiar)", callback_data=f"an:tg:{aid}:act")],
        [Btn(f"Modo: {mode_text(a)}", callback_data=f"an:md:{aid}")],
        [Btn(f"📌 Fijar: {'Sí' if a['pin'] else 'No'}", callback_data=f"an:pn:{aid}")],
        [Btn("🔕 Silencioso" if a["silent"] else "🔔 Con notificación", callback_data=f"an:tg:{aid}:sil")],
        [Btn(f"🗑 Borrar anterior: {'Sí' if a['del_prev'] else 'No'}", callback_data=f"an:tg:{aid}:dp")],
        [Btn(f"⏳ Fin: {fts(a['end_ts']) if a['end_ts'] else 'sin fin'}", callback_data=f"an:en:{aid}")],
        [Btn("👁 Vista previa", callback_data=f"an:pv:{aid}"), Btn("🚀 Enviar ahora", callback_data=f"an:sn:{aid}")],
        nav("an:l:0"),
    ])
    return t, kb


async def scr_mode(adb: AnunciosDB, aid: int):
    a = await adb.get_anuncio(aid)
    if not a:
        return "🗑 Ese anuncio ya no existe.", Kb([nav("an:l:0")])
    m = a["mode"]
    t = f"⏱ <b>Modo del anuncio #{aid}</b>\nActual: {mode_text(a)}"
    mk = lambda key, lbl: Btn(("✔ " if m == key else "") + lbl, callback_data=f"an:mm:{aid}:{key}")
    rows = [[mk("time", "⏱ Por tiempo"), mk("msgs", "💬 Por mensajes"), mk("man", "🖐 Manual")]]
    if m == "time":
        opts = [30, 60, 180, 360, 720, 1440]
        rows.append([Btn(("✔ " if a["interval_min"] == v else "") + fmin(v), callback_data=f"an:iv:{aid}:{v}") for v in opts[:3]])
        rows.append([Btn(("✔ " if a["interval_min"] == v else "") + fmin(v), callback_data=f"an:iv:{aid}:{v}") for v in opts[3:]])
    elif m == "msgs":
        opts = [50, 100, 200, 500]
        rows.append([Btn(("✔ " if a["every_msgs"] == v else "") + f"cada {v}", callback_data=f"an:ev:{aid}:{v}") for v in opts])
        custom = a["every_msgs"] not in opts and a["every_msgs"] > 0
        extra = " (" + str(a["every_msgs"]) + ")" if custom else ""
        rows.append([Btn(("✔ " if custom else "") + "✏️ Personalizado" + extra, callback_data=f"an:cu:{aid}")])
        t += "\n\nCooldown mínimo entre envíos en un mismo grupo:"
        rows.append([Btn(("✔ " if a["cooldown_min"] == v else "") + ("sin" if v == 0 else fmin(v)), callback_data=f"an:cd:{aid}:{v}") for v in (0, 10, 30, 60)])
    else:
        t += "\n\nSolo se envía con 🚀 Enviar ahora o /enviar."
    rows.append(nav(f"an:a:{aid}"))
    return t, Kb(rows)


async def scr_pin(adb: AnunciosDB, aid: int):
    a = await adb.get_anuncio(aid)
    if not a:
        return "🗑 Ese anuncio ya no existe.", Kb([nav("an:l:0")])
    t = f"📌 <b>Fijado del anuncio #{aid}</b>\nSi falta el permiso de fijar, se envía igual y te aviso."
    rows = [[Btn(f"📌 Fijar: {'Sí ✅' if a['pin'] else 'No ⬜'}", callback_data=f"an:pp:{aid}")]]
    if a["pin"]:
        rows.append([Btn(("✔ " if a["unpin_h"] == v else "") + (f"{v} h" if v else "Nunca"), callback_data=f"an:pu:{aid}:{v}") for v in (1, 6, 12, 24, 0)])
        t += "\n\nDesfijar automáticamente después de:"
    rows.append(nav(f"an:a:{aid}"))
    return t, Kb(rows)


async def scr_end(adb: AnunciosDB, aid: int):
    a = await adb.get_anuncio(aid)
    if not a:
        return "🗑 Ese anuncio ya no existe.", Kb([nav("an:l:0")])
    t = f"⏳ <b>Fecha de fin del anuncio #{aid}</b>\nActual: {fts(a['end_ts']) if a['end_ts'] else 'sin fin'}\n\nContando desde ahora:"
    rows = [[Btn(f"{d} d", callback_data=f"an:ek:{aid}:{d}") for d in (1, 3, 7, 30)],
            [Btn(("✔ " if not a["end_ts"] else "") + "Sin fin", callback_data=f"an:ek:{aid}:0")], nav(f"an:a:{aid}")]
    return t, Kb(rows)


async def scr_groups(adb: AnunciosDB, page: int):
    gc = await adb.group_counts()
    gs = await adb.list_groups(page * PAGE_GRP, PAGE_GRP)
    t = (f"👥 <b>Grupos</b> ({gc['act']} activos, {gc['total'] - gc['act']} inactivos)\n"
         "✅/⬜ = recibe anuncios programados · 🚫 = lista negra (nunca recibe nada)")
    rows = [[Btn("✅ Seleccionar todos", callback_data="an:ga:1"), Btn("⬜ Quitar todos", callback_data="an:ga:0")]]
    for g in gs:
        rows.append([
            Btn(f"{chk(g['selected'])} {short(g['title'], 22)}", callback_data=f"an:gt:{g['chat_id']}:{page}"),
            Btn("🚫" if g["blacklisted"] else "⭕", callback_data=f"an:gb:{g['chat_id']}:{page}"),
        ])
    if gc["act"] > PAGE_GRP:
        rows.append(pager("an:g", page, gc["act"], PAGE_GRP))
    rows.append(nav("an:m"))
    return t, Kb(rows)


async def scr_settings(adb: AnunciosDB):
    on = await adb.get_setting("quiet_on", "0") == "1"
    s, e = int(await adb.get_setting("quiet_start", "23")), int(await adb.get_setting("quiet_end", "7"))
    t = (f"⚙️ <b>Ajustes</b>\nHorario sin envíos: {chk(on)} {'activo' if on else 'desactivado'}\n"
         f"No se envía de {s:02d}:00 a {e:02d}:00 (zona {h(TZ.key)}).\n"
         "Los envíos manuales (🚀 / /enviar) ignoran este horario.")
    kb = Kb([
        [Btn(f"🌙 Horario: {'ACTIVO' if on else 'apagado'}", callback_data="an:sq")],
        [Btn(f"Inicio silencio: {s:02d}:00", callback_data="an:sh:s"), Btn(f"Fin silencio: {e:02d}:00", callback_data="an:sh:e")],
        nav("an:m"),
    ])
    return t, kb


async def scr_hours(adb: AnunciosDB, which: str):
    cur = int(await adb.get_setting("quiet_start" if which == "s" else "quiet_end", "23" if which == "s" else "7"))
    t = "🌙 Elige la hora de <b>inicio</b> del silencio:" if which == "s" else "🌅 Elige la hora de <b>fin</b> del silencio:"
    rows, row = [], []
    for hr in range(24):
        row.append(Btn(("✔" if hr == cur else "") + f"{hr:02d}", callback_data=f"an:sv:{which}:{hr}"))
        if len(row) == 6:
            rows.append(row)
            row = []
    rows.append(nav("an:s"))
    return t, Kb(rows)


async def scr_stats(adb: AnunciosDB, page: int):
    total = await adb.count_anuncios()
    ss = await adb.stats(page * 5, 5)
    t = "📊 <b>Estadísticas por anuncio</b>\n"
    if not ss:
        t += "\nSin datos todavía."
    for s in ss:
        t += (f"\n<b>#{s['id']}</b> {h(s['tipo'])} · {h(short(s['preview'], 25))}\n"
              f"   Grupos recibidos: {s['recibidos']} · envíos OK: {s['envios_ok']} · fallidos: {s['fallidos']}\n"
              f"   Último envío: {fts(s['ultimo'])}\n")
    rows = []
    if total > 5:
        rows.append(pager("an:st", page, total, 5))
    rows.append(nav("an:m"))
    return t, Kb(rows)


async def scr_confirm_send(adb: AnunciosDB, aid: int, n: int, ctx: str, label: str):
    a = await adb.get_anuncio(aid)
    back = f"an:a:{aid}" if ctx == "d" else f"an:ed:{aid}"
    if not a or a["deleted"]:
        return "🗑 Ese anuncio ya no existe.", Kb([nav("an:l:0")])
    t = (f"🚀 ¿Enviar el anuncio <b>#{aid}</b> ({h(short(a['preview'], 40))}) a <b>{n}</b> grupo(s)?\n"
         f"Destino: {h(label)}")
    return t, Kb([[Btn("✅ Confirmar envío", callback_data=f"an:ey:{aid}:{ctx}")], nav(back)])


# --------- /enviar (privado) ---------
async def scr_e_list(adb: AnunciosDB, page: int):
    total = await adb.count_anuncios()
    rows_db = await adb.list_anuncios(page * PAGE_ANN, PAGE_ANN)
    t = "📨 <b>Enviar anuncio</b>\nElige el anuncio:"
    if not rows_db:
        t += "\n\nNo hay anuncios guardados."
    rows = [[Btn(f"#{a['id']} {a['tipo']} · {short(a['preview'], 28)}", callback_data=f"an:ed:{a['id']}")] for a in rows_db]
    if total > PAGE_ANN:
        rows.append(pager("an:e", page, total, PAGE_ANN))
    rows.append([Btn("🏠 Menú", callback_data="an:m"), Btn("❌ Cerrar", callback_data="an:x")])
    return t, Kb(rows)


async def scr_e_dest(aid: int):
    t = f"📨 Anuncio <b>#{aid}</b>: ¿a dónde lo envío?"
    return t, Kb([
        [Btn("🌐 Todos los grupos", callback_data=f"an:ea:{aid}")],
        [Btn("☑️ Algunos grupos", callback_data=f"an:es:{aid}:0")],
        [Btn("1️⃣ Un grupo", callback_data=f"an:eo:{aid}:0")],
        nav("an:e:0"),
    ])


async def scr_e_some(adb: AnunciosDB, aid: int, page: int, sel: set):
    gs = await adb.list_groups(page * PAGE_GRP, PAGE_GRP)
    gc = await adb.group_counts()
    t = f"☑️ Marca los grupos para el anuncio <b>#{aid}</b> ({len(sel)} elegidos):"
    rows = [[Btn(f"{chk(g['chat_id'] in sel)} {short(g['title'], 30)}", callback_data=f"an:esx:{aid}:{g['chat_id']}:{page}")] for g in gs if not g["blacklisted"]]
    if gc["act"] > PAGE_GRP:
        rows.append(pager(f"an:es:{aid}", page, gc["act"], PAGE_GRP))
    rows.append([Btn(f"➡️ Continuar ({len(sel)})", callback_data=f"an:esc:{aid}")])
    rows.append(nav(f"an:ed:{aid}"))
    return t, Kb(rows)


async def scr_e_one(adb: AnunciosDB, aid: int, page: int):
    gs = await adb.list_groups(page * PAGE_GRP, PAGE_GRP)
    gc = await adb.group_counts()
    t = f"1️⃣ Elige el grupo para el anuncio <b>#{aid}</b>:"
    rows = [[Btn(short(g["title"], 34), callback_data=f"an:eg:{aid}:{g['chat_id']}")] for g in gs if not g["blacklisted"]]
    if gc["act"] > PAGE_GRP:
        rows.append(pager(f"an:eo:{aid}", page, gc["act"], PAGE_GRP))
    rows.append(nav(f"an:ed:{aid}"))
    return t, Kb(rows)


# ----------------------------------------------------------------------------
# Comandos
# ----------------------------------------------------------------------------
async def anuncios_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None:
        return
    if chat.type != ChatType.PRIVATE:
        if autorizado(user.id):
            await update.effective_message.reply_text("🔒 Usa /anuncios por privado conmigo.")
        return
    if not autorizado(user.id):
        await update.effective_message.reply_text("Sin permiso.")
        return
    context.user_data.pop("anc_wait", None)
    t, kb = await scr_menu(_adb(context.application))
    await update.effective_message.reply_text(t, reply_markup=kb, parse_mode=ParseMode.HTML)


async def enviar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat, user = update.effective_chat, update.effective_user
    if chat is None or user is None:
        return
    if not autorizado(user.id):
        if chat.type == ChatType.PRIVATE:
            await update.effective_message.reply_text("Sin permiso.")
        return
    adb = _adb(context.application)
    if chat.type == ChatType.PRIVATE:
        t, kb = await scr_e_list(adb, 0)
        await update.effective_message.reply_text(t, reply_markup=kb, parse_mode=ParseMode.HTML)
    elif chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await adb.ensure_group(chat.id, chat.title or str(chat.id))
        t, kb = await _scr_q_list(adb, chat.id, 0)
        await update.effective_message.reply_text(t, reply_markup=kb, parse_mode=ParseMode.HTML)


async def _scr_q_list(adb: AnunciosDB, chat_id: int, page: int):
    total = await adb.count_anuncios()
    rows_db = await adb.list_anuncios(page * PAGE_ANN, PAGE_ANN)
    t = "📨 <b>Enviar anuncio a este grupo</b>" if rows_db else "No hay anuncios guardados."
    rows = [[Btn(f"#{a['id']} {a['tipo']} · {short(a['preview'], 28)}", callback_data=f"an:q:{a['id']}:{chat_id}")] for a in rows_db]
    if total > PAGE_ANN:
        rows.append(pager(f"an:qp:{chat_id}", page, total, PAGE_ANN))
    rows.append([Btn("❌ Cerrar", callback_data="an:x")])
    return t, Kb(rows)


# ----------------------------------------------------------------------------
# Callbacks
# ----------------------------------------------------------------------------
async def _show(query, text: str, kb: Kb) -> None:
    try:
        await query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def _run_and_report(app: Application, aid: int, ids: list[int], chat_id: int, msg_id: int, back: Optional[str], only: int) -> None:
    adb = _adb(app)
    try:
        res = await enviar_anuncio(app.bot, adb, aid, ids)
        await _avisar_fijado(app.bot, aid, res, only=only)
        text = fmt_result(aid, res)
    except Exception:
        logger.exception("Error en envío manual")
        text = "⚠️ Ocurrió un error durante el envío. Revisa los logs."
    try:
        kb = Kb([nav(back)]) if back else Kb([[Btn("❌ Cerrar", callback_data="an:x")]])
        await app.bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id, reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass


@safe_callback
async def anuncios_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if q is None or not q.data:
        return
    if not autorizado(q.from_user.id):
        await q.answer("Sin permiso", show_alert=True)
        return
    adb = _adb(context.application)
    p = q.data.split(":")  # ["an", cmd, ...]
    cmd = p[1] if len(p) > 1 else ""
    ud = context.user_data
    msg = q.message
    now = int(time.time())

    if cmd != "cu":
        ud.pop("anc_wait", None)

    if cmd == "noop":
        await q.answer()
        return
    if cmd == "x":
        await q.answer()
        try:
            await q.message.delete()
        except TelegramError:
            pass
        return

    # --- envío desde un grupo ---
    if cmd == "qp":
        await q.answer()
        t, kb = await _scr_q_list(adb, int(p[2]), int(p[3]))
        return await _show(q, t, kb)
    if cmd == "q":
        aid, cid = int(p[2]), int(p[3])
        await q.answer("Enviando…")
        await q.edit_message_text("⏳ Enviando…")
        context.application.create_task(_run_and_report(context.application, aid, [cid], msg.chat_id, msg.message_id, None, q.from_user.id))
        return

    await q.answer()

    if cmd == "m":
        t, kb = await scr_menu(adb)
    elif cmd == "p":
        await adb.set_setting("paused", "0" if await is_paused(adb) else "1")
        t, kb = await scr_menu(adb)
    elif cmd == "l":
        t, kb = await scr_list(adb, int(p[2]))
    elif cmd == "ta":
        aid, page = int(p[2]), int(p[3])
        await _toggle_active(adb, aid, now)
        t, kb = await scr_list(adb, page)
    elif cmd == "a":
        t, kb = await scr_detail(adb, int(p[2]))
    elif cmd == "tg":
        aid, f = int(p[2]), p[3]
        if f == "act":
            await _toggle_active(adb, aid, now)
        else:
            a = await adb.get_anuncio(aid)
            if a:
                col = {"sil": "silent", "dp": "del_prev"}[f]
                await adb.set_fields(aid, **{col: 1 - a[col]})
        t, kb = await scr_detail(adb, aid)
    elif cmd == "md":
        t, kb = await scr_mode(adb, int(p[2]))
    elif cmd == "mm":
        aid, mode = int(p[2]), p[3]
        a = await adb.get_anuncio(aid)
        if a and mode in ("time", "msgs", "man"):
            f: dict[str, Any] = {"mode": mode}
            if mode == "time":
                f["interval_min"] = a["interval_min"] or 60
                f["next_run"] = now + f["interval_min"] * 60
            if mode == "msgs" and not a["every_msgs"]:
                f["every_msgs"] = 100
            await adb.set_fields(aid, **f)
        t, kb = await scr_mode(adb, aid)
    elif cmd == "iv":
        aid, v = int(p[2]), int(p[3])
        await adb.set_fields(aid, mode="time", interval_min=v, next_run=now + v * 60)
        t, kb = await scr_mode(adb, aid)
    elif cmd == "ev":
        aid, v = int(p[2]), int(p[3])
        await adb.set_fields(aid, mode="msgs", every_msgs=v)
        t, kb = await scr_mode(adb, aid)
    elif cmd == "cd":
        aid, v = int(p[2]), int(p[3])
        await adb.set_fields(aid, cooldown_min=v)
        t, kb = await scr_mode(adb, aid)
    elif cmd == "cu":
        aid = int(p[2])
        ud["anc_wait"] = {"aid": aid, "chat": msg.chat_id, "msg": msg.message_id}
        t = f"✏️ Anuncio #{aid}: escribe cada cuántos mensajes debe enviarse (número entre 5 y 100000)."
        kb = Kb([[Btn("❌ Cancelar", callback_data=f"an:md:{aid}")]])
    elif cmd == "pn":
        t, kb = await scr_pin(adb, int(p[2]))
    elif cmd == "pp":
        aid = int(p[2])
        a = await adb.get_anuncio(aid)
        if a:
            await adb.set_fields(aid, pin=1 - a["pin"])
        t, kb = await scr_pin(adb, aid)
    elif cmd == "pu":
        aid = int(p[2])
        await adb.set_fields(aid, unpin_h=int(p[3]))
        t, kb = await scr_pin(adb, aid)
    elif cmd == "en":
        t, kb = await scr_end(adb, int(p[2]))
    elif cmd == "ek":
        aid, d = int(p[2]), int(p[3])
        await adb.set_fields(aid, end_ts=(now + d * 86400) if d else None)
        t, kb = await scr_end(adb, aid)
    elif cmd == "pv":
        aid = int(p[2])
        a = await adb.get_anuncio(aid)
        if not a or a["deleted"]:
            t, kb = "🗑 Ese anuncio ya no existe.", Kb([nav("an:l:0")])
        else:
            try:
                await context.bot.forward_message(msg.chat_id, a["chat_id"], a["message_id"])
            except BadRequest:
                await adb.set_fields(aid, deleted=1, active=0)
                t, kb = "🗑 El anuncio ya fue borrado del canal.", Kb([nav("an:l:0")])
                return await _show(q, t, kb)
            return
    elif cmd == "sn":  # enviar ahora: confirmación (grupos seleccionados)
        aid = int(p[2])
        ids = [g["chat_id"] for g in await adb.target_groups()]
        ud["anc_tgt"] = ids
        t, kb = await scr_confirm_send(adb, aid, len(ids), "d", "grupos seleccionados (✅)")
    elif cmd == "ey":
        aid, ctx = int(p[2]), p[3]
        ids = ud.get("anc_tgt") or []
        if not ids:
            await q.answer("No hay grupos destino (o la selección expiró).", show_alert=True)
            return
        ud.pop("anc_tgt", None)
        await _show(q, f"⏳ Enviando #{aid} a {len(ids)} grupo(s)…", Kb([[Btn("❌ Cerrar", callback_data="an:x")]]))
        back = f"an:a:{aid}" if ctx == "d" else "an:e:0"
        context.application.create_task(_run_and_report(context.application, aid, list(ids), msg.chat_id, msg.message_id, back, q.from_user.id))
        return
    elif cmd == "g":
        t, kb = await scr_groups(adb, int(p[2]))
    elif cmd == "gt":
        await adb.toggle_group(int(p[2]), "selected")
        t, kb = await scr_groups(adb, int(p[3]))
    elif cmd == "gb":
        await adb.toggle_group(int(p[2]), "blacklisted")
        t, kb = await scr_groups(adb, int(p[3]))
    elif cmd == "ga":
        await adb.set_all_selected(int(p[2]))
        t, kb = await scr_groups(adb, 0)
    elif cmd == "s":
        t, kb = await scr_settings(adb)
    elif cmd == "sq":
        await adb.set_setting("quiet_on", "0" if await adb.get_setting("quiet_on", "0") == "1" else "1")
        t, kb = await scr_settings(adb)
    elif cmd == "sh":
        t, kb = await scr_hours(adb, p[2])
    elif cmd == "sv":
        await adb.set_setting("quiet_start" if p[2] == "s" else "quiet_end", p[3])
        t, kb = await scr_settings(adb)
    elif cmd == "st":
        t, kb = await scr_stats(adb, int(p[2]))
    # ---- /enviar privado ----
    elif cmd == "e":
        t, kb = await scr_e_list(adb, int(p[2]))
    elif cmd == "ed":
        ud.pop("anc_sel", None)
        t, kb = await scr_e_dest(int(p[2]))
    elif cmd == "ea":
        aid = int(p[2])
        ids = [g["chat_id"] for g in await adb.all_groups_sendable()]
        ud["anc_tgt"] = ids
        t, kb = await scr_confirm_send(adb, aid, len(ids), "e", "todos los grupos activos (sin lista negra)")
    elif cmd == "es":
        aid, page = int(p[2]), int(p[3])
        t, kb = await scr_e_some(adb, aid, page, ud.setdefault("anc_sel", set()))
    elif cmd == "esx":
        aid, cid, page = int(p[2]), int(p[3]), int(p[4])
        sel: set = ud.setdefault("anc_sel", set())
        sel.symmetric_difference_update({cid})
        t, kb = await scr_e_some(adb, aid, page, sel)
    elif cmd == "esc":
        aid = int(p[2])
        sel = ud.get("anc_sel") or set()
        if not sel:
            await q.answer("Elige al menos un grupo", show_alert=True)
            return
        ud["anc_tgt"] = list(sel)
        t, kb = await scr_confirm_send(adb, aid, len(sel), "e", "grupos elegidos")
    elif cmd == "eo":
        t, kb = await scr_e_one(adb, int(p[2]), int(p[3]))
    elif cmd == "eg":
        aid, cid = int(p[2]), int(p[3])
        g = await adb.get_group(cid)
        ud["anc_tgt"] = [cid]
        t, kb = await scr_confirm_send(adb, aid, 1, "e", (g or {}).get("title", str(cid)))
    else:
        return
    await _show(q, t, kb)


async def anuncios_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Consume el número personalizado (cada X mensajes) cuando hay uno pendiente."""
    wait = context.user_data.get("anc_wait")
    user, m = update.effective_user, update.effective_message
    if not wait or user is None or m is None or not autorizado(user.id):
        return
    txt = (m.text or "").strip()
    if not txt.isdigit() or not (5 <= int(txt) <= 100000):
        await m.reply_text("⚠️ Escribe solo un número entre 5 y 100000.")
        raise ApplicationHandlerStop
    adb = _adb(context.application)
    aid = wait["aid"]
    context.user_data.pop("anc_wait", None)
    await adb.set_fields(aid, mode="msgs", every_msgs=int(txt))
    try:
        await m.delete()
    except TelegramError:
        pass
    t, kb = await scr_mode(adb, aid)
    try:
        await context.bot.edit_message_text(t, chat_id=wait["chat"], message_id=wait["msg"], reply_markup=kb, parse_mode=ParseMode.HTML)
    except TelegramError:
        await context.bot.send_message(wait["chat"], t, reply_markup=kb, parse_mode=ParseMode.HTML)
    raise ApplicationHandlerStop


async def _toggle_active(adb: AnunciosDB, aid: int, now: int) -> None:
    a = await adb.get_anuncio(aid)
    if not a or a["deleted"]:
        return
    new = 1 - a["active"]
    f: dict[str, Any] = {"active": new}
    if new and a["mode"] == "time" and a["interval_min"]:
        f["next_run"] = now + a["interval_min"] * 60
    await adb.set_fields(aid, **f)


# ----------------------------------------------------------------------------
# Registro (llamado desde main.py)
# ----------------------------------------------------------------------------
async def init_anuncios(application: Application) -> None:
    """post_init: abre la BD, registra grupos ya conocidos y arranca el planificador."""
    adb = AnunciosDB(DB_PATH)
    await adb.connect()
    application.bot_data[CB_BOT_DATA] = adb
    try:  # siembra de grupos que el bot de moderación ya conocía (solo lectura)
        main_db = application.bot_data.get("db")
        if main_db is not None:
            for gid, title in await main_db.get_known_groups():
                await adb.ensure_group(gid, title)
    except Exception:
        logger.exception("No se pudieron sembrar los grupos conocidos")
    if application.job_queue is not None:
        application.job_queue.run_repeating(anuncios_tick, interval=TICK_SECONDS, first=10, name="anuncios_tick")
    else:
        logger.error("JobQueue no disponible: instala python-telegram-bot[job-queue]. Los anuncios programados NO correrán.")
    if not CHANNEL_ID:
        logger.warning("ANUNCIOS_CHANNEL_ID no está en .env: no se guardarán anuncios del canal.")
    logger.info("Sistema de anuncios listo (BD: %s)", DB_PATH)


async def close_anuncios(application: Application) -> None:
    adb: Optional[AnunciosDB] = application.bot_data.get(CB_BOT_DATA)
    if adb is not None:
        await adb.close()


def register_anuncios_handlers(application: Application) -> None:
    """Grupos -30/-19/-18/-17: corren ANTES del resto y no cortan la propagación."""
    if CHANNEL_ID:
        application.add_handler(
            MessageHandler(filters.UpdateType.CHANNEL_POSTS & filters.Chat(chat_id=CHANNEL_ID), on_channel_post),
            group=-30,
        )
    application.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER), group=-30)
    application.add_handler(
        MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL & ~filters.COMMAND, contar_mensajes),
        group=-19,
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, anuncios_text_input), group=-18
    )
    application.add_handler(CommandHandler("anuncios", anuncios_command), group=-17)
    application.add_handler(CommandHandler("enviar", enviar_command), group=-17)
    application.add_handler(CallbackQueryHandler(anuncios_callback, pattern=r"^an:"), group=-17)
