"""
handlers/owner_groups.py
/grupos — le muestra al propietario (por privado) la lista completa de
grupos guardados como un menú que se puede ir pasando de página con
botones (⬅️ Anterior / Siguiente ➡️), en vez de mandar todo junto en un
solo mensaje larguísimo — así se puede navegar cómodo aunque haya
decenas de grupos.

Se puede usar tanto en un grupo como en el chat privado con el bot; el
menú siempre se manda por privado al propietario para no exponer los
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

El reporte (que implica una consulta a Telegram por cada grupo) se pide
una sola vez al abrir /grupos y se guarda en memoria (user_data) para que
pasar de página sea instantáneo; el link de invitación de cada grupo se
pide recién cuando esa página se muestra por primera vez, y se cachea
para no volver a pedirlo si el propietario va y vuelve entre páginas.
Hay un botón "🔄 Actualizar" para volver a consultar todo desde cero.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import ContextTypes

from database import Database
from utils.callbacks import safe_callback
from utils.groups import GroupStatus, get_groups_report
from utils.permissions import is_owner

logger = logging.getLogger(__name__)

GROUPS_PER_PAGE = 6
_REPORT_KEY = "grupos_report"
_INVITE_CACHE_KEY = "grupos_invite_cache"


def _format_group_block(group: GroupStatus, invite_link: str | None, invite_error: str | None) -> str:
    parts = [f"• <b>{group.title}</b>", f"  ID: <code>{group.group_id}</code>"]
    if group.member_count is not None:
        parts.append(f"  Miembros: {group.member_count}")
    parts.append(f"  Estado: {group.status_label}")
    if invite_link:
        parts.append(f"  {invite_link}")
    elif invite_error:
        parts.append(f"  ⚠️ {invite_error}")
    return "\n".join(parts)


async def _invite_link_for(context: ContextTypes.DEFAULT_TYPE, group: GroupStatus) -> tuple[str | None, str | None]:
    """Devuelve (link, error) para un grupo activo, usando/llenando el
    cache de user_data para no volver a pedirlo cada vez que se muestra
    la misma página."""
    if group.status != "activo":
        return None, None
    cache: dict[int, str] = context.user_data.setdefault(_INVITE_CACHE_KEY, {})
    if group.group_id in cache:
        cached = cache[group.group_id]
        return (cached, None) if cached.startswith("http") else (None, cached)
    try:
        link = await context.bot.export_chat_invite_link(group.group_id)
        cache[group.group_id] = link
        return link, None
    except TelegramError as exc:
        err = f"No pude generar el link ({exc}). Necesito ser admin con permiso de invitar."
        cache[group.group_id] = err
        return None, err


async def _render_page(context: ContextTypes.DEFAULT_TYPE, report: list[GroupStatus], page: int) -> tuple[str, InlineKeyboardMarkup]:
    total_pages = max(1, (len(report) + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * GROUPS_PER_PAGE
    page_groups = report[start:start + GROUPS_PER_PAGE]

    active_count = sum(1 for g in report if g.status == "activo")
    blocks = []
    for group in page_groups:
        invite_link, invite_error = await _invite_link_for(context, group)
        blocks.append(_format_group_block(group, invite_link, invite_error))

    header = (
        f"📋 <b>Grupos registrados</b> ({len(report)} en total, {active_count} activos)\n"
        f"Página {page + 1}/{total_pages}\n"
    )
    text = header + "\n" + "\n\n".join(blocks)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Anterior", callback_data=f"g:page:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Siguiente ➡️", callback_data=f"g:page:{page + 1}"))
    rows = [nav] if nav else []
    rows.append([InlineKeyboardButton("🔄 Actualizar", callback_data="g:refresh")])
    return text, InlineKeyboardMarkup(rows)


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

    context.user_data[_REPORT_KEY] = report
    context.user_data[_INVITE_CACHE_KEY] = {}
    text, markup = await _render_page(context, report, 0)

    try:
        await context.bot.send_message(user.id, text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
    except Forbidden:
        await message.reply_text(
            "⚠️ No puedo mandarte mensajes por privado todavía. "
            "Abrí un chat conmigo (tocá mi nombre y dale /start) y volvé a probar /grupos."
        )
        return

    if update.effective_chat.id != user.id:
        await message.reply_text("✅ Te mandé la lista de grupos por privado.")


@safe_callback
async def grupos_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not is_owner(user.id):
        await query.answer("Este comando es solo para el propietario.", show_alert=True)
        return

    action_raw = (query.data or "").split(":", 1)[1]
    action, _, action_arg = action_raw.partition(":")

    if action == "refresh":
        db: Database = context.application.bot_data["db"]
        await query.answer("Actualizando...")
        report = await get_groups_report(context.bot, db)
        context.user_data[_REPORT_KEY] = report
        context.user_data[_INVITE_CACHE_KEY] = {}
        if not report:
            await query.edit_message_text("No tengo registrado ningún grupo todavía.")
            return
        text, markup = await _render_page(context, report, 0)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        return

    if action == "page":
        report = context.user_data.get(_REPORT_KEY)
        if not report:
            await query.answer("Esta lista ya expiró, usá /grupos de nuevo.", show_alert=True)
            return
        page = int(action_arg) if action_arg.lstrip("-").isdigit() else 0
        text, markup = await _render_page(context, report, page)
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup, disable_web_page_preview=True)
        await query.answer()
        return

    await query.answer()
