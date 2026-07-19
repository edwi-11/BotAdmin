"""
handlers/secret_messages.py
"Mensaje secreto" en modo en línea, al estilo @mensajesecretobot.

Cómo se usa (en cualquier chat, sin necesidad de que el bot sea admin):

    @NombreDeTuBot @usuario1 @usuario2 el texto del mensaje aquí

Telegram muestra dos resultados para elegir:
    💬 Enviar a: @usuario1, @usuario2       -> mensaje normal, se puede
                                                releer las veces que quieran
                                                los destinatarios.
    💥 Enviar con autodestrucción a: ...    -> el texto se borra de la base
                                                de datos apenas alguien
                                                autorizado lo lee una vez.

El mensaje que queda publicado en el chat solo muestra QUIÉN puede leerlo,
nunca el contenido. El contenido real solo se muestra en un aviso
("toast") privado de Telegram cuando un destinatario autorizado toca
"🔒 Leer mensaje" — así nadie más en el chat lo ve.

Botones:
    🔒 Leer mensaje   -> visible siempre.
    ✏️ Editar mensaje -> solo antes de que lo lean, solo para el autor.
    📧 Responder      -> aparece en vez de "Editar" una vez que el mensaje
                          fue leído; abre el modo en línea con el @usuario
                          del autor original ya escrito, listo para
                          responder con otro mensaje secreto.

Nota técnica: como el mensaje se inserta vía modo en línea, el bot no
recibe automáticamente su chat_id/message_id. Para poder editarlo después
(al usar "Editar mensaje" desde el chat privado del bot) nos apoyamos en
`inline_message_id`, que sí llega en cada CallbackQuery y que Telegram
acepta en `edit_message_text` en vez de chat_id+message_id.
"""
from __future__ import annotations

import html
import logging
import re
import time
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database, SecretMessage

logger = logging.getLogger(__name__)

_MENTION_RE = re.compile(r"^@([A-Za-z0-9_]{3,32})$")
_SEC_CALLBACK_RE = re.compile(r"^sec:(read|edit):(\d+)$")
_START_EDIT_RE = re.compile(r"^secedit_(\d+)$")

_MAX_PREVIEW = 100
_MAX_TARGETS = 5
_MAX_TEXT_LEN = 500


# --------------------------------------------------------------------- #
# Parseo de la query en línea: "@user1 @user2 el mensaje ..."
# --------------------------------------------------------------------- #
def _parse_query(query: str) -> tuple[list[str], str]:
    tokens = query.strip().split()
    targets: list[str] = []
    i = 0
    while i < len(tokens) and _MENTION_RE.match(tokens[i]):
        username = tokens[i][1:].lower()
        if username not in targets:
            targets.append(username)
        i += 1
        if len(targets) >= _MAX_TARGETS:
            break
    text = " ".join(tokens[i:]).strip()
    return targets, text


def _compact_seconds(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


# --------------------------------------------------------------------- #
# Construcción del texto + botones de la tarjeta
# --------------------------------------------------------------------- #
def _build_card(secret: SecretMessage) -> tuple[str, InlineKeyboardMarkup]:
    targets_line = ", ".join(f"@{t}" for t in secret.targets)
    author = html.escape(secret.author_name)

    lines = [f"🔒 <b>Mensaje secreto de {author} para:</b>", targets_line]
    if secret.self_destruct:
        lines.append("<i>💥 Se autodestruye después de la primera lectura.</i>")
    if secret.is_read:
        reader = html.escape(secret.read_by_name or "alguien")
        delta = max(0, (secret.read_at or 0) - secret.created_at)
        lines.append(f"↳ <i>leído por {reader} después de {_compact_seconds(delta)}</i>")
        if secret.is_destroyed:
            lines.append("🔥 <i>Mensaje autodestruido.</i>")

    text = "\n".join(lines)

    read_button = InlineKeyboardButton("🔒 Leer mensaje", callback_data=f"sec:read:{secret.id}")
    if secret.is_read:
        if secret.author_username:
            reply_button = InlineKeyboardButton(
                "📧 Responder", switch_inline_query_current_chat=f"@{secret.author_username} "
            )
        else:
            reply_button = InlineKeyboardButton(
                "📧 Responder", switch_inline_query_current_chat=""
            )
        buttons = [read_button, reply_button]
    else:
        edit_button = InlineKeyboardButton("✏️ Editar mensaje", callback_data=f"sec:edit:{secret.id}")
        buttons = [read_button, edit_button]

    return text, InlineKeyboardMarkup([buttons])


# --------------------------------------------------------------------- #
# Modo en línea: @bot @usuario mensaje...
# --------------------------------------------------------------------- #
async def secret_inline_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    inline_query = update.inline_query
    if inline_query is None:
        return
    user = inline_query.from_user
    targets, text = _parse_query(inline_query.query)

    results: list[InlineQueryResultArticle] = []

    if not targets or not text:
        results.append(
            InlineQueryResultArticle(
                id="help",
                title="🔒 Mensaje secreto — cómo usarlo",
                description="Escribe: @usuario1 @usuario2 tu mensaje",
                input_message_content=InputTextMessageContent(
                    "ℹ️ Para enviar un mensaje secreto escribe, después del nombre del bot, "
                    "uno o más @usuario seguidos del mensaje. Ejemplo:\n\n"
                    f"<code>@{context.bot.username} @usuario tu mensaje aquí</code>",
                    parse_mode=ParseMode.HTML,
                ),
            )
        )
        await inline_query.answer(results, cache_time=0, is_personal=True)
        return

    text = text[:_MAX_TEXT_LEN]
    db: Database = context.application.bot_data["db"]
    author_name = user.first_name or user.username or "Usuario"
    targets_line = ", ".join(f"@{t}" for t in targets)
    preview = text if len(text) <= _MAX_PREVIEW else text[: _MAX_PREVIEW - 1] + "…"

    variants = (
        (False, "💬", "Enviar mensaje secreto a"),
        (True, "💥", "Enviar con autodestrucción a"),
    )
    for self_destruct, emoji, label in variants:
        secret_id = await db.create_secret_message(
            author_id=user.id, author_name=author_name, author_username=user.username,
            targets=targets, text=text, self_destruct=self_destruct,
        )
        secret = await db.get_secret_message(secret_id)
        card_text, markup = _build_card(secret)
        results.append(
            InlineQueryResultArticle(
                id=str(secret_id),
                title=f"{emoji} {label} {targets_line}",
                description=preview,
                input_message_content=InputTextMessageContent(card_text, parse_mode=ParseMode.HTML),
                reply_markup=markup,
            )
        )

    await inline_query.answer(results, cache_time=0, is_personal=True)


# --------------------------------------------------------------------- #
# Botones de la tarjeta (Leer mensaje / Editar mensaje)
# --------------------------------------------------------------------- #
async def secret_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    match = _SEC_CALLBACK_RE.match(query.data)
    if not match:
        return

    action, raw_id = match.groups()
    secret_id = int(raw_id)
    db: Database = context.application.bot_data["db"]
    secret = await db.get_secret_message(secret_id)
    if secret is None:
        await query.answer("⚠️ Este mensaje secreto ya no existe.", show_alert=True)
        return

    if query.inline_message_id and not secret.inline_message_id:
        await db.save_inline_message_id(secret_id, query.inline_message_id)
        secret.inline_message_id = query.inline_message_id

    user = query.from_user
    username = (user.username or "").lower()
    is_author = user.id == secret.author_id
    is_target = username in secret.targets or is_author

    if action == "read":
        if not is_target:
            allowed = ", ".join(f"@{t}" for t in secret.targets)
            await query.answer(f"⛔ Solo puede leerlo: {allowed}", show_alert=True)
            return
        if secret.text is None:
            await query.answer("🔥 Este mensaje ya se autodestruyó.", show_alert=True)
            return

        await query.answer(secret.text, show_alert=True)

        # Solo cuenta como "lectura" si quien la hace es un destinatario de
        # verdad (que el propio autor lo previsualice no gasta la autodestrucción).
        if not secret.is_read and username in secret.targets:
            reader_name = user.first_name or user.username or "Usuario"
            await db.mark_secret_read(secret_id, user.id, reader_name, wipe_text=secret.self_destruct)
            updated = await db.get_secret_message(secret_id)
            await _refresh_card(context, updated)
        return

    if action == "edit":
        if not is_author:
            await query.answer("⛔ Solo el autor puede editar este mensaje.", show_alert=True)
            return
        if secret.is_read:
            await query.answer(
                "⚠️ Este mensaje ya fue leído, así que no se puede editar (usa Responder para mandar uno nuevo).",
                show_alert=True,
            )
            return
        try:
            bot_username = context.bot.username
            await query.answer(url=f"https://t.me/{bot_username}?start=secedit_{secret_id}")
        except TelegramError:
            await query.answer(
                "✏️ Abre el chat privado conmigo y usa /start para poder editarlo.", show_alert=True
            )
        return


async def _refresh_card(context: ContextTypes.DEFAULT_TYPE, secret: Optional[SecretMessage]) -> None:
    if secret is None or not secret.inline_message_id:
        return
    card_text, markup = _build_card(secret)
    try:
        await context.bot.edit_message_text(
            inline_message_id=secret.inline_message_id,
            text=card_text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    except TelegramError as exc:
        logger.info("No se pudo refrescar la tarjeta del mensaje secreto #%s: %s", secret.id, exc)


# --------------------------------------------------------------------- #
# Deep link /start secedit_<id> (chat privado con el bot)
# --------------------------------------------------------------------- #
async def handle_secret_start_deeplink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Se llama desde el /start general. Devuelve True si el parámetro era
    de edición de un mensaje secreto (y por lo tanto ya se respondió)."""
    message = update.effective_message
    args = context.args or []
    if not args:
        return False
    match = _START_EDIT_RE.match(args[0])
    if not match:
        return False

    secret_id = int(match.group(1))
    db: Database = context.application.bot_data["db"]
    secret = await db.get_secret_message(secret_id)
    user = update.effective_user

    if secret is None or user is None or secret.author_id != user.id:
        await message.reply_text("⚠️ No podés editar ese mensaje secreto.")
        return True
    if secret.is_read:
        await message.reply_text(
            "⚠️ Ese mensaje secreto ya fue leído, así que no se puede editar. "
            "Usá el botón «Responder» de la tarjeta para mandar uno nuevo."
        )
        return True

    context.user_data["pending_secret_edit"] = {
        "secret_id": secret_id,
        "started_at": time.time(),
    }
    targets_line = ", ".join(f"@{t}" for t in secret.targets)
    await message.reply_text(
        "✏️ Escribime el nuevo texto para tu mensaje secreto dirigido a: "
        f"{targets_line}\n\n(Los destinatarios no cambian, solo el texto.)"
    )
    return True


# --------------------------------------------------------------------- #
# Consumir el siguiente mensaje de texto en el chat privado como la
# edición pendiente (se engancha desde el router general de mensajes).
# --------------------------------------------------------------------- #
async def try_consume_pending_secret_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.effective_message
    chat = update.effective_chat
    if chat is None or chat.type != "private" or message is None or not message.text:
        return False

    pending = context.user_data.get("pending_secret_edit")
    if not pending:
        return False

    context.user_data.pop("pending_secret_edit", None)
    secret_id = pending["secret_id"]
    db: Database = context.application.bot_data["db"]
    secret = await db.get_secret_message(secret_id)
    user = update.effective_user

    if secret is None or user is None or secret.author_id != user.id:
        await message.reply_text("⚠️ Ya no se puede editar ese mensaje.")
        return True
    if secret.is_read:
        await message.reply_text("⚠️ Ese mensaje ya fue leído, no se puede editar.")
        return True

    new_text = message.text.strip()[:_MAX_TEXT_LEN]
    if not new_text:
        await message.reply_text("⚠️ El mensaje no puede quedar vacío. Envía el texto nuevamente.")
        context.user_data["pending_secret_edit"] = pending
        return True

    await db.update_secret_text(secret_id, new_text)
    updated = await db.get_secret_message(secret_id)
    await _refresh_card(context, updated)
    await message.reply_text("✅ Mensaje secreto actualizado.")
    return True
