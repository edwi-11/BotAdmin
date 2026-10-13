"""
handlers/votaciones.py
Sistema de votaciones por grupo (votación ABIERTA con @usuario).

Comandos (en el grupo, o en privado con selector de grupo):
    /NuevaVotacion  — asistente por privado: título, foto (opcional) y duración.
    /MenuVotacion   — menú: listar, ver, editar, eliminar, lanzar, cerrar y
                      ver resultados (uno por uno o todos con un solo botón).

Quién administra: el propietario del BOT (OWNER_IDS) y el propietario
(creador) del GRUPO. Los grupos sin /activar solo los usa el propietario del bot.

Cómo se vota:
 - Al crear la votación NO se definen candidatos: cada votante manda el @usuario
   que quiera (con o sin @) en el PRIVADO del bot. El bot guarda cada voto y
   suma cuántas veces salió cada usuario.
 - Opcional por votación: «votos por persona» = 1 o 2 (para poder votar por
   2 candidatos en un mismo mensaje).
 - Si hay varias votaciones abiertas, el bot muestra botones para elegir una.
 - Una persona vota UNA sola vez por votación (PRIMARY KEY en la base de datos).
 - Cierre automático (job cada 15 s sobre closes_at, sobrevive reinicios) o manual.
   Al cerrar, los resultados se envían al privado del propietario del grupo.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
import time
import unicodedata

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
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

_INPUT_KEY = "vot_input"        # asistente de creación/edición en curso
_VOTING_KEY = "vot_voting"      # id de la votación en la que la persona está votando
_PENDING_TEXT_KEY = "vot_text"  # voto escrito antes de elegir votación (hay varias abiertas)
_START_RE = re.compile(r"^(vote|votar|vmenu|vnew)_(-?\d+)$")
_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
_FREE_VOTE_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{3,31}(?:[\s,;]+@[A-Za-z][A-Za-z0-9_]{3,31})*$")
SWEEP_SECONDS = 15
STATUS_ICON = {"draft": "📝", "active": "🟢", "closed": "🔒"}
STATUS_NAME = {"draft": "Borrador", "active": "Activa", "closed": "Cerrada"}
_SKIP_WORDS = {"omitir", "no", "skip", "sin", "sin foto", "ninguna"}


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
    if not await _db(context).is_group_activated(group_id):
        return "Este grupo no está activado para usar el bot."
    if not await _is_group_creator(context.bot, group_id, user_id):
        return "Solo el propietario del grupo o el propietario del bot pueden administrar votaciones."
    return None


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


# --------------------------------------------------------------------- #
# Parseo
# --------------------------------------------------------------------- #
def _norm(text: str) -> str:
    """Clave para comparar/sumar votos: sin @, sin acentos, minúsculas, espacios simples."""
    t = unicodedata.normalize("NFKD", text.strip().lstrip("@"))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.casefold().split())


def _parse_options(text: str, max_choices: int) -> tuple[list[tuple[str, str]], str | None]:
    """Convierte el mensaje del votante en [(clave, texto)]. Puede ser un
    @usuario o cualquier texto (ej. «mejor pelea»). Con 2 votos por persona se
    separan con coma, punto y coma o salto de línea (o espacios si son @usuarios).
    Devuelve (opciones, error)."""
    raw = text.strip()
    ws = [t for t in re.split(r"[\s,;]+", raw) if t]
    if len(ws) > 1 and all(t.startswith("@") for t in ws):
        parts = ws
    else:
        parts = [p.strip() for p in re.split(r"[,;\n]+", raw) if p.strip()]
    options: list[tuple[str, str]] = []
    for part in parts:
        if part.startswith("@"):
            if not _USERNAME_RE.match(part[1:]):
                return [], f"«{part}» no es un @usuario válido (5-32 caracteres, letras, números o _)."
            label = "@" + part[1:].lower()
        else:
            label = " ".join(part.split())
        if len(label) > 60:
            return [], "Ese voto es muy largo (máximo 60 caracteres)."
        key = _norm(label)
        if not key:
            return [], "No entendí tu voto."
        if all(k != key for k, _l in options):
            options.append((key, label))
    if not options:
        return [], "No entendí tu voto."
    if len(options) > max_choices:
        return [], (f"En esta votación podés votar por {max_choices} "
                    f"{'opción' if max_choices == 1 else 'opciones'} como máximo.")
    return options, None


def _parse_duration_secs(text: str) -> int | None:
    t = text.strip().lower()
    if t in ("0", "manual", "sin", "ninguna", "no"):
        return 0
    td = parse_duration(t)
    return int(td.total_seconds()) if td else None


def _duration_label(secs: int) -> str:
    return humanize_seconds(secs) if secs > 0 else "solo cierre manual"


# --------------------------------------------------------------------- #
# Textos y teclados
# --------------------------------------------------------------------- #
def _announce_text(v) -> str:
    when = f"⏳ Cierra en {humanize_seconds(v['duration_secs'])}." if v["duration_secs"] else "🔒 Se cierra manualmente."
    n = v["max_choices"]
    how = ("Podés votar por hasta 2 opciones (separadas por coma)." if n == 2
           else "Mandá tu voto: un @usuario o el nombre de la opción.")
    return (f"🗳 <b>VOTACIÓN ABIERTA</b>\n\n<b>{_e(v['title'])}</b>\n\n{how}\n"
            f"{when}\nTocá el botón, abrí mi privado y mandame tu voto.")


async def _view_content(db: Database, vid: int):
    v = await db.get_votacion(vid)
    if v is None:
        return None, None
    voters = await db.count_votacion_voters(vid)
    lines = [
        f"{STATUS_ICON[v['status']]} <b>Votación #{vid}</b> — {STATUS_NAME[v['status']]}",
        f"<b>{_e(v['title'])}</b>",
        "",
        f"📷 Foto: {'sí' if v['photo_file_id'] else 'no'}",
        f"⏱ Duración: {_duration_label(v['duration_secs'])}",
        f"👥 Votos por persona: {v['max_choices']}",
    ]
    if v["status"] != "draft":
        lines.append(f"🗳 Personas que ya votaron: {voters}")
    if v["status"] == "active" and v["closes_at"]:
        lines.append(f"⏳ Cierra en: {humanize_seconds(max(0, v['closes_at'] - int(time.time())))}")
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
    if v["photo_file_id"]:
        rows.append([InlineKeyboardButton("🖼 Ver cómo se publica", callback_data=f"vt:pv:{vid}")])
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
        [InlineKeyboardButton("🚀 Lanzar todas", callback_data=f"vt:launchall:{gid}")],
        [InlineKeyboardButton("📊 Resultados de todas", callback_data=f"vt:resall:{gid}")],
    ]
    if show_groups_back:
        rows.append([InlineKeyboardButton("⬅️ Cambiar de grupo", callback_data="vt:groups")])
    return text, InlineKeyboardMarkup(rows)


async def _results_text(db: Database, v) -> str:
    counts = await db.get_votacion_counts(v["id"])
    voters = await db.count_votacion_voters(v["id"])
    total = sum(c for _n, c in counts)
    state = "FINAL" if v["status"] == "closed" else "PARCIAL"
    lines = [f"📊 <b>Resultados {state} — #{v['id']}</b>", f"<b>{_e(v['title'])}</b>", ""]
    top = counts[0][1] if counts else 0
    for name, votes in counts:
        pct = (votes / total * 100) if total else 0
        filled = round(pct / 10)
        crown = " 👑" if votes == top and v["status"] == "closed" else ""
        lines.append(f"{_e(name)}{crown}\n{'█' * filled}{'░' * (10 - filled)} {votes} ({pct:.0f}%)")
    if not counts:
        lines.append("Todavía nadie votó.")
    lines.append("")
    lines.append(f"🗳 Personas que votaron: {voters} · Votos totales: {total}")
    if v["status"] == "closed" and top > 0:
        winners = [n for n, c in counts if c == top]
        if len(winners) == 1:
            lines.append(f"🏆 Ganador: {_e(winners[0])}")
        else:
            lines.append("🤝 Empate entre: " + ", ".join(_e(w) for w in winners))
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
    """Se llama desde el grupo: continúa en el privado."""
    user, message = update.effective_user, update.effective_message
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
    groups = await _manageable_groups(context, user.id)
    if not groups:
        await message.reply_text("No encontré grupos donde puedas administrar votaciones.")
        return
    prefix = "new" if kind == "new" else "menu"
    rows = [[InlineKeyboardButton(t[:40], callback_data=f"vt:{prefix}:{g}")] for g, t in groups[:40]]
    await message.reply_text("Elegí el grupo:", reply_markup=InlineKeyboardMarkup(rows))


async def nueva_votacion_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _group_command(update, context, "new")


async def menu_votacion_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _group_command(update, context, "menu")


# --------------------------------------------------------------------- #
# Asistente (wizard): título -> foto (opcional) -> duración
# --------------------------------------------------------------------- #
async def _start_wizard(context, user_id: int, gid: int) -> None:
    context.user_data[_INPUT_KEY] = {"kind": "title", "gid": gid, "draft": {}}
    await context.bot.send_message(
        user_id,
        "🗳 <b>Nueva votación</b>\n\nPaso 1/3: escribí el <b>título</b> de la votación.\n"
        "Para salir escribí /cancelar.",
        parse_mode="HTML",
    )


_PHOTO_PROMPT = "Paso 2/3: mandame una <b>foto</b> para la votación (opcional), o tocá «Omitir»."
_PHOTO_KB = InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Omitir foto", callback_data="vt:skip")]])
_DUR_PROMPT = ("Paso 3/3: <b>duración</b> de la votación. Ejemplos: <code>30m</code>, <code>2h</code>, "
               "<code>1d</code>. Escribí <code>0</code> para cerrarla solo a mano.")


async def _consume_wizard(update, context, pending: dict) -> bool:
    message, user = update.effective_message, update.effective_user
    db = _db(context)
    kind, gid = pending["kind"], pending["gid"]
    text = (message.text or message.caption or "").strip()
    photo = message.photo[-1].file_id if message.photo else None

    reason = await _authorize(context, gid, user.id)
    if reason:
        context.user_data.pop(_INPUT_KEY, None)
        await message.reply_text(f"❌ {reason}")
        return True

    if kind in ("title", "edit_title"):
        if not text:
            await message.reply_text("Mandame el título como texto, por favor.")
            return True
        if len(text) > 200:
            await message.reply_text("Ese título es muy largo (máximo 200 caracteres). Probá con uno más corto.")
            return True
        if kind == "title":
            pending["draft"]["title"] = text
            pending["kind"] = "photo"
            await message.reply_text(_PHOTO_PROMPT, parse_mode="HTML", reply_markup=_PHOTO_KB)
            return True
        await db.update_votacion(pending["vid"], title=text)
        return await _finish_wizard(update, context, pending["vid"], "Título actualizado.")

    if kind in ("photo", "edit_photo"):
        if kind == "photo":
            if photo:
                pending["draft"]["photo"] = photo
            elif text.lower() in _SKIP_WORDS:
                pending["draft"]["photo"] = None
            else:
                await message.reply_text("Mandame una foto, o tocá «Omitir».", reply_markup=_PHOTO_KB)
                return True
            pending["kind"] = "dur"
            await message.reply_text(_DUR_PROMPT, parse_mode="HTML")
            return True
        if photo:
            await db.update_votacion(pending["vid"], photo_file_id=photo)
            return await _finish_wizard(update, context, pending["vid"], "Foto actualizada.")
        if text.lower() in _SKIP_WORDS | {"quitar"}:
            await db.update_votacion(pending["vid"], clear_photo=True)
            return await _finish_wizard(update, context, pending["vid"], "Foto quitada.")
        await message.reply_text("Mandame una foto, o escribí «quitar» para sacarla.")
        return True

    if kind in ("dur", "edit_dur"):
        secs = _parse_duration_secs(text) if text else None
        if secs is None:
            await message.reply_text("No entendí la duración. Usá por ejemplo 30m, 2h, 1d, o 0 para solo manual.")
            return True
        if kind == "dur":
            d = pending["draft"]
            vid = await db.create_votacion(gid, d["title"], d.get("photo"), secs, user.id)
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
    """/cancelar: corta el asistente o el voto en curso; si no había nada,
    cae al /cancelar de las confesiones (mismo comando, ya existente)."""
    popped = context.user_data.pop(_INPUT_KEY, None) or context.user_data.pop(_VOTING_KEY, None)
    context.user_data.pop(_PENDING_TEXT_KEY, None)
    if popped:
        await update.effective_message.reply_text("Listo, cancelado.")
        return
    from handlers.confessions import cancelar_confesion_command
    await cancelar_confesion_command(update, context)


# --------------------------------------------------------------------- #
# Votar (privado)
# --------------------------------------------------------------------- #
async def _eligible(context, user_id: int, group_id: int | None = None) -> list:
    """Votaciones activas en las que esta persona puede votar ahora."""
    db = _db(context)
    result = []
    for v in await db.get_active_votaciones(group_id):
        if await db.has_voted(v["id"], user_id):
            continue
        if await _is_member(context.bot, v["group_id"], user_id):
            result.append(v)
    return result


async def _choices_markup(context, votaciones: list) -> InlineKeyboardMarkup:
    db = _db(context)
    multi_group = len({v["group_id"] for v in votaciones}) > 1
    rows = []
    for v in votaciones[:40]:
        label = v["title"][:35]
        if multi_group:
            label = f"{(await db.get_group_title(v['group_id']) or '')[:15]} · {label}"
        rows.append([InlineKeyboardButton(f"🗳 {label}", callback_data=f"vt:pick:{v['id']}")])
    return InlineKeyboardMarkup(rows)


def _vote_prompt(v) -> str:
    extra = ("\nPodés votar por <b>hasta 2 opciones</b>: separalas con coma." if v["max_choices"] == 2 else "")
    return (f"🗳 <b>{_e(v['title'])}</b>\n\nEscribime tu voto: un <b>@usuario</b> o el nombre de lo que "
            f"querés votar (por ejemplo «mejor pelea»).{extra}\n"
            "No podés votar por vos mismo. Ojo: el voto no se puede cambiar. /cancelar para salir.")


async def _begin_voting(context, user_id: int, vid: int) -> None:
    """Abre el voto en el privado (el chat privado tiene el mismo id que el usuario)."""
    db = _db(context)
    v = await db.get_votacion(vid)

    async def say(text: str, **kw) -> None:
        await context.bot.send_message(user_id, text, **kw)

    if v is None or v["status"] != "active":
        await say("⚠️ Esa votación no está abierta (ya cerró o no existe).")
        return
    if not await _is_member(context.bot, v["group_id"], user_id):
        await say("⚠️ Tenés que ser miembro del grupo para votar.")
        return
    if await db.has_voted(vid, user_id):
        await say("⚠️ Ya votaste en esta votación. Solo se permite un voto por persona.")
        return
    context.user_data[_VOTING_KEY] = vid
    if v["photo_file_id"]:
        try:
            await context.bot.send_photo(user_id, v["photo_file_id"], caption=_vote_prompt(v), parse_mode="HTML")
            return
        except TelegramError as exc:
            logger.info("No pude mandar la foto de la votación #%s: %s", vid, exc)
    await say(_vote_prompt(v), parse_mode="HTML")


async def _cast(context, user, vid: int, text: str) -> tuple[str, bool]:
    """Procesa un voto escrito. Devuelve (respuesta, terminó) — si terminó
    se limpia el estado de 'votando'; si no, la persona puede reintentar."""
    db = _db(context)
    v = await db.get_votacion(vid)
    if v is None or v["status"] != "active":
        return "⚠️ Esa votación ya no está abierta. Tu voto no se registró.", True
    if await db.has_voted(vid, user.id):
        return "⚠️ Ya votaste en esta votación. Solo se permite un voto por persona.", True
    if not await _is_member(context.bot, v["group_id"], user.id):
        return "⚠️ Tenés que ser miembro del grupo para votar.", True
    options, error = _parse_options(text, v["max_choices"])
    if error:
        return _e(error), False
    own = {_norm(user.username or ""), _norm(user.full_name or "")} - {""}
    if any(key in own for key, _l in options):
        return "🚫 No podés votar por vos mismo. Mandame otro voto.", False
    if not await db.cast_ballot(vid, user.id, options):
        return "⚠️ Ya votaste en esta votación. Solo se permite un voto por persona.", True
    shown = ", ".join(_e(label) for _k, label in options)
    return f"✅ ¡Voto registrado para {shown} en «{_e(v['title'])}»!", True


async def try_consume_votacion_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Mensajes del privado: pasos del asistente o votos."""
    message, user = update.effective_message, update.effective_user
    if message is None or user is None or update.effective_chat.type != ChatType.PRIVATE:
        return False

    pending = context.user_data.get(_INPUT_KEY)
    if pending:
        return await _consume_wizard(update, context, pending)

    text = (message.text or "").strip()
    if not text:
        return False

    vid = context.user_data.get(_VOTING_KEY)
    if vid:
        reply, done = await _cast(context, user, vid, text)
        if done:
            context.user_data.pop(_VOTING_KEY, None)
        await message.reply_text(reply, parse_mode="HTML")
        return True

    # Sin estado (p. ej. tras reiniciar el bot): "@usuario" suelto se toma como voto.
    if not _FREE_VOTE_RE.match(text):
        return False
    eligible = await _eligible(context, user.id)
    if not eligible:
        return False
    if len(eligible) == 1:
        reply, _ = await _cast(context, user, eligible[0]["id"], text)
        await message.reply_text(reply, parse_mode="HTML")
        return True
    context.user_data[_PENDING_TEXT_KEY] = text
    await message.reply_text("Hay varias votaciones abiertas. Elegí en cuál querés votar:",
                             reply_markup=await _choices_markup(context, eligible))
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
        await _begin_voting(context, user.id, num)
        return True
    if kind == "votar":  # botón del cartel del grupo
        eligible = await _eligible(context, user.id, num)
        if not eligible:
            await message.reply_text("No tenés votaciones abiertas para votar en ese grupo "
                                     "(o ya votaste en todas, o no sos miembro).")
        elif len(eligible) == 1:
            await _begin_voting(context, user.id, eligible[0]["id"])
        else:
            await message.reply_text("Hay varias votaciones abiertas. Elegí una:",
                                     reply_markup=await _choices_markup(context, eligible))
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
    if await db.get_votacion(vid) is None:
        return "La votación no existe."
    if not await db.launch_votacion(vid):
        return "Solo se pueden lanzar votaciones en borrador."
    v = await db.get_votacion(vid)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🗳 Votar", url=f"https://t.me/{context.bot.username}?start=votar_{v['group_id']}")]])
    try:
        if v["photo_file_id"]:
            sent = await context.bot.send_photo(v["group_id"], v["photo_file_id"], caption=_announce_text(v),
                                                parse_mode="HTML", reply_markup=markup)
        else:
            sent = await context.bot.send_message(v["group_id"], _announce_text(v), parse_mode="HTML",
                                                  reply_markup=markup)
    except TelegramError as exc:
        await db.revert_votacion_to_draft(vid)
        return f"No pude publicarla en el grupo: {exc}"
    await db.set_votacion_group_message(vid, sent.message_id)
    return None


async def _launch_all(context, gid: int) -> tuple[int, str | None]:
    """Activa TODAS las votaciones en borrador del grupo y publica UN solo
    mensaje ("Las votaciones del grupo X han empezado") con un botón que
    lleva al privado, donde cada persona elige la votación con botones.
    Devuelve (cuántas se lanzaron, error o None)."""
    db = _db(context)
    launched = []
    for x in await db.list_votaciones(gid):
        if x["status"] == "draft" and await db.launch_votacion(x["id"]):
            launched.append(x["id"])
    if not launched:
        return 0, "No hay votaciones en borrador para lanzar."
    # Las votaciones con foto se publican primero (álbum o foto suelta, con su
    # título), y después el mensaje único con el botón «Votar».
    with_photo = [v for v in (await db.get_votacion(i) for i in launched) if v["photo_file_id"]]
    for k in range(0, len(with_photo), 10):
        chunk = with_photo[k:k + 10]
        try:
            if len(chunk) == 1:
                await context.bot.send_photo(gid, chunk[0]["photo_file_id"],
                                             caption=f"🗳 <b>{_e(chunk[0]['title'])}</b>", parse_mode="HTML")
            else:
                await context.bot.send_media_group(gid, [
                    InputMediaPhoto(v["photo_file_id"], caption=f"🗳 {v['title']}"[:1024]) for v in chunk])
        except TelegramError as exc:
            logger.warning("No pude publicar las fotos de las votaciones en %s: %s", gid, exc)
    title = await db.get_group_title(gid) or "del grupo"
    text = (f"🗳 <b>Las votaciones del grupo {_e(title)} han empezado</b>\n\n"
            "Tocá el botón, abrí mi privado y elegí en cuál querés votar.")
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(
        "🗳 Votar", url=f"https://t.me/{context.bot.username}?start=votar_{gid}")]])
    try:
        # Sin group_message_id a propósito: este cartel es compartido por
        # varias votaciones y no debe editarse cuando una de ellas cierre.
        await context.bot.send_message(gid, text, parse_mode="HTML", reply_markup=markup)
    except TelegramError as exc:
        for vid in launched:
            await db.revert_votacion_to_draft(vid)
        return 0, f"No pude publicar en el grupo: {exc}"
    return len(launched), None


async def _edit_group_message(bot, v, text: str) -> None:
    """Reemplaza el cartel del grupo (y le saca el botón). Sirve para texto o foto."""
    try:
        if v["photo_file_id"]:
            await bot.edit_message_caption(chat_id=v["group_id"], message_id=v["group_message_id"],
                                           caption=text, parse_mode="HTML")
        else:
            await bot.edit_message_text(text, chat_id=v["group_id"], message_id=v["group_message_id"],
                                        parse_mode="HTML")
    except TelegramError:
        try:
            await bot.send_message(v["group_id"], text, parse_mode="HTML")
        except TelegramError as exc:
            logger.info("No pude actualizar el cartel de la votación #%s: %s", v["id"], exc)


async def finalize_votacion(bot, db: Database, vid: int, closed_by: int | None, reason: str) -> tuple[bool, int | None]:
    """Cierra (una sola vez) y manda los resultados al propietario del grupo.
    Devuelve (se_cerró, id_de_quien_recibió_los_resultados)."""
    if not await db.close_votacion(vid, closed_by, reason):
        return False, None
    v = await db.get_votacion(vid)
    header = "⏰ Se cerró automáticamente por tiempo.\n\n" if reason == "auto" else "🔒 Votación cerrada manualmente.\n\n"
    group_title = await db.get_group_title(v["group_id"]) or str(v["group_id"])
    text = f"{header}Grupo: <b>{_e(group_title)}</b>\n\n{await _results_text(db, v)}"

    owner_id = await _group_owner_id(bot, v["group_id"])
    sent_to, tried = None, set()
    for rid in [owner_id, closed_by, v["created_by"], *settings.owner_ids]:
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

    if v["group_message_id"]:
        await _edit_group_message(
            bot, v,
            f"🔒 <b>VOTACIÓN CERRADA</b>\n\n<b>{_e(v['title'])}</b>\n\n"
            "Los resultados se enviaron al propietario del grupo.")
    return True, sent_to


async def votaciones_sweep_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cierra las votaciones vencidas (también las que vencieron con el bot apagado)."""
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

    # --- Acciones de votantes (sin permisos de administración) ---
    if action == "pick" and arg is not None:
        pending_text = context.user_data.pop(_PENDING_TEXT_KEY, None)
        if pending_text:
            reply, _ = await _cast(context, user, arg, pending_text)
            await _edit(query, reply)
        else:
            await _begin_voting(context, user.id, arg)
        await query.answer()
        return

    # --- Asistente: omitir foto ---
    if action == "skip":
        pending = context.user_data.get(_INPUT_KEY)
        if pending and pending["kind"] == "photo":
            pending["draft"]["photo"] = None
            pending["kind"] = "dur"
            await query.message.reply_text(_DUR_PROMPT, parse_mode="HTML")
            await query.answer("Sin foto.")
        else:
            await query.answer("Ese paso ya pasó.")
        return

    if action == "groups":
        groups = await _manageable_groups(context, user.id)
        rows = [[InlineKeyboardButton(t[:40], callback_data=f"vt:menu:{g}")] for g, t in groups[:40]]
        await _edit(query, "Elegí el grupo:", InlineKeyboardMarkup(rows) if rows else None)
        await query.answer()
        return
    if arg is None:
        await query.answer()
        return

    # --- Acciones de administración ---
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

    if action == "menu":
        title = await db.get_group_title(gid) or str(gid)
        show_back = len(await _manageable_groups(context, user.id)) > 1
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
    elif action == "pv":
        if v["photo_file_id"]:
            await query.message.reply_photo(v["photo_file_id"], caption=_announce_text(v), parse_mode="HTML")
        else:
            await query.answer("Esta votación no tiene foto.", show_alert=True)
            return
    elif action == "glob":
        vs = await db.list_votaciones(gid)
        if not vs:
            body = "No hay votaciones creadas todavía."
        else:
            blocks = []
            for x in vs:
                voters = await db.count_votacion_voters(x["id"])
                blocks.append(
                    f"{STATUS_ICON[x['status']]} <b>#{x['id']} {_e(x['title'])}</b>\n"
                    f"   {STATUS_NAME[x['status']]} · {_duration_label(x['duration_secs'])} · "
                    f"📷 {'sí' if x['photo_file_id'] else 'no'} · 👥 {x['max_choices']} voto(s) por persona · "
                    f"🗳 {voters} votaron"
                )
            body = "👁 <b>Vista previa global</b>\n\n" + "\n\n".join(blocks)
        await _edit(query, body[:4000],
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Menú", callback_data=f"vt:menu:{gid}")]]))
    elif action == "launch":
        err = await _launch(context, vid)
        if err:
            await query.answer(err, show_alert=True)
            return
        text, markup = await _view_content(db, vid)
        await _edit(query, "🚀 ¡Lanzada en el grupo!\n\n" + text, markup)
    elif action == "launchall":
        count, err = await _launch_all(context, gid)
        if err:
            await query.answer(err, show_alert=True)
            return
        await query.answer(f"🚀 {count} votación(es) lanzadas. Se avisó en el grupo.", show_alert=True)
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
             InlineKeyboardButton("📷 Foto", callback_data=f"vt:ep:{vid}")],
            [InlineKeyboardButton("⏱ Duración", callback_data=f"vt:ed:{vid}"),
             InlineKeyboardButton(f"👥 Votos por persona: {v['max_choices']}", callback_data=f"vt:emc:{vid}")],
            [InlineKeyboardButton("⬅️ Volver", callback_data=f"vt:view:{vid}")],
        ]
        await _edit(query, f"✏️ <b>Editar votación #{vid}</b>\n{_e(v['title'])}", InlineKeyboardMarkup(rows))
    elif action == "emc":
        if v["status"] == "closed":
            await query.answer("Una votación cerrada no se puede editar.", show_alert=True)
            return
        await db.update_votacion(vid, max_choices=2 if v["max_choices"] == 1 else 1)
        v = await db.get_votacion(vid)
        rows = [
            [InlineKeyboardButton("📝 Título", callback_data=f"vt:et:{vid}"),
             InlineKeyboardButton("📷 Foto", callback_data=f"vt:ep:{vid}")],
            [InlineKeyboardButton("⏱ Duración", callback_data=f"vt:ed:{vid}"),
             InlineKeyboardButton(f"👥 Votos por persona: {v['max_choices']}", callback_data=f"vt:emc:{vid}")],
            [InlineKeyboardButton("⬅️ Volver", callback_data=f"vt:view:{vid}")],
        ]
        await _edit(query, f"✏️ <b>Editar votación #{vid}</b>\n{_e(v['title'])}", InlineKeyboardMarkup(rows))
        await query.answer(f"Ahora cada persona vota por {v['max_choices']}.")
        return
    elif action in ("et", "ep", "ed"):
        if v["status"] == "closed":
            await query.answer("Una votación cerrada no se puede editar.", show_alert=True)
            return
        kind, prompt = {
            "et": ("edit_title", "Mandame el nuevo <b>título</b>."),
            "ep": ("edit_photo", "Mandame la nueva <b>foto</b>, o escribí «quitar» para sacarla."),
            "ed": ("edit_dur", "Mandame la nueva <b>duración</b> (30m, 2h, 1d o 0 = solo manual)."),
        }[action]
        context.user_data[_INPUT_KEY] = {"kind": kind, "gid": gid, "vid": vid, "draft": {}}
        await query.message.reply_text(prompt + "\n/cancelar para salir.", parse_mode="HTML")
    elif action == "del":
        rows = [[InlineKeyboardButton("🗑 Sí, eliminar", callback_data=f"vt:delok:{vid}"),
                 InlineKeyboardButton("Cancelar", callback_data=f"vt:view:{vid}")]]
        await _edit(query, f"¿Eliminar la votación #{vid} «{_e(v['title'])}» con todos sus votos? "
                           "No se puede deshacer.", InlineKeyboardMarkup(rows))
    elif action == "delok":
        was_active = v["status"] == "active"
        await db.delete_votacion(vid)
        if was_active and v["group_message_id"]:
            await _edit_group_message(context.bot, v, "🚫 Esta votación fue eliminada.")
        await _edit(query, f"🗑 Votación #{vid} eliminada.",
                    InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Lista", callback_data=f"vt:list:{gid}")]]))
    await query.answer()
