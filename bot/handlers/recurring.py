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

Flujo de creación (wizard), iniciado con /addrecurrente dentro de un grupo,
o desde el menú ⚙️ → 🔁 Mensajes recurrentes → ➕ Agregar:
    1) El admin envía el contenido (texto o media, con formato/emojis premium
       si quiere).
    2) El admin envía los botones (opcional) con la sintaxis:
           Texto del botón - https://enlace.com
           Botón A - https://a.com | Botón B - https://b.com
       o escribe "no" / "ninguno" para omitir.
    3) Elige el intervalo con botones (10 min ... 24 h).
    4) Elige si se fija el mensaje.
    5) Elige si se borra el mensaje anterior de ese recurrente antes de
       publicar el nuevo.
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
from utils.entities import (
    build_inline_keyboard,
    count_premium_emojis,
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

CONTENT_TYPES = ("photo", "video", "animation", "document", "audio", "voice")


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


async def _reply(update: Update, text: str) -> None:
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


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

    entities = json_to_entities(rec.entities)
    markup = build_inline_keyboard(json_to_buttons(rec.buttons))

    try:
        if rec.content_type == "text":
            sent = await context.bot.send_message(
                chat_id, rec.text or "", entities=entities, reply_markup=markup,
            )
        else:
            kwargs = dict(caption=rec.text, caption_entities=entities, reply_markup=markup)
            if rec.content_type == "photo":
                sent = await context.bot.send_photo(chat_id, photo=rec.file_id, **kwargs)
            elif rec.content_type == "video":
                sent = await context.bot.send_video(chat_id, video=rec.file_id, **kwargs)
            elif rec.content_type == "animation":
                sent = await context.bot.send_animation(chat_id, animation=rec.file_id, **kwargs)
            elif rec.content_type == "document":
                sent = await context.bot.send_document(chat_id, document=rec.file_id, **kwargs)
            elif rec.content_type == "audio":
                sent = await context.bot.send_audio(chat_id, audio=rec.file_id, **kwargs)
            elif rec.content_type == "voice":
                sent = await context.bot.send_voice(chat_id, voice=rec.file_id, **kwargs)
            else:
                logger.warning("Tipo de contenido desconocido en recurrente %s: %s", rec_id, rec.content_type)
                return
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


# --------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------- #
async def _guard_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return False
    if not await is_chat_admin(context.bot, chat.id, user.id):
        await message.reply_text(error("No tienes permisos de administrador para usar este comando."))
        return False
    return True


async def addrecurring_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    await start_wizard(update.effective_chat.id, update.effective_chat.id, context)


async def recurring_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _guard_group_admin(update, context):
        return
    db = _get_db(context)
    text, markup = await _build_list_view(db, update.effective_chat.id)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)


# --------------------------------------------------------------------- #
# Wizard: inicio y consumo de texto/media libre
# --------------------------------------------------------------------- #
async def start_wizard(group_id: int, prompt_chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["pending_recurring"] = {"step": "content", "group_id": group_id}
    await context.bot.send_message(
        prompt_chat_id,
        "🔁 *Nuevo mensaje recurrente*\n\n"
        "Envíame el contenido: puede ser texto \\(con el formato y los "
        "emojis premium que quieras\\) o una foto/video/GIF/documento/audio "
        "con su descripción\\.\n\n"
        "Escribe /cancelar para cancelar\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


def _build_buttons_prompt_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⏭ Sin botones", callback_data=f"r:nobtn:{group_id}")]])


async def try_consume_pending_recurring(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    pending: Optional[dict] = context.user_data.get("pending_recurring")
    if not pending or pending.get("step") not in ("content", "buttons"):
        return False

    message = update.effective_message
    raw_text = (message.text or message.caption or "").strip()

    if raw_text.lower() in ("/cancelar", "cancelar"):
        context.user_data.pop("pending_recurring", None)
        await message.reply_text("❌ Creación de mensaje recurrente cancelada.")
        return True

    step = pending["step"]

    if step == "content":
        content = _extract_content(message)
        if content is None:
            await message.reply_text(
                error("Envía texto, foto, video, GIF, documento, audio o nota de voz.")
            )
            return True
        content_type, text, entities_json, file_id = content
        pending.update(
            content_type=content_type, text=text, entities_json=entities_json, file_id=file_id,
        )
        pending["step"] = "buttons"

        premium_note = ""
        n = count_premium_emojis(json_to_entities(entities_json))
        if n:
            premium_note = f"\n✨ Detecté {n} emoji\\(s\\) premium; se conservarán tal cual\\."

        await message.reply_text(
            "🔘 ¿Quieres agregar botones en línea\\? Envíalos con este formato "
            "\\(una fila por línea, botones de la misma fila separados por ` | `\\):\n\n"
            "`Texto del botón - https://enlace.com`\n"
            "`Botón A - https://a.com | Botón B - https://b.com`\n\n"
            "O toca el botón de abajo si no quieres botones\\." + premium_note,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=_build_buttons_prompt_keyboard(pending["group_id"]),
        )
        return True

    if step == "buttons":
        if not message.text:
            await message.reply_text(error("Envía el texto de los botones, o toca «Sin botones»."))
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
        pending["buttons_json"] = json.dumps(rows, ensure_ascii=False)
        pending["step"] = "interval"
        await message.reply_text(
            f"✅ Botones guardados:\n{escape_md(describe_buttons(rows))}"
            if rows else "✅ Sin botones.",
            parse_mode=ParseMode.MARKDOWN_V2 if rows else None,
        )
        await _ask_interval(update.effective_chat.id, pending["group_id"], context)
        return True

    return False


async def _ask_interval(prompt_chat_id: int, group_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = []
    row: list[InlineKeyboardButton] = []
    for label, seconds in INTERVAL_OPTIONS:
        row.append(InlineKeyboardButton(label, callback_data=f"r:int:{group_id}:{seconds}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await context.bot.send_message(
        prompt_chat_id,
        "⏱ ¿Cada cuánto quieres que se envíe este mensaje?",
        reply_markup=InlineKeyboardMarkup(rows),
    )


def _yesno_keyboard(prefix: str, group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Sí", callback_data=f"r:{prefix}:{group_id}:1"),
        InlineKeyboardButton("❌ No", callback_data=f"r:{prefix}:{group_id}:0"),
    ]])


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


# --------------------------------------------------------------------- #
# Callback dispatcher (pattern "^r:")
# --------------------------------------------------------------------- #
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
    if action in ("view", "toggle", "togpin", "togdel", "delask", "deldo"):
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

    # --- Acciones que usan group_id (lista, agregar, wizard) ---
    if not parts[2].lstrip("-").isdigit():
        await query.answer()
        return
    group_id = int(parts[2])

    if not await is_chat_admin(context.bot, group_id, user.id):
        await query.answer("No tienes permisos de administrador en ese grupo.", show_alert=True)
        return

    if action == "list":
        text, markup = await _build_list_view(db, group_id)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
        await query.answer()
        return

    if action == "add":
        await query.answer()
        await query.edit_message_text(
            "🔁 Revisa tus mensajes privados con el bot \\(o este chat\\) para continuar\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await start_wizard(group_id, query.message.chat_id, context)
        return

    if action == "nobtn":
        pending = context.user_data.get("pending_recurring")
        if not pending or pending.get("group_id") != group_id:
            await query.answer()
            return
        pending["buttons_json"] = "[]"
        pending["step"] = "interval"
        await query.edit_message_text("✅ Sin botones.")
        await query.answer()
        await _ask_interval(query.message.chat_id, group_id, context)
        return

    if action == "int":
        seconds = int(parts[3])
        pending = context.user_data.get("pending_recurring")
        if not pending or pending.get("group_id") != group_id:
            await query.answer("Esta configuración ya expiró, usa /addrecurrente de nuevo.", show_alert=True)
            return
        pending["interval_seconds"] = seconds
        pending["step"] = "pin"
        await query.edit_message_text(
            f"⏱ Intervalo: cada {humanize_seconds(seconds)}.\n\n📌 ¿Quieres fijar (pin) este mensaje al enviarse?"
        )
        await query.answer()
        await context.bot.send_message(
            query.message.chat_id, "Elige una opción:", reply_markup=_yesno_keyboard("pinsel", group_id)
        )
        return

    if action == "pinsel":
        value = bool(int(parts[3]))
        pending = context.user_data.get("pending_recurring")
        if not pending or pending.get("group_id") != group_id:
            await query.answer("Esta configuración ya expiró, usa /addrecurrente de nuevo.", show_alert=True)
            return
        pending["pin"] = value
        pending["step"] = "delprev"
        await query.edit_message_text(
            f"📌 Fijar: {'Sí' if value else 'No'}.\n\n"
            "🗑 ¿Quieres que el bot borre el mensaje anterior de este recurrente antes de publicar el nuevo?"
        )
        await query.answer()
        await context.bot.send_message(
            query.message.chat_id, "Elige una opción:", reply_markup=_yesno_keyboard("delprevsel", group_id)
        )
        return

    if action == "delprevsel":
        value = bool(int(parts[3]))
        pending = context.user_data.get("pending_recurring")
        if not pending or pending.get("group_id") != group_id:
            await query.answer("Esta configuración ya expiró, usa /addrecurrente de nuevo.", show_alert=True)
            return

        rec_id = await db.add_recurring_message(
            group_id=group_id,
            content_type=pending["content_type"],
            text=pending.get("text"),
            entities=pending.get("entities_json", "[]"),
            file_id=pending.get("file_id"),
            buttons=pending.get("buttons_json", "[]"),
            interval_seconds=pending["interval_seconds"],
            pin=pending["pin"],
            delete_previous=value,
            created_by=user.id,
        )
        schedule_recurring_job(context.application, rec_id, pending["interval_seconds"])
        context.user_data.pop("pending_recurring", None)

        await query.edit_message_text(
            success(f"Mensaje recurrente #{rec_id} creado y activado.")
        )
        await query.answer("¡Listo!")
        text, markup = await _build_list_view(db, group_id)
        await context.bot.send_message(
            query.message.chat_id, text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup,
        )
        return

    await query.answer()
