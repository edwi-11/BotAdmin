"""
handlers/confessions.py
Confesiones anónimas: la gente le escribe una confesión al bot por
privado y el bot la publica en el grupo como una tarjeta, sin decir
nunca quién la mandó.

Comandos (solo administradores del grupo):
    /confesion  — activa las confesiones y publica el cartel con las
                  instrucciones y el botón para escribir una.
                  También responde a la palabra "confesión"/"confesion"
                  escrita sola, sin la barra.
    /parar      — corta las confesiones en ese grupo.

Flujo completo:
 1. Un admin usa /confesion en el grupo. Se guarda que el grupo tiene
    confesiones activas y se manda el cartel (imagen) con un botón que
    abre el chat privado del bot con un deep link: ?start=conf_<group_id>.
 2. Al tocar el botón, el usuario cae en el privado del bot y el /start
    con ese parámetro lo deja "esperando confesión" para ESE grupo
    (context.user_data), y le pide el texto.
 3. El usuario escribe su confesión en un mensaje. El bot arma la
    tarjeta, la publica en el grupo (con el botón para que otros manden
    la suya) y le confirma por privado a qué grupo se mandó.
 4. /parar desactiva todo: el botón deja de aceptar confesiones nuevas.

Privacidad: el autor se guarda en la base SOLO para poder rastrear un
abuso si hiciera falta y para el límite anti-spam. No se muestra en el
grupo, ni en la tarjeta, ni en ningún comando.

Reacciones: cada confesión publicada lleva tres botones (👍 😂 ❤️) además
del de "hacer una confesión". Tocar uno registra la reacción de esa
persona y actualiza el contador en el botón al toque; tocar el mismo de
nuevo la saca; tocar otro la cambia. Es anónimo también: no se muestra
quién reaccionó, solo el total de cada uno.

Anti-abuso (lo mínimo, porque el anonimato invita a probar suerte):
 - Solo se aceptan confesiones para grupos donde la persona es miembro de
   verdad (se verifica contra Telegram en el momento).
 - Las palabras prohibidas que el grupo ya tenga configuradas
   (/palabras) se aplican también acá, para que las confesiones no sean
   un agujero por donde esquivar los filtros del grupo.
 - NO hay límite de cantidad por persona: se pueden mandar todas las
   confesiones que se quieran (ver MAX_POR_HORA más abajo).
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database
from utils.callbacks import safe_callback
from utils.confession_image import MAX_CHARS, generate_announcement_image, generate_confession_image
from utils.formatting import error, success
from utils.permissions import check_executor_is_admin

logger = logging.getLogger(__name__)

# "confesion" / "confesión" escrita sola (sin barra), como pidió el owner
# que funcione además del comando. Se exige que sea TODO el mensaje para
# no dispararse cuando alguien nombra la palabra en medio de una charla.
_CONFESION_TEXT_PATTERN = re.compile(r"^/?confesi[oó]n(?:es)?$", re.IGNORECASE)

_START_CONF_RE = re.compile(r"^conf_(-?\d+)$")

# Las tres reacciones disponibles: código interno -> emoji mostrado. El
# código es lo que viaja en el callback_data (más corto y estable que el
# emoji en sí, por si algún día se cambia el emoji sin romper botones viejos).
REACTION_EMOJIS = {"like": "👍", "haha": "😂", "love": "❤️"}

# Límite de confesiones por persona, por grupo y por hora.
# 0 = sin límite (es lo que pidió el owner). Si algún día hace falta
# frenar un abuso, poner acá un número (por ejemplo 3) vuelve a activar
# el tope sin tocar nada más.
MAX_POR_HORA = 0
_PENDING_KEY = "pending_confession"


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _confess_button_row(bot_username: str, group_id: int) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(
        "✍️ Hacer una confesión",
        url=f"https://t.me/{bot_username}?start=conf_{group_id}",
    )]


def _confess_button(bot_username: str, group_id: int) -> InlineKeyboardMarkup:
    """Solo el botón de confesar (para el cartel de instrucciones, que no
    tiene reacciones)."""
    return InlineKeyboardMarkup([_confess_button_row(bot_username, group_id)])


def _reaction_row(confession_id: int, counts: dict[str, int]) -> list[InlineKeyboardButton]:
    row = []
    for code, emoji in REACTION_EMOJIS.items():
        count = counts.get(code, 0)
        label = emoji if count == 0 else f"{emoji} {count}"
        row.append(InlineKeyboardButton(label, callback_data=f"conf:react:{confession_id}:{code}"))
    return row


def _confession_keyboard(
    confession_id: int, counts: dict[str, int], bot_username: str, group_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        _reaction_row(confession_id, counts),
        _confess_button_row(bot_username, group_id),
    ])


# --------------------------------------------------------------------- #
# /confesion — activar y publicar el cartel
# --------------------------------------------------------------------- #
async def confesion_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text(error("Este comando se usa dentro del grupo donde querés activar las confesiones."))
        return

    check = await check_executor_is_admin(context.bot, chat.id, user.id)
    if not check.allowed:
        await message.reply_text(error("Solo los administradores pueden activar las confesiones."))
        return

    await db.set_confessions_enabled(chat.id, True, user.id)

    title = chat.title or "este grupo"
    image = await asyncio.to_thread(generate_announcement_image, title)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    buf.name = "confesiones.png"

    bot_username = context.bot.username
    await context.bot.send_photo(
        chat.id, photo=buf,
        caption=(
            "🤫 <b>Confesiones activadas</b>\n\n"
            "Tocá el botón para escribirme tu confesión por privado. "
            "La publico acá sin decir quién la mandó.\n\n"
            f"<i>Los administradores pueden cortarlas con /parar.</i>"
        ),
        parse_mode="HTML",
        reply_markup=_confess_button(bot_username, chat.id),
    )


async def confesion_text_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Permite escribir "confesión" (o "confesion") sola, sin barra, como
    alternativa a /confesion. Devuelve True si consumió el mensaje."""
    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None or not message.text:
        return False
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False
    if not _CONFESION_TEXT_PATTERN.match(message.text.strip()):
        return False

    await confesion_command(update, context)
    return True


# --------------------------------------------------------------------- #
# /parar — desactivar
# --------------------------------------------------------------------- #
async def parar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text(error("Este comando se usa dentro del grupo."))
        return

    check = await check_executor_is_admin(context.bot, chat.id, user.id)
    if not check.allowed:
        await message.reply_text(error("Solo los administradores pueden cortar las confesiones."))
        return

    if not await db.are_confessions_enabled(chat.id):
        await message.reply_text(error("Las confesiones ya estaban desactivadas en este grupo."))
        return

    await db.set_confessions_enabled(chat.id, False, user.id)
    await message.reply_text(
        success("Confesiones desactivadas. No voy a publicar ninguna más hasta que usen /confesion de nuevo.")
    )


# --------------------------------------------------------------------- #
# Deep link: /start conf_<group_id>
# --------------------------------------------------------------------- #
async def handle_confession_start_deeplink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Se llama desde el /start general. Devuelve True si el parámetro era
    de confesión (y por lo tanto ya se respondió)."""
    args = context.args or []
    if not args:
        return False
    match = _START_CONF_RE.match(args[0])
    if not match:
        return False

    message = update.effective_message
    user = update.effective_user
    group_id = int(match.group(1))
    db = _get_db(context)

    if not await db.are_confessions_enabled(group_id):
        await message.reply_text("⚠️ Las confesiones no están activas en ese grupo ahora mismo.")
        return True

    # Verificamos que sea miembro de verdad: si no, cualquiera con el link
    # podría publicar en un grupo al que ni pertenece.
    try:
        member = await context.bot.get_chat_member(group_id, user.id)
        if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            await message.reply_text("⚠️ Tenés que ser miembro del grupo para mandar una confesión ahí.")
            return True
    except TelegramError:
        await message.reply_text("⚠️ No pude verificar que seas miembro de ese grupo.")
        return True

    try:
        chat = await context.bot.get_chat(group_id)
        title = chat.title or str(group_id)
    except TelegramError:
        title = str(group_id)

    context.user_data[_PENDING_KEY] = {"group_id": group_id, "title": title, "started_at": time.time()}
    await message.reply_text(
        f"🤫 Escribime tu confesión para <b>{title}</b> en un solo mensaje.\n\n"
        f"Se publica sin decir quién la mandó. Máximo {MAX_CHARS} caracteres.\n"
        "Si te arrepentís, escribí /cancelar.",
        parse_mode="HTML",
    )
    return True


async def cancelar_confesion_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.pop(_PENDING_KEY, None):
        await update.effective_message.reply_text("Listo, no mando nada.")
    else:
        await update.effective_message.reply_text("No tenías ninguna confesión a medio escribir.")


# --------------------------------------------------------------------- #
# Recibir el texto de la confesión (chat privado)
# --------------------------------------------------------------------- #
async def try_consume_confession_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Si la persona venía de tocar el botón y está escribiendo su
    confesión, la publica en el grupo. Devuelve True si consumió el
    mensaje."""
    pending = context.user_data.get(_PENDING_KEY)
    if not pending:
        return False

    message = update.effective_message
    user = update.effective_user
    if message is None or update.effective_chat.type != ChatType.PRIVATE:
        return False

    text = (message.text or "").strip()
    if not text:
        await message.reply_text("Mandame la confesión como texto, por favor.")
        return True

    group_id = pending["group_id"]
    title = pending.get("title") or str(group_id)
    db = _get_db(context)

    if not await db.are_confessions_enabled(group_id):
        context.user_data.pop(_PENDING_KEY, None)
        await message.reply_text("⚠️ Las confesiones se desactivaron en ese grupo mientras escribías. No mandé nada.")
        return True

    if len(text) > MAX_CHARS:
        await message.reply_text(
            f"Esa confesión tiene {len(text)} caracteres y el máximo es {MAX_CHARS}. "
            "Acortala un poco y mandámela de nuevo."
        )
        return True

    if MAX_POR_HORA > 0:
        recientes = await db.count_recent_confessions(group_id, user.id, int(time.time()) - 3600)
        if recientes >= MAX_POR_HORA:
            context.user_data.pop(_PENDING_KEY, None)
            await message.reply_text(
                f"Ya mandaste {recientes} confesiones a ese grupo en la última hora. "
                "Esperá un rato antes de mandar otra."
            )
            return True

    # Las palabras prohibidas del grupo también valen acá: si no, las
    # confesiones serían un atajo para esquivar los filtros del grupo.
    banned = await db.get_banned_words(group_id)
    lowered = text.lower()
    if any(word.lower() in lowered for word in banned):
        context.user_data.pop(_PENDING_KEY, None)
        await message.reply_text(
            "⚠️ Tu confesión tiene palabras que ese grupo tiene prohibidas, así que no la publiqué."
        )
        return True

    confession_id, numero = await db.add_confession(group_id, user.id, text)
    image = await asyncio.to_thread(generate_confession_image, text, numero)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    buf.name = f"confesion_{numero}.png"

    keyboard = _confession_keyboard(confession_id, {}, context.bot.username, group_id)
    try:
        await context.bot.send_photo(group_id, photo=buf, reply_markup=keyboard)
    except TelegramError as exc:
        context.user_data.pop(_PENDING_KEY, None)
        logger.warning("No pude publicar la confesión #%s en %s: %s", numero, group_id, exc)
        await message.reply_text(f"⚠️ No pude publicarla en el grupo: {exc}")
        return True

    context.user_data.pop(_PENDING_KEY, None)
    await message.reply_text(
        f"✅ Listo, tu confesión se mandó al grupo <b>{title}</b>.\n\n"
        "Nadie ahí sabe que fuiste vos. Si querés mandar otra, tocá de nuevo el botón en el grupo.",
        parse_mode="HTML",
    )
    return True


# --------------------------------------------------------------------- #
# Reacciones (👍 😂 ❤️) en las tarjetas publicadas
# --------------------------------------------------------------------- #
@safe_callback
async def confession_reaction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    db = _get_db(context)

    _, _, confession_id_raw, code = query.data.split(":")
    confession_id = int(confession_id_raw)
    if code not in REACTION_EMOJIS:
        await query.answer()
        return

    confession = await db.get_confession(confession_id)
    if confession is None:
        await query.answer("Esta confesión ya no existe.", show_alert=True)
        return

    user = query.from_user
    current = await db.get_confession_reaction(confession_id, user.id)
    if current == code:
        await db.remove_confession_reaction(confession_id, user.id)
        toast = "Reacción quitada."
    else:
        await db.set_confession_reaction(confession_id, user.id, code)
        toast = f"¡Reaccionaste con {REACTION_EMOJIS[code]}!"

    counts = await db.get_confession_reaction_counts(confession_id)
    keyboard = _confession_keyboard(confession_id, counts, context.bot.username, confession["group_id"])
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except TelegramError:
        pass  # el conteo puede no haber cambiado visualmente (Telegram no deja "editar" a lo mismo); no pasa nada
    await query.answer(toast)
