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
from utils.callbacks import safe_callback
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


def build_welcome_menu(group_id: int, enabled: bool, clean: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"Estado: {_onoff(enabled)}", callback_data=f"m:wtoggle:{group_id}")],
        [InlineKeyboardButton(f"🧹 Auto-limpiar anterior: {'Sí' if clean else 'No'}", callback_data=f"m:wclean:{group_id}")],
        [InlineKeyboardButton("✏️ Editar mensaje", callback_data=f"m:wedit:{group_id}")],
        [InlineKeyboardButton("♻️ Restablecer texto", callback_data=f"m:wreset:{group_id}")],
        [InlineKeyboardButton("🔙 Volver", callback_data=f"m:main:{group_id}")],
    ]
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
    return (
        "👋 *Bienvenida*\n\n"
        f"Estado: {_onoff(s.welcome_enabled)}\n"
        f"Auto\\-limpiar anterior: {'Sí' if s.clean_welcome else 'No'}\n\n"
        f"Mensaje actual:\n{escape_md(s.welcome_text)}\n\n"
        "Marcadores disponibles: `{name}` `{mention}` `{username}` `{id}` `{group}`"
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
            reply_markup=build_welcome_menu(group_id, s.welcome_enabled, s.clean_welcome),
        )
        await query.answer()
        return

    if action == "wtoggle":
        s = await db.get_group_settings(group_id)
        await db.set_group_setting(group_id, "welcome_enabled", 0 if s.welcome_enabled else 1)
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _welcome_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_welcome_menu(group_id, s.welcome_enabled, s.clean_welcome),
        )
        await query.answer()
        return

    if action == "wclean":
        s = await db.get_group_settings(group_id)
        await db.set_group_setting(group_id, "clean_welcome", 0 if s.clean_welcome else 1)
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _welcome_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_welcome_menu(group_id, s.welcome_enabled, s.clean_welcome),
        )
        await query.answer()
        return

    if action == "wreset":
        await db.reset_group_setting(group_id, "welcome_text")
        s = await db.get_group_settings(group_id)
        await query.edit_message_text(
            await _welcome_text(db, group_id), parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=build_welcome_menu(group_id, s.welcome_enabled, s.clean_welcome),
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
async def try_consume_pending_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Devuelve True si el mensaje fue consumido como respuesta a una edición pendiente."""
    pending: Optional[dict] = context.user_data.get("pending_edit")
    if not pending:
        return False

    message = update.effective_message
    text = (message.text or message.caption or "").strip()
    if not text:
        return False

    db = _get_db(context)
    field = pending["field"]
    group_id = pending["group_id"]
    chat_id = pending["chat_id"]
    message_id = pending["message_id"]

    if text.lower() in ("/cancelar", "cancelar"):
        context.user_data.pop("pending_edit", None)
        title = await db.get_group_title(group_id) or str(group_id)
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=_main_text(title), parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=build_main_menu(group_id, is_private=(update.effective_chat.type == "private")),
            )
        except TelegramError:
            pass
        await message.reply_text("❌ Edición cancelada.")
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
                reply_markup=build_welcome_menu(group_id, s.welcome_enabled, s.clean_welcome),
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
