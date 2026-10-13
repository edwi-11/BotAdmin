"""
handlers/votaciones.py
Sistema de votaciones por grupo.

Comandos (en el grupo, o en privado con selector de grupo):
    /NuevaVotacion  — asistente (por privado) para crear una votación.
    /MenuVotacion   — menú para listar, ver, editar, eliminar, lanzar,
                      cerrar y ver resultados.

Quién puede administrar votaciones: el propietario del BOT (OWNER_IDS) y el
propietario (creador) del GRUPO. Nadie más, ni siquiera otros admins.
Los grupos no activados con /activar solo los puede usar el propietario del bot.

Flujo:
 1. Se crea la votación (borrador): título, candidatos (@usuario) y duración
    (ej. 30m, 2h, 1d, o 0 = solo cierre manual). Se guarda en SQLite.
 2. "Lanzar" la publica en el grupo con un botón que abre el privado del bot
    (?start=vote_<id>).
 3. Cada persona vota en el PRIVADO del bot escribiendo @usuario del candidato.
    La PRIMARY KEY (votacion_id, voter_id) impide votar dos veces.
 4. Cierre automático (job cada 15 s, sobrevive reinicios porque se basa en
    closes_at guardado en la base) o manual. Al cerrar, los resultados se
    envían al privado del propietario del grupo.
 5. Todo vive en la base de datos, así que se conserva al reiniciar el bot.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from config import settings
from database import Database
from utils.callbacks import safe_callback
from utils.formatting import humanize_seconds
from utils.permissions import is_owner
from utils.time_parser import parse_duration

logger = logging.getLogger(__name__)

_INPUT_KEY = "vot_input"      # asistente de creación/edición en curso
_VOTING_KEY = "vot_voting"    # id de la votación en la que la persona está votando
_START_RE = re.compile(r"^(vote|vmenu|vnew)_(-?\d+)$")
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_VOTE_TEXT_RE = re.compile(r"^@([A-Za-z][A-Za-z0-9_]{3,31})$")
SWEEP_SECONDS = 15
STATUS_ICON = {"draft": "📝", "active": "🟢", "closed": "🔒"}
STATUS_NAME = {"draft": "Borrador", "active": "Activa", "closed": "Cerrada"}


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _e(text) -> str:
    return html.escape(str(text))


# --------------------------------------------------------------------- #
# Permisos
# --------------------------------------------------------------------- #
async def _is_group_creator(bot, group_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(group_id, user_id)
    except TelegramError:
        return False
    return member.status == ChatMemberStatus.OWNER


async def _authorize(context, group_id: int, user_id: int) -> str | None:
    """None si puede administrar votaciones en ese grupo; si no, el motivo."""
    if is_owner(user_id):
        return None
    if not await _get_db_activated(context, group_id):
        return "Este grupo no está activado para usar el bot."
    if not await _is_group_creator(context.bot, group_id, user_id):
        return "Solo el propietario del grupo o el propietario del bot pueden administrar votaciones."
    return None


async def _get_db_activated(context, group_id: int) -> bool:
    return await _db(context).is_group_activated(group_id)


async def _manageable_groups(context, user_id: int) -> list[tuple[int, str]]:
    db = _db(context)
    groups = await db.get_known_groups()
    if is_owner(user_id):
        return groups

    async def check(gid: int, title: str):
        if not await db.is_group_activated(gid):
            return None
        return (gid, title) if await _is_group_creator(context.bot, gid, user_id) else None

    results = await asyncio.gather(*(check(g, t) for g, t in groups))
    return [r for r in results if r]


async def _group_owner_id(bot, group_id: int) -> int | None:
    try:
        admins = await bot.get_chat_administrators(group_id)
    except TelegramError:
        return None
    for adm in admins:
        if adm.status == ChatMemberStatus.OWNER:
            return adm.user.id
    return None


# --------------------------------------------------------------------- #
# Textos y teclados
# --------------------------------------------------------------------- #
def _parse_usernames(text: str) -> tuple[list[str], list[str]]:
    """Devuelve (válidos sin repetir, inválidos)."""
    good: list[str] = []
    bad: list[str] = []
    for token in re.split(r"[\s,;]+", text.strip()):
        if not token:
            continue
        name = token.lstrip("@")
        if _USERNAME_RE.match(name):
            if name.lower() not in good:
                good.append(name.lower())
        else:
            bad.append(token)
    return good, bad


def _parse_duration_secs(text: str) -> int | None:
    t = text.strip().lower()
    if t in ("0", "manual", "sin", "ninguna", "no"):
        return 0
    td = parse_duration(t)
    return int(td.total_seconds()) if td else None


def _duration_label(secs: int) -> str:
    return humanize_seconds(secs) if secs > 0 else "solo cierre manual"


async def _view_content(db: Database, vid: int):
    v = await db.get_votacion(vid)
    if v is None:
        return None, None
    cands = await db.get_votacion_candidates(vid)
    counts = await db.get_votacion_counts(vid)
    total = sum(c for _n, c in counts)
    lines = [
        f"{STATUS_ICON[v['status']]} <b>Votación #{vid}</b> — {STATUS_NAME[v['status']]}",
        f"<b>{_e(v['title'])}</b>",
        "",
        "<b>Candidatos:</b>",
        *[f"  • @{_e(c['username'])}" for c in cands],
        "",
        f"⏱ Duración: {_duration_label(v['duration_secs'])}",
    ]
    if v["status"] == "active":
        lines.append(f"🗳 Votos hasta ahora: {total}")
        if v["closes_at"]:
            lines.append(f"⏳ Cierra en: {humanize_seconds(max(0, v['closes_at'] - int(time.time())))}")
    if v["status"] == "closed":
        lines.append(f"🗳 Votos totales: {total}")
    status = v["status"]
    rows: list[list[InlineKeyboardButton]] = []
    if status == "draft":
        rows.append([InlineKeyboardButton("🚀 Lanzar en el grupo", callback_data=f"vt:launch:{vid}")])
    if status == "active":
        rows.append([
            InlineKeyboardButton("🔒 Cerrar ahora", callback_data=f"vt:close:{vid}"),
            InlineKeyboardButton("📊 Parciales", callback_data=f"vt:res:{vid}"),
        ])
    if status == "closed":
        rows.append([InlineKeyboardButton("📊 Ver resultados", callback_data=f"vt:res:{vid}")])
    if status != "closed":
        rows.append([InlineKeyboardButton("✏️ Editar", callback_data=f"vt:edit:{vid}")])
    rows.append([
        InlineKeyboardButton("🗑 Eliminar", callback_data=f"vt:del:{vid}"),
        InlineKeyboardButton("⬅️ Lista", callback_data=f"vt:list:{v['group_id']}"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _menu_content(title: str, gid: int, show_groups_back: bool):
    text = f"🗳 <b>Votaciones — {_e(title)}</b>\n\nElegí una opción:"
    rows = [
        [InlineKeyboardButton("➕ Nueva votación", callback_data=f"vt:new:{gid}")],
        [InlineKeyboardButton("📋 Listar votaciones", callback_data=f"vt:list:{gid}")],
        [InlineKeyboardButton("👁 Vista previa global", callback_data=f"vt:glob:{gid}")],
        [InlineKeyboardButton("🚀 Lanzar todas las pendientes", callback_data=f"vt:launchall:{gid}")],
        [InlineKeyboardButton("📊 Resultados de todas", callback_data=f"vt:resall:{gid}")],
    ]
    if show_groups_back:
        rows.append([InlineKeyboardButton("⬅️ Cambiar de grupo", callback_data="vt:groups")])
    return text, InlineKeyboardMarkup(rows)


async def _results_text(db: Database, v) -> str:
    counts = await db.get_votacion_counts(v["id"])
    total = sum(c for _n, c in counts)
    state = "FINAL" if v["status"] == "closed" else "PARCIAL"
    lines = [f"📊 <b>Resultados {state} — #{v['id']}</b>", f"<b>{_e(v['title'])}</b>", ""]
    top = counts[0][1] if counts else 0
    for name, votes in counts:
        pct = (votes / total * 100) if total else 0
        bar = "█" * round(pct / 10) + "░" * (10 - round(pct / 10))
        crown = " 👑" if votes == top and top > 0 and v["status"] == "closed" else ""
        lines.append(f"@{_e(name)}{crown}\n{bar} {votes} ({pct:.0f}%)")
    lines.append("")
    lines.append(f"🗳 Total de votos: {total}")
    if v["status"] == "closed" and top > 0:
        winners = [n for n, c in counts if c == top]
        if len(winners) == 1:
            lines.append(f"🏆 Ganador: @{_e(winners[0])}")
        else:
            lines.append("🤝 Empate entre: " + ", ".join(f"@{_e(w)}" for w in winners))
    elif v["status"] == "closed":
        lines.append("Nadie votó.")
    return "\n".join(lines)


async def _send_chunks(bot, chat_id: int, parts: list[str]) -> None:
    buf = ""
    for part in parts:
        if buf and len(buf) + len(part) + 2 > 3800:
            await bot.send_message(chat_id, buf, parse_mode="HTML")
            buf = ""
        buf = f"{buf}\n\n{part}" if buf else part
    if buf:
        await bot.send_message(chat_id, buf, parse_mode="HTML")


async def _edit(query, text: str, markup=None) -> None:
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


# --------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------- #
async def _open_in_private(update, context, gid: int, kind: str) -> None:
    """kind: 'menu' o 'new'. Se llama desde el grupo: lleva al privado."""
    user = update.effective_user
    message = update.effective_message
    try:
        if kind == "new":
            await _start_wizard(context, user.id, gid)
        else:
            title = await _db(context).get_group_title(gid) or str(gid)
            text, markup = _menu_content(title, gid, False)
            await context.bot.send_message(user.id, text, parse_mode="HTML", reply_markup=markup)
        await message.reply_text("📩 Te lo mandé por privado.")
    except Forbidden:
        link = f"https://t.me/{context.bot.username}?start={'vnew' if kind == 'new' else 'vmenu'}_{gid}"
        await message.reply_text(
            "Primero abrí el chat privado conmigo para continuar:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💬 Abrir privado", url=link)]]),
        )


async def _group_command(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str) -> None:
    chat, user, message = update.effective_chat, update.effective_user, update.effective_message
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        reason = await _authorize(context, chat.id, user.id)
        if reason:
            await message.reply_text(f"❌ {reason}")
            return
        await _db(context).upsert_group(chat.id, chat.title)
        await _open_in_private(update, context, chat.id, kind)
        return
    # Privado: selector de grupo.
    await _show_group_picker(message.reply_text, context, user.id, kind)


async def nueva_votacion_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _group_command(update, context, "new")


async def menu_votacion_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _group_command(update, context, "menu")


async def _show_group_picker(send, context, user_id: int, kind: str) -> None:
    groups = await _manageable_groups(context, user_id)
    if not groups:
        await send("No encontré grupos donde puedas administrar votaciones.")
        return
    prefix = "new" if kind == "new" else "menu"
    rows = [[InlineKeyboardButton(t[:40], callback_data=f"vt:{prefix}:{g}")] for g, t in groups[:40]]
    await send("Elegí el grupo:", reply_markup=InlineKeyboardMarkup(rows))


# --------------------------------------------------------------------- #
# Asistente (wizard) de creación / edición
# --------------------------------------------------------------------- #
async def _start_wizard(context, user_id: int, gid: int) -> None:
    context.user_data[_INPUT_KEY] = {"kind": "title", "gid": gid, "draft": {}}
    await context.bot.send_message(
        user_id,
        "🗳 <b>Nueva votación</b>\n\nPaso 1/3: escribí el <b>título</b> de la votación.\n"
        "Para salir escribí /cancelar.",
        parse_mode="HTML",
    )


async def try_consume_votacion_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Consume mensajes del privado: pasos del asistente o el voto @usuario."""
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or update.effective_chat.type != ChatType.PRIVATE:
        return False
    text = (message.text or "").strip()
    db = _db(context)

    pending = context.user_data.get(_INPUT_KEY)
    if pending:
        if not text:
            await message.reply_text("Mandame el dato como texto, por favor.")
            return True
        return await _consume_wizard(update, context, pending, text)

    vid = context.user_data.get(_VOTING_KEY)
    if vid:
        return await _consume_vote_text(update, context, vid, text)

    # Sin estado en memoria (p. ej. tras reiniciar el bot): si manda solo
    # "@usuario" y hay una única votación activa donde ese usuario es
    # candidato y la persona puede votar, se toma como voto.
    m = _VOTE_TEXT_RE.match(text)
    if not m:
        return False
    candidates = await db.get_active_votaciones_with_candidate(m.group(1))
    eligible = []
    for v in candidates:
        if await db.has_voted(v["id"], user.id):
            continue
        if await _is_member(context.bot, v["group_id"], user.id):
            eligible.append(v)
    if not eligible:
        return False
    if len(eligible) > 1:
        await message.reply_text(
            "Hay varias votaciones abiertas con ese candidato. Tocá el botón «Votar» "
            "del mensaje de la votación en el grupo para elegir cuál."
        )
        return True
    return await _consume_vote_text(update, context, eligible[0]["id"], text)


async def _consume_wizard(update, context, pending: dict, text: str) -> bool:
    message, user = update.effective_message, update.effective_user
    db = _db(context)
    kind, gid = pending["kind"], pending["gid"]

    # Se vuelve a verificar el permiso en cada paso.
    reason = await _authorize(context, gid, user.id)
    if reason:
        context.user_data.pop(_INPUT_KEY, None)
        await message.reply_text(f"❌ {reason}")
        return True

    if kind in ("title", "edit_title"):
        if len(text) > 200:
            await message.reply_text("Ese título es muy largo (máximo 200 caracteres). Probá con uno más corto.")
            return True
        if kind == "title":
            pending["draft"]["title"] = text
            pending["kind"] = "cands"
            await message.reply_text(
                "Paso 2/3: mandame los <b>candidatos</b> con su @usuario, separados por espacios, comas "
                "o saltos de línea (mínimo 2).\nEjemplo: <code>@ana @luis @maria</code>",
                parse_mode="HTML",
            )
            return True
        await db.update_votacion(pending["vid"], title=text)
        return await _finish_wizard(update, context, pending["vid"], "Título actualizado.")

    if kind in ("cands", "add_cands"):
        good, bad = _parse_usernames(text)
        if bad:
            await message.reply_text(
                "Estos usuarios no son válidos: " + ", ".join(_e(b) for b in bad)
                + "\nUsá @usuario (5-32 caracteres, letras, números o _). Probá de nuevo.",
                parse_mode="HTML",
            )
            return True
        if kind == "cands":
            if len(good) < 2:
                await message.reply_text("Necesito al menos 2 candidatos distintos. Mandámelos de nuevo.")
                return True
            pending["draft"]["cands"] = good
            pending["kind"] = "dur"
            await message.reply_text(
                "Paso 3/3: <b>duración</b> de la votación. Ejemplos: <code>30m</code>, <code>2h</code>, "
                "<code>1d</code>. Escribí <code>0</code> para cerrarla solo a mano.",
                parse_mode="HTML",
            )
            return True
        if not good:
            await message.reply_text("No encontré ningún @usuario. Probá de nuevo.")
            return True
        added = await db.add_votacion_candidates(pending["vid"], good)
        return await _finish_wizard(update, context, pending["vid"], f"Candidatos agregados: {added}.")

    if kind in ("dur", "edit_dur"):
        secs = _parse_duration_secs(text)
        if secs is None:
            await message.reply_text("No entendí la duración. Usá por ejemplo 30m, 2h, 1d, o 0 para solo manual.")
            return True
        if kind == "dur":
            d = pending["draft"]
            vid = await db.create_votacion(gid, d["title"], d["cands"], secs, user.id)
            return await _finish_wizard(update, context, vid, "Votación creada (borrador).")
        await db.update_votacion(pending["vid"], duration_secs=secs)
        return await _finish_wizard(update, context, pending["vid"], "Duración actualizada.")

    context.user_data.pop(_INPUT_KEY, None)
    return False


async def _finish_wizard(update, context, vid: int, note: str) -> bool:
    context.user_data.pop(_INPUT_KEY, None)
    text, markup = await _view_content(_db(context), vid)
    await update.effective_message.reply_text(f"✅ {note}\n\n{text}", parse_mode="HTML", reply_markup=markup)
    return True


async def cancelar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancelar: corta el asistente/voto de votaciones; si no había nada,
    cae al /cancelar de las confesiones (mismo comando, ya existente)."""
    popped = context.user_data.pop(_INPUT_KEY, None) or context.user_data.pop(_VOTING_KEY, None)
    if popped:
        await update.effective_message.reply_text("Listo, cancelado.")
        return
    from handlers.confessions import cancelar_confesion_command
    await cancelar_confesion_command(update, context)


# --------------------------------------------------------------------- #
# Votar
# --------------------------------------------------------------------- #
async def _is_member(bot, group_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(group_id, user_id)
    except TelegramError:
        return False
    if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        return False
    if m.status == ChatMemberStatus.RESTRICTED and not getattr(m, "is_member", True):
        return False
    return True


async def _begin_voting(update, context, vid: int) -> None:
    message, user = update.effective_message, update.effective_user
    db = _db(context)
    v = await db.get_votacion(vid)
    if v is None or v["status"] != "active":
        await message.reply_text("⚠️ Esa votación no está abierta (ya cerró o no existe).")
        return
    if not await _is_member(context.bot, v["group_id"], user.id):
        await message.reply_text("⚠️ Tenés que ser miembro del grupo para votar.")
        return
    if await db.has_voted(vid, user.id):
        await message.reply_text("⚠️ Ya votaste en esta votación. Solo se permite un voto por persona.")
        return
    cands = await db.get_votacion_candidates(vid)
    context.user_data[_VOTING_KEY] = vid
    await message.reply_text(
        f"🗳 <b>{_e(v['title'])}</b>\n\nCandidatos:\n"
        + "\n".join(f"  • @{_e(c['username'])}" for c in cands)
        + "\n\nEscribime el <b>@usuario</b> del candidato por el que querés votar. "
        "Ojo: el voto no se puede cambiar. /cancelar para salir.",
        parse_mode="HTML",
    )


async def _consume_vote_text(update, context, vid: int, text: str) -> bool:
    message, user = update.effective_message, update.effective_user
    db = _db(context)
    v = await db.get_votacion(vid)
    if v is None or v["status"] != "active":
        context.user_data.pop(_VOTING_KEY, None)
        await message.reply_text("⚠️ Esa votación ya no está abierta. Tu voto no se registró.")
        return True
    if await db.has_voted(vid, user.id):
        context.user_data.pop(_VOTING_KEY, None)
        await message.reply_text("⚠️ Ya votaste en esta votación. Solo se permite un voto por persona.")
        return True
    if not await _is_member(context.bot, v["group_id"], user.id):
        context.user_data.pop(_VOTING_KEY, None)
        await message.reply_text("⚠️ Tenés que ser miembro del grupo para votar.")
        return True
    name = text.lstrip("@").strip().lower()
    cands = await db.get_votacion_candidates(vid)
    chosen = next((c for c in cands if c["username"] == name), None)
    if chosen is None:
        await message.reply_text(
            "Ese @usuario no es candidato. Elegí uno de:\n"
            + "\n".join(f"  • @{_e(c['username'])}" for c in cands),
            parse_mode="HTML",
        )
        return True
    if not await db.cast_vote(vid, user.id, chosen["id"]):
        context.user_data.pop(_VOTING_KEY, None)
        await message.reply_text("⚠️ Ya votaste en esta votación. Solo se permite un voto por persona.")
        return True
    context.user_data.pop(_VOTING_KEY, None)
    await message.reply_text(
        f"✅ ¡Voto registrado para @{_e(chosen['username'])} en «{_e(v['title'])}»!", parse_mode="HTML"
    )
    return True


async def handle_votacion_start_deeplink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Desde el /start general. True si el parámetro era de votaciones."""
    args = context.args or []
    if not args:
        return False
    m = _START_RE.match(args[0])
    if not m:
        return False
    kind, num = m.group(1), int(m.group(2))
    message, user = update.effective_message, update.effective_user
    if kind == "vote":
        await _begin_voting(update, context, num)
        return True
    reason = await _authorize(context, num, user.id)
    if reason:
        await message.reply_text(f"❌ {reason}")
        return True
    if kind == "vnew":
        await _start_wizard(context, user.id, num)
    else:
        title = await _db(context).get_group_title(num) or str(num)
        text, markup = _menu_content(title, num, False)
        await message.reply_text(text, parse_mode="HTML", reply_markup=markup)
    return True


# --------------------------------------------------------------------- #
# Lanzar / cerrar
# --------------------------------------------------------------------- #
async def _launch(context, vid: int) -> str | None:
    """Publica la votación en el grupo. None si salió bien, o el error."""
    db = _db(context)
    v = await db.get_votacion(vid)
    if v is None:
        return "La votación no existe."
    if not await db.launch_votacion(vid):
        return "Solo se pueden lanzar votaciones en borrador."
    v = await db.get_votacion(vid)
    cands = await db.get_votacion_candidates(vid)
    when = f"⏳ Cierra en {humanize_seconds(v['duration_secs'])}." if v["duration_secs"] else "🔒 Se cierra manualmente."
    text = (
        f"🗳 <b>VOTACIÓN ABIERTA</b>\n\n<b>{_e(v['title'])}</b>\n\nCandidatos:\n"
        + "\n".join(f"  • @{_e(c['username'])}" for c in cands)
        + f"\n\n{when}\nTocá el botón, abrí mi privado y escribí el @usuario por el que votás."
    )
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🗳 Votar", url=f"https://t.me/{context.bot.username}?start=vote_{vid}")]])
    try:
        sent = await context.bot.send_message(v["group_id"], text, parse_mode="HTML", reply_markup=markup)
    except TelegramError as exc:
        await db.revert_votacion_to_draft(vid)
        return f"No pude publicarla en el grupo: {exc}"
    await db.set_votacion_group_message(vid, sent.message_id)
    return None


async def finalize_votacion(bot, db: Database, vid: int, closed_by: int | None, reason: str) -> tuple[bool, int | None]:
    """Cierra (una sola vez) y manda los resultados al propietario del grupo.
    Devuelve (se_cerró, id_del_que_recibió_los_resultados)."""
    if not await db.close_votacion(vid, closed_by, reason):
        return False, None
    v = await db.get_votacion(vid)
    text = await _results_text(db, v)
    header = "⏰ Se cerró automáticamente por tiempo.\n\n" if reason == "auto" else "🔒 Votación cerrada manualmente.\n\n"
    group_title = await db.get_group_title(v["group_id"]) or str(v["group_id"])
    text = f"{header}Grupo: <b>{_e(group_title)}</b>\n\n{text}"

    owner_id = await _group_owner_id(bot, v["group_id"])
    fallbacks = [owner_id, closed_by, v["created_by"], *settings.owner_ids]
    sent_to = None
    tried: set[int] = set()
    for rid in fallbacks:
        if not rid or rid in tried:
            continue
        tried.add(rid)
        try:
            await bot.send_message(rid, text, parse_mode="HTML")
            sent_to = rid
            break
        except TelegramError as exc:
            logger.info("No pude mandar resultados de la votación #%s a %s: %s", vid, rid, exc)
    if sent_to:
        await db.set_votacion_results_sent_to(vid, sent_to)

    # Cartel del grupo: se le saca el botón y se marca como cerrado (sin publicar resultados).
    closed_txt = f"🔒 <b>VOTACIÓN CERRADA</b>\n\n<b>{_e(v['title'])}</b>\n\nLos resultados se enviaron al propietario del grupo."
    try:
        if v["group_message_id"]:
            await bot.edit_message_text(closed_txt, chat_id=v["group_id"], message_id=v["group_message_id"], parse_mode="HTML")
        else:
            await bot.send_message(v["group_id"], closed_txt, parse_mode="HTML")
    except TelegramError:
        try:
            await bot.send_message(v["group_id"], closed_txt, parse_mode="HTML")
        except TelegramError as exc:
            logger.info("No pude avisar el cierre de la votación #%s en el grupo: %s", vid, exc)
    return True, sent_to


async def votaciones_sweep_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cierra las votaciones vencidas. Se basa en closes_at guardado en la
    base, así que también recupera las que vencieron con el bot apagado."""
    db = _db(context)
    for v in await db.get_expired_votaciones():
        try:
            await finalize_votacion(context.bot, db, v["id"], None, "auto")
        except Exception:  # noqa: BLE001
            logger.exception("Error cerrando automáticamente la votación #%s", v["id"])


def schedule_votaciones(application) -> None:
    if application.job_queue is not None:
        application.job_queue.run_repeating(
            votaciones_sweep_job, interval=SWEEP_SECONDS, first=5, name="votaciones_sweep"
        )


# --------------------------------------------------------------------- #
# Callbacks de botones (prefijo vt:)
# --------------------------------------------------------------------- #
@safe_callback
async def votaciones_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    db = _db(context)
    parts = query.data.split(":")
    action = parts[1]
    arg = int(parts[2]) if len(parts) > 2 and re.fullmatch(r"-?\d+", parts[2]) else None
    arg2 = int(parts[3]) if len(parts) > 3 and re.fullmatch(r"-?\d+", parts[3]) else None

    if action == "groups":
        groups = await _manageable_groups(context, user.id)
        rows = [[InlineKeyboardButton(t[:40], callback_data=f"vt:menu:{g}")] for g, t in groups[:40]]
        await _edit(query, "Elegí el grupo:", InlineKeyboardMarkup(rows) if rows else None)
        await query.answer()
        return
    if arg is None:
        await query.answer()
        return

    # Grupo y votación a los que apunta el botón.
    v = None
    if action in ("menu", "new", "list", "glob", "launchall", "resall"):
        gid = arg
    else:
        v = await db.get_votacion(arg)
        if v is None:
            await query.answer("Esa votación ya no existe.", show_alert=True)
            return
        gid = v["group_id"]

    reason = await _authorize(context, gid, user.id)
    if reason:
        await query.answer(reason, show_alert=True)
        return

    vid = v["id"] if v else None
    show_back = len(await _manageable_groups(context, user.id)) > 1 if action == "menu" else False

    if action == "menu":
        title = await db.get_group_title(gid) or str(gid)
        text, markup = _menu_content(title, gid, show_back)
        await _edit(query, text, markup)
    elif action == "new":
        await _start_wizard(context, user.id, gid)
        await query.answer("Seguí en el chat.")
        return
    elif action == "list":
        vs = await db.list_votaciones(gid)
        rows = [[InlineKeyboardButton(f"{STATUS_ICON[x['status']]} #{x['id']} {x['title'][:35]}",
                                      callback_data=f"vt:view:{x['id']}")] for x in vs]
        rows.append([InlineKeyboardButton("⬅️ Menú", callback_data=f"vt:menu:{gid}")])
        await _edit(query, "📋 <b>Votaciones del grupo</b>" if vs else "No hay votaciones creadas todavía.",
                    InlineKeyboardMarkup(rows))
    elif action == "view":
        text, markup = await _view_content(db, vid)
        await _edit(query, text, markup)
    elif action == "glob":
        vs = await db.list_votaciones(gid)
        if not vs:
            body = "No hay votaciones creadas todavía."
        else:
            blocks = []
            for x in vs:
                cands = await db.get_votacion_candidates(x["id"])
                counts = await db.get_votacion_counts(x["id"])
                blocks.append(
                    f"{STATUS_ICON[x['status']]} <b>#{x['id']} {_e(x['title'])}</b> "
                    f"({STATUS_NAME[x['status']]}, {_duration_label(x['duration_secs'])})\n   "
                    + ", ".join(f"@{_e(c['username'])}" for c in cands)
                    + f"\n   🗳 {sum(c for _n, c in counts)} votos"
                )
            body = "👁 <b>Vista previa global</b>\n\n" + "\n\n".join(blocks)
        await _edit(query, body[:4000], InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data=f"vt:menu:{gid}")]]))
    elif action == "launch":
        err = await _launch(context, vid)
        if err:
            await query.answer(err, show_alert=True)
            return
        text, markup = await _view_content(db, vid)
        await _edit(query, "🚀 ¡Lanzada en el grupo!\n\n" + text, markup)
    elif action == "launchall":
        drafts = [x for x in await db.list_votaciones(gid) if x["status"] == "draft"]
        if not drafts:
            await query.answer("No hay votaciones en borrador para lanzar.", show_alert=True)
            return
        ok, fail = 0, []
        for x in drafts:
            err = await _launch(context, x["id"])
            if err:
                fail.append(f"#{x['id']}: {err}")
            else:
                ok += 1
        msg = f"🚀 Lanzadas: {ok}/{len(drafts)}" + ("\n" + "\n".join(fail) if fail else "")
        await query.answer(msg[:190], show_alert=True)
        return
    elif action == "close":
        closed, sent_to = await finalize_votacion(context.bot, db, vid, user.id, "manual")
        if not closed:
            await query.answer("Esa votación ya estaba cerrada.", show_alert=True)
            return
        text, markup = await _view_content(db, vid)
        note = "enviados al propietario del grupo" if sent_to else "no pude enviarlos por privado; usá «Ver resultados»"
        await _edit(query, f"🔒 Votación cerrada. Resultados {note}.\n\n" + text, markup)
    elif action == "res":
        v = await db.get_votacion(vid)
        await query.message.reply_text(await _results_text(db, v), parse_mode="HTML")
    elif action == "resall":
        vs = [x for x in await db.list_votaciones(gid) if x["status"] != "draft"]
        if not vs:
            await query.answer("Todavía no hay votaciones lanzadas.", show_alert=True)
            return
        await _send_chunks(context.bot, query.message.chat_id, [await _results_text(db, x) for x in vs])
    elif action == "edit":
        if v["status"] == "closed":
            await query.answer("Una votación cerrada no se puede editar.", show_alert=True)
            return
        rows = [
            [InlineKeyboardButton("📝 Título", callback_data=f"vt:et:{vid}"),
             InlineKeyboardButton("⏱ Duración", callback_data=f"vt:ed:{vid}")],
            [InlineKeyboardButton("➕ Candidatos", callback_data=f"vt:ea:{vid}"),
             InlineKeyboardButton("➖ Quitar candidato", callback_data=f"vt:er:{vid}")],
            [InlineKeyboardButton("⬅️ Volver", callback_data=f"vt:view:{vid}")],
        ]
        await _edit(query, f"✏️ <b>Editar votación #{vid}</b>\n{_e(v['title'])}", InlineKeyboardMarkup(rows))
    elif action in ("et", "ed", "ea"):
        if v["status"] == "closed":
            await query.answer("Una votación cerrada no se puede editar.", show_alert=True)
            return
        kind, prompt = {
            "et": ("edit_title", "Mandame el nuevo <b>título</b>."),
            "ed": ("edit_dur", "Mandame la nueva <b>duración</b> (30m, 2h, 1d o 0 = solo manual)."),
            "ea": ("add_cands", "Mandame los <b>@usuarios</b> a agregar."),
        }[action]
        context.user_data[_INPUT_KEY] = {"kind": kind, "gid": gid, "vid": vid, "draft": {}}
        await query.message.reply_text(prompt + "\n/cancelar para salir.", parse_mode="HTML")
    elif action == "er":
        if arg2 is None:
            cands = await db.get_votacion_candidates(vid)
            rows = [[InlineKeyboardButton(f"➖ @{c['username']}", callback_data=f"vt:er:{vid}:{c['id']}")] for c in cands]
            rows.append([InlineKeyboardButton("⬅️ Volver", callback_data=f"vt:edit:{vid}")])
            await _edit(query, "¿Qué candidato querés quitar?", InlineKeyboardMarkup(rows))
        else:
            cands = await db.get_votacion_candidates(vid)
            counts = await db.get_votacion_counts(vid)
            if sum(c for _n, c in counts) > 0:
                await query.answer("Ya hay votos registrados: no se pueden quitar candidatos.", show_alert=True)
                return
            if len(cands) <= 2:
                await query.answer("Tiene que quedar al menos 2 candidatos.", show_alert=True)
                return
            await db.remove_votacion_candidate(vid, arg2)
            text, markup = await _view_content(db, vid)
            await _edit(query, "Candidato quitado.\n\n" + text, markup)
    elif action == "del":
        rows = [[InlineKeyboardButton("🗑 Sí, eliminar", callback_data=f"vt:delok:{vid}"),
                 InlineKeyboardButton("Cancelar", callback_data=f"vt:view:{vid}")]]
        await _edit(query, f"¿Eliminar la votación #{vid} «{_e(v['title'])}» con todos sus votos? "
                           "No se puede deshacer.", InlineKeyboardMarkup(rows))
    elif action == "delok":
        was_active = v["status"] == "active"
        await db.delete_votacion(vid)
        if was_active and v["group_message_id"]:
            try:
                await context.bot.edit_message_text(
                    "🚫 Esta votación fue eliminada.", chat_id=v["group_id"], message_id=v["group_message_id"])
            except TelegramError:
                pass
        await _edit(query, f"🗑 Votación #{vid} eliminada.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Lista", callback_data=f"vt:list:{gid}")]]))
    await query.answer()

