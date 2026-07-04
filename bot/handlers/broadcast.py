"""
handlers/broadcast.py
Menú del BOT ANUNCIADOR (proceso aparte, ver broadcast_bot.py en la raíz).

Solo el/los propietario(s) definidos en OWNER_IDS pueden usarlo. Permite
componer un anuncio (foto/texto/botones) igual que el editor de mensajes
recurrentes, con vista previa, y al confirmar lo deja anotado en la tabla
`broadcast_queue` de la MISMA base de datos que usa el bot de moderación.

Importante: este bot anunciador NO envía nada directamente a los grupos
(no necesita ser miembro de ellos). Es el bot de moderación —que ya está
presente y con permisos en todos los grupos— quien revisa esa cola cada
cierto tiempo (ver `_broadcast_dispatch_job` en el `main.py` del bot de
moderación) y hace el envío real.
"""
from __future__ import annotations

import json
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database
from utils.callbacks import safe_callback
from utils.entities import (
    build_inline_keyboard,
    describe_buttons,
    entities_to_json,
    json_to_buttons,
    json_to_entities,
    parse_buttons_text,
)
from utils.formatting import error, escape_md, success
from utils.permissions import is_owner

logger = logging.getLogger(__name__)

MEDIA_LABELS = {
    "photo": "Foto", "video": "Video", "animation": "GIF",
    "document": "Documento", "audio": "Audio", "voice": "Nota de voz",
}


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _new_draft() -> dict:
    return {
        "content_type": "text", "text": None, "entities_json": "[]",
        "file_id": None, "buttons_json": "[]", "awaiting": None,
        "menu_chat_id": None, "menu_message_id": None,
    }


def _extract_content(message: Message):
    if message.photo:
        return "photo", message.caption, entities_to_json(message.caption_entities), message.photo[-1].file_id
    if message.video:
        return "video", message.caption, entities_to_json(message.caption_entities), message.video.file_id
    if message.animation:
        return "animation", message.caption, entities_to_json(message.caption_entities), message.animation.file_id
    if message.document:
        return "document", message.caption, entities_to_json(message.caption_entities), message.document.file_id
    if message.audio:
        return "audio", message.caption, entities_to_json(message.caption_entities), message.audio.file_id
    if message.voice:
        return "voice", message.caption, entities_to_json(message.caption_entities), message.voice.file_id
    if message.text:
        return "text", message.text, entities_to_json(message.entities), None
    return None


async def _send_content(context, chat_id, content_type, text, entities_json, file_id, buttons_json) -> Message:
    entities = json_to_entities(entities_json)
    markup = build_inline_keyboard(json_to_buttons(buttons_json))
    if content_type == "text":
        return await context.bot.send_message(chat_id, text or "", entities=entities, reply_markup=markup)
    kwargs = dict(caption=text, caption_entities=entities, reply_markup=markup)
    sender = {
        "photo": context.bot.send_photo, "video": context.bot.send_video,
        "animation": context.bot.send_animation, "document": context.bot.send_document,
        "audio": context.bot.send_audio, "voice": context.bot.send_voice,
    }[content_type]
    field = {"photo": "photo", "video": "video", "animation": "animation",
              "document": "document", "audio": "audio", "voice": "voice"}[content_type]
    return await sender(chat_id, **{field: file_id}, **kwargs)


def _draft_view(draft: dict) -> tuple[str, InlineKeyboardMarkup]:
    content_type = draft["content_type"]
    media_status = "No definida" if content_type == "text" else f"Sí ({MEDIA_LABELS.get(content_type, content_type)})"
    text_status = ("Definido" if draft.get("text") else "No definido") if content_type == "text" \
        else ("Con descripción" if draft.get("text") else "Sin descripción")
    buttons_desc = describe_buttons(json_to_buttons(draft["buttons_json"]))

    text = "\n".join([
        "📢 *Compositor de anuncio*",
        "",
        "Este mensaje se enviará a *todos los grupos* donde esté el bot de "
        "moderación\\. Revisa bien antes de enviarlo\\.",
        "",
        f"📷 Multimedia: *{escape_md(media_status)}*",
        f"📝 Texto: *{escape_md(text_status)}*",
        f"🔘 Botones:\n{escape_md(buttons_desc)}",
    ])
    rows = [
        [InlineKeyboardButton(f"📷 Multimedia: {media_status}", callback_data="b:media")],
        [InlineKeyboardButton(f"📝 Texto: {text_status}", callback_data="b:text")],
        [InlineKeyboardButton("🔘 Botones", callback_data="b:buttons")],
        [InlineKeyboardButton("👁 Vista previa", callback_data="b:preview")],
        [InlineKeyboardButton("📢 Enviar a todos los grupos", callback_data="b:sendask")],
        [InlineKeyboardButton("❌ Descartar", callback_data="b:cancel")],
    ]
    return text, InlineKeyboardMarkup(rows)


def _cancel_field_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al editor", callback_data="b:back")]])


async def _render_draft(context: ContextTypes.DEFAULT_TYPE, draft: dict) -> None:
    text, markup = _draft_view(draft)
    try:
        await context.bot.edit_message_text(
            chat_id=draft["menu_chat_id"], message_id=draft["menu_message_id"],
            text=text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup,
        )
    except TelegramError:
        sent = await context.bot.send_message(draft["menu_chat_id"], text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
        draft["menu_chat_id"] = sent.chat_id
        draft["menu_message_id"] = sent.message_id


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start o /anuncio — abre el compositor. Solo para el/los propietario(s)."""
    user = update.effective_user
    if not is_owner(user.id):
        await update.effective_message.reply_text(
            error("Este bot es privado: solo el propietario puede usarlo.")
        )
        return
    draft = _new_draft()
    sent = await update.effective_message.reply_text("Cargando editor…")
    draft["menu_chat_id"] = sent.chat_id
    draft["menu_message_id"] = sent.message_id
    context.user_data["broadcast_draft"] = draft
    await _render_draft(context, draft)


@safe_callback
async def broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not is_owner(user.id):
        await query.answer("Este bot es privado.", show_alert=True)
        return

    draft = context.user_data.get("broadcast_draft")
    if not draft:
        await query.answer("El editor expiró, usa /anuncio de nuevo.", show_alert=True)
        return

    action = (query.data or "").split(":", 1)[1]
    db = _get_db(context)

    if action == "back":
        draft["awaiting"] = None
        await _render_draft(context, draft)
        await query.answer()
        return

    if action == "media":
        draft["awaiting"] = "media"
        await query.edit_message_text(
            "📷 Envía la foto/video/GIF/documento/audio/nota de voz \\(con descripción opcional\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_cancel_field_keyboard(),
        )
        await query.answer()
        return

    if action == "text":
        draft["awaiting"] = "text"
        await query.edit_message_text(
            "📝 Envía el texto del anuncio\\.", parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_cancel_field_keyboard(),
        )
        await query.answer()
        return

    if action == "buttons":
        draft["awaiting"] = "buttons"
        await query.edit_message_text(
            "🔘 Envía los botones \\(mismo formato que en el bot de moderación\\), "
            "o `no` para quitarlos:\n\n`Texto - https://enlace.com`",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=_cancel_field_keyboard(),
        )
        await query.answer()
        return

    if action == "preview":
        has_content = (draft["content_type"] == "text" and draft.get("text")) or \
                      (draft["content_type"] != "text" and draft.get("file_id"))
        if not has_content:
            await query.answer("Primero define un texto o una foto/video.", show_alert=True)
            return
        await context.bot.send_message(draft["menu_chat_id"], "👁 *Vista previa:*", parse_mode=ParseMode.MARKDOWN_V2)
        await _send_content(
            context, draft["menu_chat_id"], draft["content_type"], draft.get("text"),
            draft["entities_json"], draft.get("file_id"), draft["buttons_json"],
        )
        await query.answer()
        return

    if action == "sendask":
        has_content = (draft["content_type"] == "text" and draft.get("text")) or \
                      (draft["content_type"] != "text" and draft.get("file_id"))
        if not has_content:
            await query.answer("Primero define un texto o una foto/video.", show_alert=True)
            return
        groups = await db.get_known_groups()
        confirm = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Sí, enviar", callback_data="b:senddo"),
            InlineKeyboardButton("❌ Cancelar", callback_data="b:back"),
        ]])
        await query.edit_message_text(
            f"⚠️ Vas a enviar este anuncio a *{len(groups)}* grupo\\(s\\)\\. "
            "¿Confirmas?",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=confirm,
        )
        await query.answer()
        return

    if action == "senddo":
        broadcast_id = await db.create_broadcast(
            content_type=draft["content_type"], text=draft.get("text"),
            entities=draft["entities_json"], file_id=draft.get("file_id"),
            buttons=draft["buttons_json"], created_by=user.id,
        )
        context.user_data.pop("broadcast_draft", None)
        await query.edit_message_text(
            success(
                f"Anuncio #{broadcast_id} en cola. El bot de moderación lo enviará a todos "
                "los grupos en los próximos segundos."
            )
        )
        await query.answer("¡Encolado!")
        return

    if action == "cancel":
        context.user_data.pop("broadcast_draft", None)
        await query.edit_message_text("❌ Anuncio descartado.")
        await query.answer()
        return

    await query.answer()


async def try_consume_broadcast_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    draft = context.user_data.get("broadcast_draft")
    if not draft or not draft.get("awaiting"):
        return False
    if not is_owner(update.effective_user.id):
        return False

    message = update.effective_message
    awaiting = draft["awaiting"]

    if awaiting == "media":
        content = _extract_content(message)
        if content is None or content[0] == "text":
            await message.reply_text(error("Envía una foto, video, GIF, documento, audio o nota de voz."))
            return True
        content_type, text, entities_json, file_id = content
        draft.update(content_type=content_type, text=text, entities_json=entities_json, file_id=file_id)

    elif awaiting == "text":
        if not message.text:
            await message.reply_text(error("Envía el texto del anuncio (no una foto/video)."))
            return True
        draft["text"] = message.text
        if draft["content_type"] == "text":
            draft["entities_json"] = entities_to_json(message.entities)
        else:
            draft["entities_json"] = entities_to_json(message.caption_entities or message.entities)

    elif awaiting == "buttons":
        raw_text = (message.text or "").strip()
        if not raw_text:
            await message.reply_text(error("Envía el texto de los botones, o `no` para quitarlos."))
            return True
        if raw_text.lower() in ("no", "ninguno", "omitir", "skip"):
            rows: list[list[dict]] = []
        else:
            rows, errors = parse_buttons_text(raw_text)
            if errors:
                await message.reply_text(error("Hay problemas con el formato:\n- " + "\n- ".join(errors)))
                return True
        draft["buttons_json"] = json.dumps(rows, ensure_ascii=False)

    else:
        return False

    draft["awaiting"] = None
    await _render_draft(context, draft)
    return True
