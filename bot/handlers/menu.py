"""
handlers/menu.py
Menú de configuración con botones inline (al estilo de bots como Rose /
Xheat), accesible con /start o /menu tanto en el chat privado con el bot
como directamente dentro de un grupo.

- En un grupo: /menu muestra directamente la configuración de ese grupo
  (solo si quien lo ejecuta es administrador).
- En privado: /start o /menu muestra la lista de grupos donde el usuario
  es administrador y el bot está presente, para elegir cuál configurar.

Estructura de callback_data: "m:<accion>:<resto>", con el id del grupo
casi siempre como último segmento (ej. "m:welcome:-1001234567890").

Las secciones "Reglamento", "Bienvenida", "Despedida" y "AFK / BRB" están
totalmente funcionales. El resto de botones que aparecen en la captura de
referencia (Antispam, Anti-flood, Captcha, etc.) se muestran como
"próximamente" para mantener la misma disposición visual mientras se
implementan; tocarlos no rompe nada, solo avisa que aún no están listos.
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database
from handlers.recurring import MEDIA_LABELS, _extract_content
from utils.callbacks import safe_callback
from utils.entities import buttons_to_json, describe_buttons, json_to_buttons, parse_buttons_text
from utils.formatting import escape_md
from utils.permissions import is_chat_admin, is_owner

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Definición de secciones
# --------------------------------------------------------------------- #
# (código, etiqueta, emoji)
SOON_SECTIONS: list[tuple[str, str, str]] = [
    ("antispam", "Antispam", "📧"),
    ("antiflood", "Anti-flood", "🗣"),
    ("captcha", "Captcha", "🧠"),
    ("multimedia", "Multimedia", "📸"),
    ("nocturno", "Modo nocturno", "🌙"),
]

FIELD_LABELS = {
    "welcome_text": "mensaje de bienvenida",
    "goodbye_text": "mensaje de despedida",
    "rules_text": "reglamento",
}


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _onoff(value: bool) -> str:
    return "🟢 Activado" if value else "🔴 Desactivado"


def _media_label(settings) -> str:
    if settings.welcome_content_type != "text" and settings.welcome_file_id:
        return MEDIA_LABELS.get(settings.welcome_content_type, settings.welcome_content_type)
    return "Ninguna"


# --------------------------------------------------------------------- #
# Permisos
# --------------------------------------------------------------------- #
async def _user_can_manage(context: ContextTypes.DEFAULT_TYPE, group_id: int, user_id: int) -> bool:
    return await is_chat_admin(context.bot, group_id, user_id)


async def _admin_groups(context: ContextTypes.DEFAULT_TYPE, db: Database, user_id: int) -> list[tuple[int, str]]:
    groups = await db.get_known_groups()
    allowed: list[tuple[int, str]] = []
    owner = is_owner(user_id)
    for group_id, title in groups:
        # Un administrador normal solo ve/gestiona grupos que el propietario
        # activó explícitamente con /activar. El propietario ve todos, para
        # poder configurarlos incluso antes de activarlos.
        if not owner and not await db.is_group_activated(group_id):
            continue
        if await _user_can_manage(context, group_id, user_id):
            allowed.append((group_id, title))
    return allowed


# --------------------------------------------------------------------- #
# Construcción de teclados
# --------------------------------------------------------------------- #
def build_groups_keyboard(groups: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"👥 {title}", callback_data=f"m:main:{gid}")] for gid, title in groups]
    return InlineKeyboardMarkup(rows)


def build_main_menu(group_id: int, is_private: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("📜 Reglamento", callback_data=f"m:rules:{group_id}"),
            InlineKeyboardButton("👋 Bienvenida", callback_data=f"m:welcome:{group_id}"),
        ],
        [
            InlineKeyboardButton("🚪 Despedida", callback_data=f"m:goodbye:{group_id}"),
            InlineKeyboardButton("💤 AFK / BRB", callback_data=f"m:afk:{group_id}"),
        ],
        [
            InlineKeyboardButton("🔁 Mensajes recurrentes", callback_data=f"r:list:{group_id}"),
        ],
        [
            InlineKeyboardButton("🚫 Palabras prohibidas", callback_data=f"w:menu:{group_id}"),
            InlineKeyboardButton("🧹 Auto-eliminar", callback_data=f"c:menu:{group_id}"),
        ],
        [
            InlineKeyboardButton("❗ Advertencias", callback_data=f"aw:menu:{group_id}"),
        ],
    ]
    for i in range(0, len(SOON_SECTIONS), 2):
        pair = SOON_SECTIONS[i:i + 2]
        row = [InlineKeyboardButton(f"{emoji} {label}", callback_data=f"m:soon:{code}") for code, label, emoji in pair]
        rows.append(row)

    bottom = []
    if is_private:
        bottom.append(InlineKeyboardButton("↩️ Cambiar de grupo", callback_data="m:groups"))
    bottom.append(InlineKeyboardButton("✅ Cerrar", callback_data="m:close"))
    rows.append(bottom)
    return InlineKeyboardMarkup(rows)


def build_welcome_menu(
    group_id: int, enabled: bool, clean: bool, send_to: str = "group",
    media_label: str = "Ninguna", buttons_count: int = 0,
) -> InlineKeyboardMarkup:
    send_to_label = {"group": "Grupo", "private": "Privado", "both": "Ambos"}.get(send_to, "Grupo")
    rows = [
        [InlineKeyboardButton(f"Estado: {_onoff(enabled)}", callback_data=f"m:wtoggle:{group_id}")],
        [InlineKeyboardButton(f"📨 Enviar a: {send_to_label}", callback_data=f"m:wsendto:{group_id}")],
        [InlineKeyboardButton(f"🧹 Auto-limpiar anterior: {'Sí' if clean else 'No'}", callback_data=f"m:wclean:{group_id}")],
        [InlineKeyboardButton("✏️ Editar mensaje", callback_data=f"m:wedit:{group_id}")],
        [InlineKeyboardButton(f"🖼 Imagen/adjunto: {media_label}", callback_data=f"m:wimgedit:{group_id}")],
    ]
    if media_label != "Ninguna":
        rows.append([InlineKeyboardButton("🗑 Quitar imagen", callback_data=f"m:wimgclear:{group_id}")])
    rows.append([InlineKeyboardButton(f"🔘 Botones: {buttons_count}", callback_data=f"m:wbtnsedit:{group_id}")])
    rows.append([InlineKeyboardButton("♻️ Restablecer texto", callback_data=f"m:wreset:{group_id}")])
    rows.append([InlineKeyboardButton("🔙 Volver", callback_data=f"m:main:{group_id}")])
    return InlineKeyboardMarkup(rows)


def build_goodbye_menu(group_id: int, enabled: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"Estado: {_onoff(enabled)}", callback_data=f"m:gtoggle:{group_id}")],
        [InlineKeyboardButton("✏️ Editar mensaje", callback_data=f"m:gedit:{group_id}")],
        [InlineKeyboardButton("♻️ Restablecer texto", callback_data=f"m:greset:{group_id}")],
        [InlineKeyboardButton("🔙 Volver", callback_data=f"m:main:{group_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def build_rules_menu(group_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ Editar reglamento", callback_data=f"m:redit:{group_id}")],
        [InlineKeyboardButton("♻️ Restablecer", callback_data=f"m:rreset:{group_id}")],
        [InlineKeyboardButton("🔙 Volver", callback_data=f"m:main:{group_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def build_afk_menu(group_id: int, enabled: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"Estado: {_onoff(enabled)}", callback_data=f"m:atoggle:{group_id}")],
        [InlineKeyboardButton("🔙 Volver", callback_data=f"m:main:{group_id}")],
    ]
    return InlineKeyboardMarkup(rows)


def build_cancel_edit_keyboard(group_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancelar", callback_data=f"m:canceledit:{group_id}")]])


# --------------------------------------------------------------------- #
# Textos de cada sección
# --------------------------------------------------------------------- #
def _main_text(title: str) -> str:
    return (
        "⚙️ *CONFIGURACIÓN*\n"
        f"Grupo: _{escape_md(title)}_\n\n"
        "Elige cuál de los ajustes quieres editar\\."
    )


async def _welcome_text(db: Database, group_id: int) -> str:
    s = await db.get_group_settings(group_id)
    send_to_label = {"group": "Grupo", "private": "Privado", "both": "Ambos"}.get(s.welcome_send_to, "Grupo")
    media_label = _media_label(s)
    buttons_count = len(json_to_buttons(s.welcome_buttons))
    return (
        "👋 *Bienvenida*\n\n"
        f"Estado: {_onoff(s.welcome_enabled)}\n"
        f"Enviar a: {escape_md(send_to_label)}\n"
        f"Auto\\-limpiar anterior: {'Sí' if s.clean_welcome else 'No'}\n"
        f"Imagen/adjunto: {escape_md(media_label)}\n"
        f"Botones configurados: {buttons_count}\n\n"
        f"Mensaje actual:\n{escape_md(s.welcome_text)}\n\n"
        "Marcadores disponibles: `{name}` `{mention}` `{username}` `{id}` `{group}`\n\n"
        "ℹ️ Si el envío es *Privado* o *Ambos*: si la persona nunca inició "
        "un chat conmigo, Telegram no me deja escribirle primero — en ese "
        "caso le aviso en el grupo con un botón para que inicie el chat\\."
    )


async def _goodbye_text(db: Database, group_id: int) -> str:
    s = await db.get_group_settings(group_id)
    return (
        "🚪 *Despedida*\n\n"
        f"Estado: {_onoff(s.goodbye_enabled)}\n\n"
        f"Mensaje actual:\n{escape_md(s.goodbye_text)}\n\n"
        "Marcadores disponibles: `{name}` `{mention}` `{username}` `{id}` `{group}`"
    )


async def _rules_text_view(db: Database, group_id: int) -> str:
    s = await db.get_group_settings(group_id)
    return f"📜 *Reglamento actual*\n\n{escape_md(s.rules_text)}"


async def _afk_text(db: Database, group_id: int) -> str:
    s = await db.get_group_settings(group_id)
    return (
        "💤 *AFK / BRB*\n\n"
        f"Estado: {_onoff(s.afk_enabled)}\n\n"
        "Cualquier miembro puede activar su estado ausente escribiendo "
        "*brb* al inicio de un mensaje \\(ya no hace falta la barra `/`\\), "
        "opcionalmente seguido del motivo, ej: `brb almorzando`\\.\n"
        "También sigue funcionando `/brb \\[motivo\\]` por compatibilidad\\."
    )


# --------------------------------------------------------------------- #
# Entrada: /start y /menu
# --------------------------------------------------------------------- #
async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)

    if chat.type == "private":
        groups = await _admin_groups(context, db, user.id)
        if not groups:
            await message.reply_text(
                "No encontré ningún grupo donde seas administrador y el bot esté presente\\.\n"
                "Añade el bot a tu grupo, hazlo administrador, y luego escribe /menu o /start "
                "de nuevo aquí\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        if len(groups) == 1:
            group_id, title = groups[0]
            await message.reply_text(
                _main_text(title), parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=build_main_menu(group_id, is_private=True),
            )
            return
        await message.reply_text(
            "Elige el grupo que quieres configurar:",
            reply_markup=build_groups_keyboard(groups),
        )
        return

    # Grupo / supergrupo
    if not await _user_can_manage(context, chat.id, user.id):
        await message.reply_text("❌ No tienes permisos de administrador para usar este comando.")
        return
    await db.upsert_group(chat.id, chat.title)
    await message.reply_text(
        _main_text(chat.title), parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=build_main_menu(chat.id, is_private=False),
    )


# --------------------------------------------------------------------- #
# Callback query dispatcher
# --------------------------------------------------------------------- #
@safe_callback
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data or ""
    parts = data.split(":")
    if len(parts) < 2:
        await query.answer()
        return

    action = parts[1]
    db = _get_db(context)
    user = update.effective_user
    is_private = query.message.chat.type == "private"

    # Acciones que no requieren un group_id numérico
    if action == "groups":
        groups = await _admin_groups(context, db, user.id)
        if not groups:
            await query.answer("Ya no tienes grupos disponibles.", show_alert=True)
            return
        await query.edit_message_text(
            "Elige el grupo que quieres configurar:", reply_markup=build_groups_keyboard(groups)
        )
        await query.answer()
        return

    if action == "close":
        try:
            await query.edit_message_text("✅ Menú cerrado.")
        except TelegramError:
            pass
        await query.answer()
        return

    if action == "soon":
        code = parts[2] if len(parts) > 2 else ""
        label = next((lbl for c, lbl, _ in SOON_SECTIONS if c == code), "Esta función")
        await query.answer(f"🚧 {label} estará disponible próximamente.", show_alert=True)
        return

    if len(parts) < 3 or not parts[2].lstrip("-").isdigit():
        await query.answer()
        return
    group_id = int(parts[2])

    if not await _user_can_manage(context, group_id, user.id):
        await query.answer("No tienes permisos de administrador en ese grupo.", show_alert=True)
        return

    title = await db.get_group_title(group_id) or str(group_id)

    if action == "main":
        await query.edit_message_text(
            _main_text(title), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_main_menu(group_id, is_private=is_private),
        )
        await query.answer()
        return

    if action == "welcome":
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _welcome_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_welcome_menu(
                group_id, s.welcome_enabled, s.clean_welcome, s.welcome_send_to,
                _media_label(s), len(json_to_buttons(s.welcome_buttons)),
            ),
        )
        await query.answer()
        return

    if action == "wtoggle":
        s = await db.get_group_settings(group_id)
        await db.set_group_setting(group_id, "welcome_enabled", 0 if s.welcome_enabled else 1)
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _welcome_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_welcome_menu(
                group_id, s.welcome_enabled, s.clean_welcome, s.welcome_send_to,
                _media_label(s), len(json_to_buttons(s.welcome_buttons)),
            ),
        )
        await query.answer()
        return

    if action == "wclean":
        s = await db.get_group_settings(group_id)
        await db.set_group_setting(group_id, "clean_welcome", 0 if s.clean_welcome else 1)
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _welcome_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_welcome_menu(
                group_id, s.welcome_enabled, s.clean_welcome, s.welcome_send_to,
                _media_label(s), len(json_to_buttons(s.welcome_buttons)),
            ),
        )
        await query.answer()
        return

    if action == "wsendto":
        s = await db.get_group_settings(group_id)
        next_value = {"group": "private", "private": "both", "both": "group"}.get(s.welcome_send_to, "group")
        await db.set_group_setting(group_id, "welcome_send_to", next_value)
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _welcome_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_welcome_menu(
                group_id, s.welcome_enabled, s.clean_welcome, s.welcome_send_to,
                _media_label(s), len(json_to_buttons(s.welcome_buttons)),
            ),
        )
        await query.answer()
        return

    if action == "wreset":
        await db.reset_group_setting(group_id, "welcome_text")
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _welcome_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_welcome_menu(
                group_id, s.welcome_enabled, s.clean_welcome, s.welcome_send_to,
                _media_label(s), len(json_to_buttons(s.welcome_buttons)),
            ),
        )
        await query.answer("Restablecido.")
        return

    if action == "wedit":
        context.user_data["pending_edit"] = {
            "field": "welcome_text", "group_id": group_id,
            "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        await query.edit_message_text(
            "✏️ Envía el nuevo *mensaje de bienvenida*\\. Puedes usar `{name}` `{mention}` "
            "`{username}` `{id}` `{group}`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_cancel_edit_keyboard(group_id),
        )
        await query.answer()
        return

    if action == "wimgedit":
        context.user_data["pending_edit"] = {
            "field": "welcome_media", "group_id": group_id,
            "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        await query.edit_message_text(
            "🖼 Envía la *foto, video, GIF o documento* que quieras usar como imagen de "
            "bienvenida\\. Escribe `quitar` para eliminar la actual\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_cancel_edit_keyboard(group_id),
        )
        await query.answer()
        return

    if action == "wimgclear":
        await db.set_group_setting(group_id, "welcome_content_type", "text")
        await db.set_group_setting(group_id, "welcome_file_id", None)
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _welcome_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_welcome_menu(
                group_id, s.welcome_enabled, s.clean_welcome, s.welcome_send_to,
                _media_label(s), len(json_to_buttons(s.welcome_buttons)),
            ),
        )
        await query.answer("Imagen eliminada.")
        return

    if action == "wbtnsedit":
        context.user_data["pending_edit"] = {
            "field": "welcome_buttons", "group_id": group_id,
            "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        await query.edit_message_text(
            "🔘 Envía los botones de la bienvenida, uno por línea, con el formato "
            "`Texto - URL`\\. Separa varios botones en una misma fila con ` | `\\.\n"
            "Ejemplo:\n`📢 Canal - https://t\\.me/canal | 📜 Reglas - https://t\\.me/reglas`\n\n"
            "Escribe `quitar` para eliminar los botones actuales\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_cancel_edit_keyboard(group_id),
        )
        await query.answer()
        return

    if action == "goodbye":
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _goodbye_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_goodbye_menu(group_id, s.goodbye_enabled),
        )
        await query.answer()
        return

    if action == "gtoggle":
        s = await db.get_group_settings(group_id)
        await db.set_group_setting(group_id, "goodbye_enabled", 0 if s.goodbye_enabled else 1)
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _goodbye_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_goodbye_menu(group_id, s.goodbye_enabled),
        )
        await query.answer()
        return

    if action == "greset":
        await db.reset_group_setting(group_id, "goodbye_text")
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _goodbye_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_goodbye_menu(group_id, s.goodbye_enabled),
        )
        await query.answer("Restablecido.")
        return

    if action == "gedit":
        context.user_data["pending_edit"] = {
            "field": "goodbye_text", "group_id": group_id,
            "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        await query.edit_message_text(
            "✏️ Envía el nuevo *mensaje de despedida*\\. Puedes usar `{name}` `{mention}` "
            "`{username}` `{id}` `{group}`\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_cancel_edit_keyboard(group_id),
        )
        await query.answer()
        return

    if action == "rules":
        await query.edit_message_text(
            await _rules_text_view(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_rules_menu(group_id),
        )
        await query.answer()
        return

    if action == "rreset":
        await db.reset_group_setting(group_id, "rules_text")
        await query.edit_message_text(
            await _rules_text_view(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_rules_menu(group_id),
        )
        await query.answer("Restablecido.")
        return

    if action == "redit":
        context.user_data["pending_edit"] = {
            "field": "rules_text", "group_id": group_id,
            "chat_id": query.message.chat_id, "message_id": query.message.message_id,
        }
        await query.edit_message_text(
            "✏️ Envía el nuevo *reglamento*\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_cancel_edit_keyboard(group_id),
        )
        await query.answer()
        return

    if action == "afk":
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _afk_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_afk_menu(group_id, s.afk_enabled),
        )
        await query.answer()
        return

    if action == "atoggle":
        s = await db.get_group_settings(group_id)
        await db.set_group_setting(group_id, "afk_enabled", 0 if s.afk_enabled else 1)
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _afk_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_afk_menu(group_id, s.afk_enabled),
        )
        await query.answer()
        return

    if action == "canceledit":
        context.user_data.pop("pending_edit", None)
        await query.edit_message_text(
            _main_text(title), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_main_menu(group_id, is_private=is_private),
        )
        await query.answer("Cancelado.")
        return

    await query.answer()


# --------------------------------------------------------------------- #
# Consumo de texto libre cuando hay una edición pendiente (wedit/gedit/redit)
# --------------------------------------------------------------------- #
async def _refresh_welcome_menu(context: ContextTypes.DEFAULT_TYPE, db: Database, group_id: int, chat_id: int, message_id: int) -> None:
    s = await db.get_group_settings(group_id)
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=await _welcome_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_welcome_menu(
                group_id, s.welcome_enabled, s.clean_welcome, s.welcome_send_to,
                _media_label(s), len(json_to_buttons(s.welcome_buttons)),
            ),
        )
    except TelegramError as exc:
        logger.warning("No pude refrescar el menú de bienvenida: %s", exc)


async def _cancel_pending_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> None:
    db = _get_db(context)
    group_id = pending["group_id"]
    context.user_data.pop("pending_edit", None)
    title = await db.get_group_title(group_id) or str(group_id)
    try:
        await context.bot.edit_message_text(
            chat_id=pending["chat_id"], message_id=pending["message_id"],
            text=_main_text(title), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_main_menu(group_id, is_private=(update.effective_chat.type == "private")),
        )
    except TelegramError:
        pass
    await update.effective_message.reply_text("❌ Edición cancelada.")


async def _consume_welcome_media(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> bool:
    """Reutiliza _extract_content() de handlers/recurring.py para reconocer
    foto/video/gif/documento, igual que hacen los mensajes recurrentes."""
    message = update.effective_message
    db = _get_db(context)
    group_id = pending["group_id"]

    plain = (message.text or "").strip().lower()
    if plain in ("/cancelar", "cancelar"):
        await _cancel_pending_edit(update, context, pending)
        return True

    if plain in ("quitar", "borrar", "no", "off"):
        await db.set_group_setting(group_id, "welcome_content_type", "text")
        await db.set_group_setting(group_id, "welcome_file_id", None)
        context.user_data.pop("pending_edit", None)
        await _refresh_welcome_menu(context, db, group_id, pending["chat_id"], pending["message_id"])
        await message.reply_text("✅ Imagen de bienvenida eliminada.")
        return True

    content = _extract_content(message)
    if content is None or content[0] == "text" or content[3] is None:
        await message.reply_text(
            "❌ Envía una foto, video, GIF o documento (o escribe «quitar» para eliminar la imagen actual)."
        )
        return True

    content_type, _caption, _entities, file_id = content
    await db.set_group_setting(group_id, "welcome_content_type", content_type)
    await db.set_group_setting(group_id, "welcome_file_id", file_id)
    context.user_data.pop("pending_edit", None)
    await _refresh_welcome_menu(context, db, group_id, pending["chat_id"], pending["message_id"])
    await message.reply_text(f"✅ Imagen de bienvenida actualizada ({MEDIA_LABELS.get(content_type, content_type)}).")
    return True


async def _consume_welcome_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE, pending: dict) -> bool:
    """Reutiliza parse_buttons_text()/buttons_to_json() de utils/entities.py,
    la misma sintaxis "Texto - URL" que ya usan los mensajes recurrentes."""
    message = update.effective_message
    db = _get_db(context)
    group_id = pending["group_id"]
    text = (message.text or message.caption or "").strip()
    if not text:
        return False

    if text.lower() in ("/cancelar", "cancelar"):
        await _cancel_pending_edit(update, context, pending)
        return True

    if text.lower() in ("quitar", "borrar", "no", "off"):
        await db.set_group_setting(group_id, "welcome_buttons", "[]")
        context.user_data.pop("pending_edit", None)
        await _refresh_welcome_menu(context, db, group_id, pending["chat_id"], pending["message_id"])
        await message.reply_text("✅ Botones de bienvenida eliminados.")
        return True

    rows, errors = parse_buttons_text(text)
    if errors:
        await message.reply_text("❌ Hay errores en los botones:\n" + "\n".join(errors))
        return True
    if not rows:
        await message.reply_text(
            "❌ No se detectó ningún botón válido. Ejemplo:\n"
            "📢 Canal - https://t.me/canal | 📜 Reglas - https://t.me/reglas"
        )
        return True

    await db.set_group_setting(group_id, "welcome_buttons", buttons_to_json(rows))
    context.user_data.pop("pending_edit", None)
    await _refresh_welcome_menu(context, db, group_id, pending["chat_id"], pending["message_id"])
    await message.reply_text("✅ Botones de bienvenida actualizados:\n\n" + describe_buttons(rows))
    return True


async def try_consume_pending_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Devuelve True si el mensaje fue consumido como respuesta a una edición pendiente."""
    pending: Optional[dict] = context.user_data.get("pending_edit")
    if not pending:
        return False

    field = pending["field"]

    # La imagen/adjunto de bienvenida necesita leer media (foto/video/etc.),
    # y los botones necesitan parsear la sintaxis "Texto - URL" antes de
    # guardar, así que ambos casos se resuelven aparte del resto de campos
    # de texto simple (welcome_text / goodbye_text / rules_text).
    if field == "welcome_media":
        return await _consume_welcome_media(update, context, pending)
    if field == "welcome_buttons":
        return await _consume_welcome_buttons(update, context, pending)

    message = update.effective_message
    text = (message.text or message.caption or "").strip()
    if not text:
        return False

    db = _get_db(context)
    group_id = pending["group_id"]
    chat_id = pending["chat_id"]
    message_id = pending["message_id"]

    if text.lower() in ("/cancelar", "cancelar"):
        await _cancel_pending_edit(update, context, pending)
        return True

    await db.set_group_setting(group_id, field, text)
    context.user_data.pop("pending_edit", None)

    label = FIELD_LABELS.get(field, field)
    is_private = update.effective_chat.type == "private"
    try:
        if field == "welcome_text":
            s = await db.get_group_settings(group_id)
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=await _welcome_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=build_welcome_menu(
                    group_id, s.welcome_enabled, s.clean_welcome, s.welcome_send_to,
                    _media_label(s), len(json_to_buttons(s.welcome_buttons)),
                ),
            )
        elif field == "goodbye_text":
            s = await db.get_group_settings(group_id)
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=await _goodbye_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=build_goodbye_menu(group_id, s.goodbye_enabled),
            )
        elif field == "rules_text":
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=await _rules_text_view(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=build_rules_menu(group_id),
            )
    except TelegramError as exc:
        logger.warning("No pude actualizar el menú tras editar %s: %s", field, exc)

    await message.reply_text(f"✅ Se actualizó el {label}.")
    return True
