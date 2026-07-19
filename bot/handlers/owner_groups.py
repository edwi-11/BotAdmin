"""
handlers/owner_groups.py
/grupos — le manda al propietario (por privado) la lista de todos los
grupos donde está el bot, con un link de invitación fresco de cada uno.

Se puede usar tanto en un grupo como en el chat privado con el bot; la
respuesta siempre se manda por privado al propietario para no exponer los
links de invitación de todos los grupos dentro de un chat que puede tener
más gente mirando.

Requisitos por grupo:
- El bot tiene que seguir siendo miembro (si lo sacaron, se muestra el
  título guardado pero avisando que ya no está).
- Para generar el link, el bot necesita el permiso de admin
  "Invitar usuarios vía enlace" en ese grupo — si no lo tiene, se avisa en
  vez de fallar en silencio.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from database import Database
from utils.permissions import is_owner

logger = logging.getLogger(__name__)

_CHUNK_LIMIT = 3500  # margen bajo el límite de 4096 de Telegram por mensaje


async def grupos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message

    if not is_owner(user.id):
        await message.reply_text("🔒 Este comando es solo para el propietario del bot.")
        return

    db: Database = context.application.bot_data["db"]
    groups = await db.get_known_groups()  # list[tuple[group_id, title]]

    if not groups:
        await message.reply_text("No tengo registrado ningún grupo todavía.")
        return

    lines: list[str] = [f"📋 <b>Grupos donde estoy</b> ({len(groups)}):\n"]
    for group_id, title in groups:
        name = title or f"Grupo {group_id}"
        try:
            link = await context.bot.export_chat_invite_link(group_id)
            lines.append(f"• <b>{name}</b>\n  {link}")
        except Forbidden:
            lines.append(f"• <b>{name}</b>\n  ⚠️ Ya no soy miembro de este grupo.")
        except TelegramError as exc:
            lines.append(f"• <b>{name}</b>\n  ⚠️ No pude generar el link ({exc}). Necesito ser admin con permiso de invitar.")

    text = "\n\n".join(lines)
    chunks = [text[i : i + _CHUNK_LIMIT] for i in range(0, len(text), _CHUNK_LIMIT)] or [text]

    try:
        for chunk in chunks:
            await context.bot.send_message(user.id, chunk, parse_mode="HTML", disable_web_page_preview=True)
    except Forbidden:
        await message.reply_text(
            "⚠️ No puedo mandarte mensajes por privado todavía. "
            "Abrí un chat conmigo (tocá mi nombre y dale /start) y volvé a probar /grupos."
        )
        return

    if update.effective_chat.id != user.id:
        await message.reply_text("✅ Te mandé la lista de grupos por privado.")
