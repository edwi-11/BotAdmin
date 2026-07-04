"""
handlers/recurring.py
Mensajes recurrentes: permite definir uno o varios mensajes (texto, foto,
video, gif, documento, audio o nota de voz) con botones en línea opcionales,
que el bot reenvía automáticamente cada cierto intervalo (de 10 minutos a
24 horas). Cada mensaje recurrente puede configurarse para:

- Fijarse (pin) automáticamente al enviarse.
- Borrar el mensaje anterior de ese mismo recurrente antes de publicar el
  nuevo (para no acumular copias en el chat).
- Activarse / desactivarse sin perder la configuración.

Los emojis premium (entidades `custom_emoji`) se conservan tal cual si la
cuenta que creó el bot en BotFather tiene Telegram Premium: el bot no
reinterpreta el texto, simplemente reenvía las mismas entidades que llegaron
en el mensaje original usado para definir el contenido.

--------------------------------------------------------------------------
EDITOR DE UN SOLO MENÚ (sin wizard mensaje-por-mensaje)
--------------------------------------------------------------------------
Desde ⚙️ → 🔁 Mensajes recurrentes → ➕ Agregar (o ✏️ Editar sobre uno ya
creado) se abre un único panel con un botón por cada campo:

    📷 Multimedia   -> pide una foto/video/GIF/documento/audio/nota de voz
    📝 Texto        -> pide el texto del mensaje
    🔘 Botones      -> pide los botones en línea (o "no" para quitarlos)
    ⏱ Intervalo    -> elige la frecuencia con botones
    📌 Fijar        -> alterna sí/no
    🗑 Borrar anterior -> alterna sí/no
    👁 Vista previa -> envía el mensaje tal cual quedaría, sin guardarlo
    💾 Guardar      -> crea o actualiza el mensaje recurrente
    ❌ Descartar    -> cancela sin guardar

Cada botón que necesita texto libre (Multimedia/Texto/Botones) pide el
dato y, en cuanto el admin lo envía, se vuelve automáticamente al MISMO
panel (editado en el mismo mensaje), en vez de encadenar preguntas una
tras otra. El admin puede tocar "👁 Vista previa" en cualquier momento
para ver exactamente cómo se vería el mensaje antes de guardarlo.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes

from database import Database, RecurringMessage
from utils.callbacks import safe_callback
from utils.entities import (
    build_inline_keyboard,
    describe_buttons,
    entities_to_json,
    json_to_buttons,
    json_to_entities,
    parse_buttons_text,
)
from utils.formatting import error, escape_md, humanize_seconds, success
from utils.permissions import is_chat_admin

logger = logging.getLogger(__name__)

# (etiqueta, segundos) — de 10 minutos a 24 horas
INTERVAL_OPTIONS: list[tuple[str, int]] = [
    ("10 min", 600), ("20 min", 1200), ("30 min", 1800),
    ("45 min", 2700), ("1 h", 3600), ("2 h", 7200),
    ("3 h", 10800), ("4 h", 14400), ("6 h", 21600),
    ("8 h", 28800), ("12 h", 43200), ("18 h", 64800),
    ("24 h", 86400),
]

MEDIA_TYPES = ("photo", "video", "animation", "document", "audio", "voice")
MEDIA_LABELS = {
    "photo": "Foto", "video": "Video", "animation": "GIF",
    "document": "Documento", "audio": "Audio", "voice": "Nota de voz",
}

DEFAULT_INTERVAL_SECONDS = 3600


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _job_name(rec_id: int) -> str:
    return f"recurring:{rec_id}"


def schedule_recurring_job(application: Application, rec_id: int, interval_seconds: int) -> None:
    if application.job_queue is None:
        return
    for job in application.job_queue.get_jobs_by_name(_job_name(rec_id)):
        job.schedule_removal()
    application.job_queue.run_repeating(
        _recurring_job_callback,
        interval=interval_seconds,
        first=interval_seconds,
        data={"id": rec_id},
        name=_job_name(rec_id),
    )


def unschedule_recurring_job(application: Application, rec_id: int) -> None:
    if application.job_queue is None:
        return
    for job in application.job_queue.get_jobs_by_name(_job_name(rec_id)):
        job.schedule_removal()


async def load_all_recurring_jobs(application: Application, db: Database) -> None:
    """Se llama en el arranque del bot para reprogramar todos los mensajes recurrentes activos."""
    records = await db.get_all_enabled_recurring_messages()
    for rec in records:
        schedule_recurring_job(application, rec.id, rec.interval_seconds)
    if records:
        logger.info("Reprogramados %d mensajes recurrentes activos.", len(records))


# --------------------------------------------------------------------- #
# Envío real (usado tanto por el job programado como por "Vista previa")
# --------------------------------------------------------------------- #
async def _send_content(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, content_type: str,
    text: Optional[str], entities_json: str, file_id: Optional[str], buttons_json: str,
) -> Message:
    entities = json_to_entities(entities_json)
    markup = build_inline_keyboard(json_to_buttons(buttons_json))

    if content_type == "text":
        return await context.bot.send_message(chat_id, text or "", entities=entities, reply_markup=markup)

    kwargs = dict(caption=text, caption_entities=entities, reply_markup=markup)
    if content_type == "photo":
        return await context.bot.send_photo(chat_id, photo=file_id, **kwargs)
    if content_type == "video":
        return await context.bot.send_video(chat_id, video=file_id, **kwargs)
    if content_type == "animation":
        return await context.bot.send_animation(chat_id, animation=file_id, **kwargs)
    if content_type == "document":
        return await context.bot.send_document(chat_id, document=file_id, **kwargs)
    if content_type == "audio":
        return await context.bot.send_audio(chat_id, audio=file_id, **kwargs)
    if content_type == "voice":
        return await context.bot.send_voice(chat_id, voice=file_id, **kwargs)
    raise ValueError(f"Tipo de contenido desconocido: {content_type}")


async def _send_recurring(context: ContextTypes.DEFAULT_TYPE, rec_id: int) -> None:
    db: Database = context.application.bot_data["db"]
    rec = await db.get_recurring_message(rec_id)
    if rec is None or not rec.enabled:
        return

    chat_id = rec.group_id
    if rec.delete_previous and rec.last_message_id:
        try:
            await context.bot.delete_message(chat_id, rec.last_message_id)
        except TelegramError:
            pass

    try:
        sent = await _send_content(
            context, chat_id, rec.content_type, rec.text, rec.entities, rec.file_id, rec.buttons
        )
    except TelegramError as exc:
        logger.warning("No se pudo enviar el mensaje recurrente %s en %s: %s", rec_id, chat_id, exc)
        return

    await db.set_recurring_last_message(rec_id, sent.message_id)

    if rec.pin:
        try:
            await context.bot.pin_chat_message(chat_id, sent.message_id, disable_notification=True)
        except TelegramError as exc:
            logger.warning("No se pudo fijar el mensaje recurrente %s: %s", rec_id, exc)


async def _recurring_job_callback(context: ContextTypes.DEFAULT_TYPE) -> None:
    rec_id = context.job.data["id"]
    await _send_recurring(context, rec_id)


# --------------------------------------------------------------------- #
# Extracción de contenido de un mensaje (texto o media)
# --------------------------------------------------------------------- #
def _extract_content(message: Message) -> Optional[tuple[str, Optional[str], str, Optional[str]]]:
    """Devuelve (content_type, texto/caption, entities_json, file_id) o None si el
    tipo de mensaje no está soportado para un recurrente."""
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


async def recurring_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat.type not in ("group", "supergroup"):
        await update.effective_message.reply_text(error("Este comando solo funciona en grupos."))
        return
    if not await is_chat_admin(context.bot, chat.id, user.id):
        await update.effective_message.reply_text(error("No tienes permisos de administrador para usar este comando."))
        return
    db = _get_db(context)
    text, markup = await _build_list_view(db, chat.id)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)


# --------------------------------------------------------------------- #
# Borrador (draft) del editor de un solo menú
# --------------------------------------------------------------------- #
def _new_draft(group_id: int, rec_id: Optional[int] = None) -> dict:
    return {
        "id": rec_id,
        "group_id": group_id,
        "content_type": "text",
        "text": None,
        "entities_json": "[]",
        "file_id": None,
        "buttons_json": "[]",
        "interval_seconds": DEFAULT_INTERVAL_SECONDS,
        "pin": False,
        "delete_previous": False,
        "awaiting": None,       # "media" | "text" | "buttons" | None
        "menu_chat_id": None,
        "menu_message_id": None,
    }


def _draft_from_record(rec: RecurringMessage) -> dict:
    draft = _new_draft(rec.group_id, rec_id=rec.id)
    draft.update(
        content_type=rec.content_type, text=rec.text, entities_json=rec.entities,
        file_id=rec.file_id, buttons_json=rec.buttons, interval_seconds=rec.interval_seconds,
        pin=rec.pin, delete_previous=rec.delete_previous,
    )
    return draft


def _draft_view(draft: dict) -> tuple[str, InlineKeyboardMarkup]:
    group_id = draft["group_id"]
    content_type = draft["content_type"]

    if content_type == "text":
        media_status = "No definida"
    else:
        media_status = f"Sí ({MEDIA_LABELS.get(content_type, content_type)})"

    if content_type == "text":
        text_status = "Definido" if draft.get("text") else "No definido"
    else:
        text_status = "Con descripción" if draft.get("text") else "Sin descripción"

    buttons_desc = describe_buttons(json_to_buttons(draft["buttons_json"]))
    interval_label = humanize_seconds(draft["interval_seconds"])

    lines = [
        "🔁 *Editor de mensaje recurrente*",
        "",
        "Configura cada parte con los botones de abajo y usa "
        "*👁 Vista previa* para ver cómo quedará antes de guardar\\.",
        "",
        f"📷 Multimedia: *{escape_md(media_status)}*",
        f"📝 Texto: *{escape_md(text_status)}*",
        f"🔘 Botones:\n{escape_md(buttons_desc)}",
        f"⏱ Intervalo: *{escape_md(interval_label)}*",
        f"📌 Fijar mensaje: *{'Sí' if draft['pin'] else 'No'}*",
        f"🗑 Borrar el anterior: *{'Sí' if draft['delete_previous'] else 'No'}*",
    ]
    text = "\n".join(lines)

    rows = [
        [InlineKeyboardButton(f"📷 Multimedia: {media_status}", callback_data=f"r:draftmedia:{group_id}")],
        [InlineKeyboardButton(f"📝 Texto: {text_status}", callback_data=f"r:drafttext:{group_id}")],
        [InlineKeyboardButton("🔘 Botones", callback_data=f"r:draftbtns:{group_id}")],
        [InlineKeyboardButton(f"⏱ Intervalo: {interval_label}", callback_data=f"r:draftint:{group_id}")],
        [
            InlineKeyboardButton(f"📌 Fijar: {'Sí' if draft['pin'] else 'No'}", callback_data=f"r:draftpin:{group_id}"),
            InlineKeyboardButton(f"🗑 Anterior: {'Sí' if draft['delete_previous'] else 'No'}", callback_data=f"r:draftdel:{group_id}"),
        ],
        [InlineKeyboardButton("👁 Vista previa", callback_data=f"r:draftpreview:{group_id}")],
        [
            InlineKeyboardButton("💾 Guardar", callback_data=f"r:draftsave:{group_id}"),
            InlineKeyboardButton("❌ Descartar", callback_data=f"r:draftcancel:{group_id}"),
        ],
    ]
    return text, InlineKeyboardMarkup(rows)


async def _render_draft(context: ContextTypes.DEFAULT_TYPE, draft: dict) -> None:
    """Muestra (o vuelve a mostrar) el panel del editor en el mismo mensaje."""
    text, markup = _draft_view(draft)
    chat_id = draft["menu_chat_id"]
    message_id = draft["menu_message_id"]
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text,
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup,
        )
    except TelegramError as exc:
        logger.warning("No pude editar el editor de recurrente, mando uno nuevo: %s", exc)
        sent = await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
        draft["menu_chat_id"] = sent.chat_id
        draft["menu_message_id"] = sent.message_id


def _cancel_field_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al editor", callback_data=f"r:draftback:{group_id}")]])


# --------------------------------------------------------------------- #
# Callback dispatcher (pattern "^r:")
# --------------------------------------------------------------------- #
@safe_callback
async def recurring_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 3:
        await query.answer()
        return

    action = parts[1]
    db = _get_db(context)
    user = update.effective_user

    # --- Acciones sobre un mensaje recurrente ya existente (usan el id) ---
    if action in ("view", "toggle", "togpin", "togdel", "delask", "deldo", "edit"):
        rec_id = int(parts[2])
        rec = await db.get_recurring_message(rec_id)
        if rec is None:
            await query.answer("Ese mensaje recurrente ya no existe.", show_alert=True)
            return
        if not await is_chat_admin(context.bot, rec.group_id, user.id):
            await query.answer("No tienes permisos de administrador en ese grupo.", show_alert=True)
            return

        if action == "view":
            text, markup = await _build_detail_view(db, rec)
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
            await query.answer()
            return

        if action == "edit":
            draft = _draft_from_record(rec)
            draft["menu_chat_id"] = query.message.chat_id
            draft["menu_message_id"] = query.message.message_id
            context.user_data["draft_recurring"] = draft
            await _render_draft(context, draft)
            await query.answer()
            return

        if action == "toggle":
            await db.set_recurring_enabled(rec_id, not rec.enabled)
            if rec.enabled:
                unschedule_recurring_job(context.application, rec_id)
            else:
                schedule_recurring_job(context.application, rec_id, rec.interval_seconds)
            rec = await db.get_recurring_message(rec_id)
            text, markup = await _build_detail_view(db, rec)
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
            await query.answer("Actualizado.")
            return

        if action == "togpin":
            await db.set_recurring_pin(rec_id, not rec.pin)
            rec = await db.get_recurring_message(rec_id)
            text, markup = await _build_detail_view(db, rec)
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
            await query.answer()
            return

        if action == "togdel":
            await db.set_recurring_delete_previous(rec_id, not rec.delete_previous)
            rec = await db.get_recurring_message(rec_id)
            text, markup = await _build_detail_view(db, rec)
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
            await query.answer()
            return

        if action == "delask":
            confirm_markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Sí, eliminar", callback_data=f"r:deldo:{rec_id}"),
                InlineKeyboardButton("❌ Cancelar", callback_data=f"r:view:{rec_id}"),
            ]])
            await query.edit_message_text(
                f"⚠️ ¿Seguro que quieres eliminar el mensaje recurrente \\#{rec_id}\\? "
                "Esta acción no se puede deshacer\\.",
                parse_mode=ParseMode.MARKDOWN_V2, reply_markup=confirm_markup,
            )
            await query.answer()
            return

        if action == "deldo":
            unschedule_recurring_job(context.application, rec_id)
            group_id = rec.group_id
            await db.delete_recurring_message(rec_id)
            text, markup = await _build_list_view(db, group_id)
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
            await query.answer("Eliminado.")
            return

    # --- Acciones que usan group_id (lista, agregar, editor) ---
    if not parts[2].lstrip("-").isdigit():
        await query.answer()
        return
    group_id = int(parts[2])

    if not await is_chat_admin(context.bot, group_id, user.id):
        await query.answer("No tienes permisos de administrador en ese grupo.", show_alert=True)
        return

    if action == "list":
        context.user_data.pop("draft_recurring", None)
        text, markup = await _build_list_view(db, group_id)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
        await query.answer()
        return

    if action == "add":
        draft = _new_draft(group_id)
        draft["menu_chat_id"] = query.message.chat_id
        draft["menu_message_id"] = query.message.message_id
        context.user_data["draft_recurring"] = draft
        await _render_draft(context, draft)
        await query.answer()
        return

    # A partir de aquí, todas las acciones operan sobre el borrador activo.
    draft: Optional[dict] = context.user_data.get("draft_recurring")
    if not draft or draft.get("group_id") != group_id:
        await query.answer("Este editor ya expiró, ábrelo de nuevo desde el menú.", show_alert=True)
        return

    if action == "draftback":
        draft["awaiting"] = None
        await _render_draft(context, draft)
        await query.answer()
        return

    if action == "draftmedia":
        draft["awaiting"] = "media"
        await query.edit_message_text(
            "📷 Envía la foto, video, GIF, documento, audio o nota de voz "
            "\\(puedes incluirle una descripción/caption\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_cancel_field_keyboard(group_id),
        )
        await query.answer()
        return

    if action == "drafttext":
        draft["awaiting"] = "text"
        await query.edit_message_text(
            "📝 Envía el texto del mensaje \\(puedes usar el formato y los emojis "
            "premium que quieras\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_cancel_field_keyboard(group_id),
        )
        await query.answer()
        return

    if action == "draftbtns":
        draft["awaiting"] = "buttons"
        await query.edit_message_text(
            "🔘 Envía los botones en línea con este formato "
            "\\(una fila por línea, botones de la misma fila separados por ` | `\\):\n\n"
            "`Texto del botón - https://enlace.com`\n"
            "`Botón A - https://a.com | Botón B - https://b.com`\n\n"
            "O envía `no` para quitar los botones\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_cancel_field_keyboard(group_id),
        )
        await query.answer()
        return

    if action == "draftint":
        rows = []
        row = []
        for label, seconds in INTERVAL_OPTIONS:
            row.append(InlineKeyboardButton(label, callback_data=f"r:draftsetint:{group_id}:{seconds}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("🔙 Volver al editor", callback_data=f"r:draftback:{group_id}")])
        await query.edit_message_text(
            "⏱ ¿Cada cuánto quieres que se envíe este mensaje?",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        await query.answer()
        return

    if action == "draftsetint":
        draft["interval_seconds"] = int(parts[3])
        await _render_draft(context, draft)
        await query.answer("Intervalo actualizado.")
        return

    if action == "draftpin":
        draft["pin"] = not draft["pin"]
        await _render_draft(context, draft)
        await query.answer()
        return

    if action == "draftdel":
        draft["delete_previous"] = not draft["delete_previous"]
        await _render_draft(context, draft)
        await query.answer()
        return

    if action == "draftpreview":
        has_content = (draft["content_type"] == "text" and draft.get("text")) or \
                      (draft["content_type"] != "text" and draft.get("file_id"))
        if not has_content:
            await query.answer("Primero define un texto o una foto/video.", show_alert=True)
            return
        try:
            await context.bot.send_message(
                draft["menu_chat_id"], "👁 *Vista previa* — así se vería el mensaje:",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            await _send_content(
                context, draft["menu_chat_id"], draft["content_type"], draft.get("text"),
                draft["entities_json"], draft.get("file_id"), draft["buttons_json"],
            )
        except TelegramError as exc:
            await query.answer(f"No pude enviar la vista previa: {exc}", show_alert=True)
            return
        await query.answer()
        return

    if action == "draftsave":
        has_content = (draft["content_type"] == "text" and draft.get("text")) or \
                      (draft["content_type"] != "text" and draft.get("file_id"))
        if not has_content:
            await query.answer("Primero define un texto o una foto/video antes de guardar.", show_alert=True)
            return

        if draft["id"] is None:
            rec_id = await db.add_recurring_message(
                group_id=group_id,
                content_type=draft["content_type"],
                text=draft.get("text"),
                entities=draft["entities_json"],
                file_id=draft.get("file_id"),
                buttons=draft["buttons_json"],
                interval_seconds=draft["interval_seconds"],
                pin=draft["pin"],
                delete_previous=draft["delete_previous"],
                created_by=user.id,
            )
            confirmation = f"Mensaje recurrente #{rec_id} creado y activado."
        else:
            rec_id = draft["id"]
            await db.update_recurring_message(
                rec_id,
                content_type=draft["content_type"],
                text=draft.get("text"),
                entities=draft["entities_json"],
                file_id=draft.get("file_id"),
                buttons=draft["buttons_json"],
                interval_seconds=draft["interval_seconds"],
                pin=draft["pin"],
                delete_previous=draft["delete_previous"],
            )
            confirmation = f"Mensaje recurrente #{rec_id} actualizado."

        schedule_recurring_job(context.application, rec_id, draft["interval_seconds"])
        context.user_data.pop("draft_recurring", None)

        text, markup = await _build_list_view(db, group_id)
        try:
            await context.bot.edit_message_text(
                chat_id=draft["menu_chat_id"], message_id=draft["menu_message_id"],
                text=f"{success(confirmation)}\n\n{text}", parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=markup,
            )
        except TelegramError:
            await context.bot.send_message(
                draft["menu_chat_id"], text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup
            )
        await query.answer("¡Guardado!")
        return

    if action == "draftcancel":
        rec_id = draft.get("id")
        context.user_data.pop("draft_recurring", None)
        if rec_id is not None:
            rec = await db.get_recurring_message(rec_id)
            if rec is not None:
                text, markup = await _build_detail_view(db, rec)
                await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
                await query.answer("Cambios descartados.")
                return
        text, markup = await _build_list_view(db, group_id)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
        await query.answer("Descartado.")
        return

    await query.answer()


# --------------------------------------------------------------------- #
# Consumo de texto/media libre cuando el editor está esperando un campo
# --------------------------------------------------------------------- #
async def try_consume_draft_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    draft: Optional[dict] = context.user_data.get("draft_recurring")
    if not draft or not draft.get("awaiting"):
        return False

    message = update.effective_message
    awaiting = draft["awaiting"]

    if awaiting == "media":
        content = _extract_content(message)
        if content is None or content[0] == "text":
            await message.reply_text(
                error("Envía una foto, video, GIF, documento, audio o nota de voz."),
            )
            return True
        content_type, text, entities_json, file_id = content
        draft.update(content_type=content_type, text=text, entities_json=entities_json, file_id=file_id)

    elif awaiting == "text":
        if not message.text:
            await message.reply_text(error("Envía el texto del mensaje (no una foto/video)."))
            return True
        draft["text"] = message.text
        # Si ya había una foto/video configurada, el texto se usa como su
        # descripción (caption); si no, es el mensaje de texto completo.
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
                await message.reply_text(
                    error("Hay problemas con el formato de los botones:\n- " + "\n- ".join(errors))
                )
                return True
        draft["buttons_json"] = json.dumps(rows, ensure_ascii=False)

    else:
        return False

    draft["awaiting"] = None
    await _render_draft(context, draft)
    return True


# --------------------------------------------------------------------- #
# Vistas de lista / detalle
# --------------------------------------------------------------------- #
def _summary_line(index: int, rec: RecurringMessage) -> str:
    status = "🟢" if rec.enabled else "🔴"
    kind = {"text": "📝", "photo": "🖼", "video": "🎬", "animation": "🎞",
            "document": "📄", "audio": "🎵", "voice": "🎙"}.get(rec.content_type, "📦")
    return f"{status} `#{index}` {kind} cada {escape_md(humanize_seconds(rec.interval_seconds))}"


async def _build_list_view(db: Database, group_id: int) -> tuple[str, InlineKeyboardMarkup]:
    records = await db.get_recurring_messages(group_id)
    if not records:
        text = (
            "🔁 *Mensajes recurrentes*\n\n"
            "Aún no hay ninguno configurado en este grupo\\."
        )
        rows = [[InlineKeyboardButton("➕ Agregar", callback_data=f"r:add:{group_id}")]]
    else:
        lines = ["🔁 *Mensajes recurrentes*", ""]
        rows = []
        for i, rec in enumerate(records, start=1):
            lines.append(_summary_line(i, rec))
            rows.append([InlineKeyboardButton(f"⚙️ #{i}", callback_data=f"r:view:{rec.id}")])
        rows.append([InlineKeyboardButton("➕ Agregar", callback_data=f"r:add:{group_id}")])
        text = "\n".join(lines)
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"m:main:{group_id}")])
    return text, InlineKeyboardMarkup(rows)


async def _build_detail_view(db: Database, rec: RecurringMessage) -> tuple[str, InlineKeyboardMarkup]:
    buttons_desc = describe_buttons(json_to_buttons(rec.buttons))
    text = (
        f"🔁 *Mensaje recurrente \\#{rec.id}*\n\n"
        f"Estado: {'🟢 Activado' if rec.enabled else '🔴 Desactivado'}\n"
        f"Tipo: `{escape_md(rec.content_type)}`\n"
        f"Intervalo: {escape_md(humanize_seconds(rec.interval_seconds))}\n"
        f"📌 Fijar mensaje: {'Sí' if rec.pin else 'No'}\n"
        f"🗑 Borrar el anterior: {'Sí' if rec.delete_previous else 'No'}\n"
        f"🔘 Botones:\n{escape_md(buttons_desc)}"
    )
    rows = [
        [InlineKeyboardButton("✏️ Editar contenido", callback_data=f"r:edit:{rec.id}")],
        [InlineKeyboardButton(
            "⏸ Pausar" if rec.enabled else "▶️ Activar", callback_data=f"r:toggle:{rec.id}"
        )],
        [InlineKeyboardButton(
            f"📌 Fijar: {'Sí' if rec.pin else 'No'}", callback_data=f"r:togpin:{rec.id}"
        )],
        [InlineKeyboardButton(
            f"🗑 Borrar anterior: {'Sí' if rec.delete_previous else 'No'}",
            callback_data=f"r:togdel:{rec.id}",
        )],
        [InlineKeyboardButton("❌ Eliminar definitivamente", callback_data=f"r:delask:{rec.id}")],
        [InlineKeyboardButton("🔙 Volver a la lista", callback_data=f"r:list:{rec.group_id}")],
    ]
    return text, InlineKeyboardMarkup(rows)
