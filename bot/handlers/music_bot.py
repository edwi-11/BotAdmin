"""
handlers/music_bot.py
Handlers de CEO Music (bot independiente, ver Music.py).

Comandos: /start /help /buscar /tendencias /audius /jamendo
En chat privado también se puede escribir directamente el nombre de la canción.
"""
from __future__ import annotations

import html
import io
import logging
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from math import ceil

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyParameters, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from providers.base import ProviderError, Track
from services.music_service import MusicService

logger = logging.getLogger(__name__)

PAGE_SIZE = 5
MAX_SESSIONS = 300          # Listas de resultados que se recuerdan en memoria
SEARCH_COOLDOWN = 2.0       # Segundos mínimos entre búsquedas de un mismo usuario
MAX_QUERY_LEN = 100
CALLBACK_PREFIX = "mu"

esc = html.escape


@dataclass
class SearchSession:
    owner_id: int
    title: str
    tracks: list[Track]


# ----------------------------------------------------------------- utilidades
def _service(context: ContextTypes.DEFAULT_TYPE) -> MusicService:
    return context.application.bot_data["music_service"]


def _sessions(context: ContextTypes.DEFAULT_TYPE) -> "OrderedDict[str, SearchSession]":
    return context.application.bot_data.setdefault("music_sessions", OrderedDict())


def _store_session(context: ContextTypes.DEFAULT_TYPE, session: SearchSession) -> str:
    sessions = _sessions(context)
    token = secrets.token_hex(4)
    sessions[token] = session
    while len(sessions) > MAX_SESSIONS:
        sessions.popitem(last=False)
    return token


def _on_cooldown(context: ContextTypes.DEFAULT_TYPE) -> bool:
    now = time.monotonic()
    if now - context.user_data.get("music_last_search", 0.0) < SEARCH_COOLDOWN:
        return True
    context.user_data["music_last_search"] = now
    return False


def _render(service: MusicService, token: str, session: SearchSession, page: int) -> tuple[str, InlineKeyboardMarkup]:
    total_pages = max(1, ceil(len(session.tracks) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    chunk = session.tracks[start:start + PAGE_SIZE]

    lines = [f"🎵 <b>{esc(session.title)}</b>", ""]
    play_row: list[InlineKeyboardButton] = []
    for offset, track in enumerate(chunk):
        index = start + offset
        duration = f" · {track.duration_text}" if track.duration_text else ""
        lines.append(
            f"<b>{index + 1}.</b> {esc(track.title)} — {esc(track.artist)}{duration} "
            f"<i>({esc(service.label_of(track.provider))})</i>"
        )
        play_row.append(InlineKeyboardButton(f"▶ {index + 1}", callback_data=f"{CALLBACK_PREFIX}:p:{token}:{index}"))
    lines += ["", f"Página {page + 1}/{total_pages} · Toca ▶ para recibir la canción."]

    rows = [play_row]
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Anterior", callback_data=f"{CALLBACK_PREFIX}:g:{token}:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Siguiente ▶️", callback_data=f"{CALLBACK_PREFIX}:g:{token}:{page + 1}"))
    if nav:
        rows.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _help_text(service: MusicService) -> str:
    sources = ", ".join(p.label for p in service.providers.values())
    lines = [
        "🎧 <b>CEO Music</b>",
        f"Busco música en: {esc(sources)}.",
        "",
        "<code>/buscar</code> <i>canción o artista</i> — buscar en todas las fuentes",
        "<code>/tendencias</code> — lo más escuchado de la semana",
    ]
    for provider in service.providers.values():
        lines.append(f"<code>/{provider.name}</code> <i>texto</i> — buscar solo en {esc(provider.label)}")
    lines += ["", "En chat privado también puedes escribir directamente el nombre de la canción."]
    return "\n".join(lines)


def _safe_filename(track: Track) -> str:
    base = re.sub(r'[\\/:*?"<>|\r\n\t]+', "", f"{track.artist} - {track.title}").strip()[:100]
    return f"{base or 'cancion'}.mp3"


def _caption(service: MusicService, track: Track) -> str:
    label = esc(service.label_of(track.provider))
    source = f'<a href="{esc(track.page_url, quote=True)}">{label}</a>' if track.page_url else label
    lines = [f"🎧 <b>{esc(track.title)}</b>", f"👤 {esc(track.artist)}", f"📻 Vía {source}"]
    if track.license_url:
        lines.append(f'📄 <a href="{esc(track.license_url, quote=True)}">Licencia</a>')
    return "\n".join(lines)


# ------------------------------------------------------------------- comandos
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(_help_text(_service(context)))


async def _run_search(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    query: str | None,
    provider: str | None = None,
) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None:
        return
    service = _service(context)

    if query is not None:
        query = " ".join(query.split())[:MAX_QUERY_LEN]
        if not query:
            await message.reply_text("Escribe qué buscar. Ejemplo: <code>/buscar Daft Punk</code>")
            return
    if _on_cooldown(context):
        await message.reply_text("⏳ Un momento, espera un par de segundos entre búsquedas.")
        return

    working = await message.reply_text("🔎 Buscando…")
    try:
        if query is None:
            tracks = await service.trending(provider)
            title = "Tendencias de la semana"
        else:
            tracks = await service.search(query, provider)
            title = f"Resultados para «{query}»"
    except ProviderError as exc:
        await working.edit_text(f"⚠️ {esc(str(exc))}")
        return
    except Exception:
        logger.exception("Fallo inesperado al buscar música")
        await working.edit_text("⚠️ Ocurrió un error inesperado al buscar. Intenta de nuevo.")
        return

    if not tracks:
        shown = f" para «{esc(query)}»" if query else ""
        await working.edit_text(f"😕 No encontré resultados{shown}.")
        return

    session = SearchSession(owner_id=user.id, title=title, tracks=tracks)
    token = _store_session(context, session)
    text, keyboard = _render(service, token, session, 0)
    await working.edit_text(text, reply_markup=keyboard)


async def buscar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_search(update, context, query=" ".join(context.args or []))


async def tendencias_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _run_search(update, context, query=None)


def _provider_command(provider_name: str):
    async def command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = " ".join(context.args or [])
        if not text.strip():
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"Escribe qué buscar. Ejemplo: <code>/{provider_name} nombre de la canción</code>"
                )
            return
        await _run_search(update, context, query=text, provider=provider_name)

    return command


async def text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Chat privado: cualquier texto que no sea comando se busca como canción."""
    if update.effective_message and update.effective_message.text:
        await _run_search(update, context, query=update.effective_message.text)


# ------------------------------------------------------------------- botones
async def music_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    parts = (query.data or "").split(":")
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX or not parts[3].isdigit():
        await query.answer()
        return
    _, action, token, raw_number = parts
    number = int(raw_number)

    session = _sessions(context).get(token)
    if session is None:
        await query.answer("Esta lista ya expiró. Haz una búsqueda nueva.", show_alert=True)
        return
    if query.from_user.id != session.owner_id:
        await query.answer("Esta lista es de otra persona. Haz tu propia búsqueda 😉", show_alert=True)
        return

    service = _service(context)
    if action == "g":
        text, keyboard = _render(service, token, session, number)
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise
        await query.answer()
    elif action == "p" and number < len(session.tracks):
        await _play(update, context, session.tracks[number])
    else:
        await query.answer()


async def _play(update: Update, context: ContextTypes.DEFAULT_TYPE, track: Track) -> None:
    query = update.callback_query
    message = query.message if query else None
    if query is None or message is None:
        return
    if context.user_data.get("music_busy"):
        await query.answer("⏳ Aún estoy enviando tu canción anterior.")
        return

    context.user_data["music_busy"] = True
    service = _service(context)
    chat_id = message.chat.id
    try:
        await query.answer("⏳ Preparando la canción…")
        await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VOICE)
        try:
            audio = await service.download(track)
        except ProviderError as exc:
            await context.bot.send_message(chat_id, f"⚠️ {esc(str(exc))}")
            return
        await context.bot.send_audio(
            chat_id,
            audio=io.BytesIO(audio),
            filename=_safe_filename(track),
            title=track.title[:64],
            performer=track.artist[:64],
            duration=track.duration or None,
            caption=_caption(service, track),
            reply_parameters=ReplyParameters(message_id=message.message_id, allow_sending_without_reply=True),
            read_timeout=120,
            write_timeout=120,
            connect_timeout=30,
            pool_timeout=30,
        )
    except TelegramError:
        logger.exception("[%s] No se pudo enviar el audio %s", track.provider, track.track_id)
        await context.bot.send_message(chat_id, "⚠️ No pude enviar el audio. Intenta con otra canción.")
    except Exception:
        logger.exception("[%s] Fallo inesperado al enviar %s", track.provider, track.track_id)
        await context.bot.send_message(chat_id, "⚠️ Ocurrió un error inesperado. Intenta de nuevo.")
    finally:
        context.user_data["music_busy"] = False


# ------------------------------------------------------------------ registro
def register_music_handlers(application: Application, provider_names: list[str]) -> None:
    application.add_handler(CommandHandler(["start", "help", "ayuda"], start_command))
    application.add_handler(CommandHandler(["buscar", "musica"], buscar_command))
    application.add_handler(CommandHandler("tendencias", tendencias_command))
    for name in provider_names:
        application.add_handler(CommandHandler(name, _provider_command(name)))
    application.add_handler(CallbackQueryHandler(music_callback, pattern=rf"^{CALLBACK_PREFIX}:"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_search))
