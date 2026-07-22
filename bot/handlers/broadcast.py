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
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import settings
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

# Carpeta donde se guardan localmente los archivos multimedia de los
# anuncios. IMPORTANTE: el `file_id` de Telegram solo es válido para el
# bot que lo generó. Como este bot anunciador tiene un token distinto al
# del bot de moderación (que es quien realmente envía el anuncio a los
# grupos), no podemos simplemente guardar el file_id en la cola: hay que
# descargar el archivo a disco (ambos procesos corren en el mismo
# servidor y comparten la carpeta `database/`) para que el bot de
# moderación lo suba él mismo con su propio token.
BROADCAST_MEDIA_DIR = settings.logs_dir.parent / "database" / "broadcast_media"
LOCAL_FILE_PREFIX = "LOCALFILE:"

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
        "menu_chat_id": None, "menu_message_id": None, "send_target": "groups",
        "selected_groups": [],
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


async def _localize_media_for_dispatch(context: ContextTypes.DEFAULT_TYPE, draft: dict) -> str:
    """Descarga el file_id (válido solo para ESTE bot anunciador) a disco y
    devuelve una referencia local que el bot de moderación pueda leer y
    subir con su propio token al momento de despachar el anuncio."""
    BROADCAST_MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    tg_file = await context.bot.get_file(draft["file_id"])
    ext = (tg_file.file_path or "").rsplit(".", 1)[-1] if tg_file.file_path and "." in tg_file.file_path else "bin"
    local_path = BROADCAST_MEDIA_DIR / f"{int(time.time())}_{uuid.uuid4().hex}.{ext}"
    await tg_file.download_to_drive(custom_path=str(local_path))
    return f"{LOCAL_FILE_PREFIX}{local_path}"


def _draft_view(draft: dict) -> tuple[str, InlineKeyboardMarkup]:
    content_type = draft["content_type"]
    media_status = "No definida" if content_type == "text" else f"Sí ({MEDIA_LABELS.get(content_type, content_type)})"
    text_status = ("Definido" if draft.get("text") else "No definido") if content_type == "text" \
        else ("Con descripción" if draft.get("text") else "Sin descripción")
    buttons_desc = describe_buttons(json_to_buttons(draft["buttons_json"]))

    text = "\n".join([
        "📢 *Compositor de anuncio*",
        "",
        "Este mensaje se puede enviar a *todos los grupos* donde esté el bot de "
        "moderación, a *grupos específicos* que elijas, o por privado a *los "
        "usuarios* que ya iniciaron chat con él \\(elige abajo\\)\\. Revisa bien "
        "antes de enviarlo\\.",
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
        [InlineKeyboardButton("📢 Enviar a todos los grupos", callback_data="b:sendask:groups")],
        [InlineKeyboardButton("🎯 Enviar a grupos específicos", callback_data="b:selgrp:0")],
        [InlineKeyboardButton("👥 Enviar a los usuarios", callback_data="b:sendask:users")],
        [InlineKeyboardButton("❌ Descartar", callback_data="b:cancel")],
    ]
    return text, InlineKeyboardMarkup(rows)


def _cancel_field_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Volver al editor", callback_data="b:back")]])


GROUPS_PER_PAGE = 8


def _group_selector_view(groups: list[tuple[int, str]], selected: list[int], page: int) -> tuple[str, InlineKeyboardMarkup]:
    total_pages = max(1, (len(groups) + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * GROUPS_PER_PAGE
    page_groups = groups[start:start + GROUPS_PER_PAGE]

    text = "\n".join([
        "🎯 *Elegí a qué grupos enviar el anuncio*",
        "",
        f"Seleccionados: *{len(selected)}* de {len(groups)}",
        f"Página {page + 1}/{total_pages}",
        "",
        "Tocá un grupo para marcarlo/desmarcarlo\\.",
    ])

    rows = []
    for group_id, title in page_groups:
        mark = "✅" if group_id in selected else "⬜"
        label = title or str(group_id)
        if len(label) > 40:
            label = label[:37] + "..."
        rows.append([InlineKeyboardButton(f"{mark} {label}", callback_data=f"b:tgrp:{group_id}:{page}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"b:selgrp:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ Siguiente", callback_data=f"b:selgrp:{page + 1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton(f"✅ Continuar ({len(selected)} seleccionado(s))", callback_data="b:selgrpdone")])
    rows.append([InlineKeyboardButton("🔙 Volver al editor", callback_data="b:back")])
    return text, InlineKeyboardMarkup(rows)


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

    action_raw = (query.data or "").split(":", 1)[1]
    action, _, action_arg = action_raw.partition(":")
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

    if action == "selgrp":
        has_content = (draft["content_type"] == "text" and draft.get("text")) or \
                      (draft["content_type"] != "text" and draft.get("file_id"))
        if not has_content:
            await query.answer("Primero define un texto o una foto/video.", show_alert=True)
            return
        groups = await db.get_known_groups()
        if not groups:
            await query.answer("El bot de moderación todavía no está en ningún grupo.", show_alert=True)
            return
        page = int(action_arg) if action_arg.isdigit() else 0
        text, markup = _group_selector_view(groups, draft["selected_groups"], page)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
        await query.answer()
        return

    if action == "tgrp":
        group_id_raw, _, page_raw = action_arg.partition(":")
        try:
            group_id = int(group_id_raw)
        except ValueError:
            await query.answer()
            return
        page = int(page_raw) if page_raw.isdigit() else 0
        if group_id in draft["selected_groups"]:
            draft["selected_groups"].remove(group_id)
        else:
            draft["selected_groups"].append(group_id)
        groups = await db.get_known_groups()
        text, markup = _group_selector_view(groups, draft["selected_groups"], page)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=markup)
        await query.answer()
        return

    if action == "selgrpdone":
        if not draft["selected_groups"]:
            await query.answer("Elegí al menos un grupo.", show_alert=True)
            return
        draft["send_target"] = "specific"
        groups = dict(await db.get_known_groups())
        names = [groups.get(gid, str(gid)) for gid in draft["selected_groups"]]
        preview = ", ".join(names[:5]) + (f" y {len(names) - 5} más" if len(names) > 5 else "")
        confirm = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Sí, enviar", callback_data="b:senddo"),
            InlineKeyboardButton("❌ Cancelar", callback_data="b:back"),
        ]])
        await query.edit_message_text(
            f"⚠️ Vas a enviar este anuncio a *{len(names)}* grupo\\(s\\): {escape_md(preview)}\\. ¿Confirmas?",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=confirm,
        )
        await query.answer()
        return

    if action == "sendask":
        has_content = (draft["content_type"] == "text" and draft.get("text")) or \
                      (draft["content_type"] != "text" and draft.get("file_id"))
        if not has_content:
            await query.answer("Primero define un texto o una foto/video.", show_alert=True)
            return
        target = action_arg if action_arg in ("groups", "users") else "groups"
        draft["send_target"] = target
        if target == "users":
            count = await db.count_dm_ok_users()
            target_desc = f"*{count}* usuario\\(s\\) \\(solo los que ya iniciaron chat con el bot de moderación\\)"
        else:
            groups = await db.get_known_groups()
            target_desc = f"*{len(groups)}* grupo\\(s\\)"
        confirm = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Sí, enviar", callback_data="b:senddo"),
            InlineKeyboardButton("❌ Cancelar", callback_data="b:back"),
        ]])
        await query.edit_message_text(
            f"⚠️ Vas a enviar este anuncio a {target_desc}\\. ¿Confirmas?",
            parse_mode=ParseMode.MARKDOWN_V2, reply_markup=confirm,
        )
        await query.answer()
        return

    if action == "senddo":
        target = draft.get("send_target", "groups")
        file_ref = draft.get("file_id")
        if draft["content_type"] != "text" and file_ref:
            try:
                file_ref = await _localize_media_for_dispatch(context, draft)
            except TelegramError as exc:
                logger.warning("No se pudo descargar el archivo del anuncio para despacho: %s", exc)
                await query.answer(
                    "No se pudo preparar el archivo multimedia. Intenta de nuevo.", show_alert=True
                )
                return
        broadcast_id = await db.create_broadcast(
            content_type=draft["content_type"], text=draft.get("text"),
            entities=draft["entities_json"], file_id=file_ref,
            buttons=draft["buttons_json"], created_by=user.id, target=target,
            target_group_ids=json.dumps(draft["selected_groups"]) if target == "specific" else "[]",
        )
        context.user_data.pop("broadcast_draft", None)
        destino = {
            "users": "a los usuarios que ya iniciaron chat con el bot",
            "specific": f"a los {len(draft['selected_groups'])} grupo(s) elegidos",
        }.get(target, "a todos los grupos")
        await query.edit_message_text(
            success(f"Anuncio #{broadcast_id} en cola. El bot de moderación lo enviará {destino} en los próximos segundos.")
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
