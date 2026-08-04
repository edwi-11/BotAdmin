"""
handlers/owner_groups.py
/grupos — le manda al propietario (por privado) la lista completa de
grupos guardados, con nombre real, ID, cantidad de miembros y estado
(activo / expulsado / sin acceso), más un link de invitación fresco para
cada grupo activo.

Se puede usar tanto en un grupo como en el chat privado con el bot; la
respuesta siempre se manda por privado al propietario para no exponer los
links de invitación de todos los grupos dentro de un chat que puede tener
más gente mirando.

El estado de cada grupo se calcula contra Telegram en el momento (ver
utils/groups.py::get_groups_report), no se confía ciegamente en lo que
quedó guardado en la base:
- ✅ Activo: el bot sigue siendo miembro; se muestra nombre, ID y
  cantidad de miembros, y se intenta generar un link de invitación
  (necesita el permiso de admin "Invitar usuarios vía enlace").
- 🚫 Bot expulsado: Telegram confirmó que ya no somos miembros (nos
  sacaron, nos banearon, o el grupo se borró). Ya se limpió de la base.
- ⚠️ Sin acceso: no se pudo verificar en este momento (rate limit, error
  de red puntual). El grupo NO se borra de la base por esto: se vuelve a
  intentar la próxima vez que se use /grupos, /owner o /menu.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from database import Database
from utils.groups import GroupStatus, get_groups_report
from utils.permissions import is_owner

logger = logging.getLogger(__name__)

_CHUNK_LIMIT = 3500  # margen bajo el límite de 4096 de Telegram por mensaje


def _format_group_line(group: GroupStatus, invite_link: str | None, invite_error: str | None) -> str:
    parts = [f"• <b>{group.title}</b>", f"  ID: <code>{group.group_id}</code>"]
    if group.member_count is not None:
        parts.append(f"  Miembros: {group.member_count}")
    parts.append(f"  Estado: {group.status_label}")
    if invite_link:
        parts.append(f"  {invite_link}")
    elif invite_error:
        parts.append(f"  ⚠️ {invite_error}")
    return "\n".join(parts)


async def grupos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message

    if not is_owner(user.id):
        await message.reply_text("🔒 Este comando es solo para el propietario del bot.")
        return

    db: Database = context.application.bot_data["db"]
    report = await get_groups_report(context.bot, db)

    if not report:
        await message.reply_text("No tengo registrado ningún grupo todavía.")
        return

    active_count = sum(1 for g in report if g.status == "activo")
    lines: list[str] = [
        f"📋 <b>Grupos registrados</b> ({len(report)} en total, {active_count} activos):\n"
    ]

    for group in report:
        invite_link = None
        invite_error = None
        if group.status == "activo":
            try:
                invite_link = await context.bot.export_chat_invite_link(group.group_id)
            except TelegramError as exc:
                invite_error = f"No pude generar el link ({exc}). Necesito ser admin con permiso de invitar."
        lines.append(_format_group_line(group, invite_link, invite_error))

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
