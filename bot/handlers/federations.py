"""
handlers/federations.py
Sistema de Federaciones (Feds): listas de baneados compartidas entre
varios grupos, administradas por un creador y sus administradores de
federación (roles separados de "admin de grupo" en Telegram).

Comandos:
    /newfed                       — crea una federación (pide el nombre)
    /fban usuario [motivo]        — banea en todos los grupos de la fed
    /joinfed IDFed                — vincula el grupo actual a una fed
    /fchat IDFed                  — hace del chat actual el FChat de esa fed
    /fedpromote usuario           — invita a alguien a administrar la fed
    /feddemote usuario            — le saca la administración de la fed
    /import (respondiendo a .csv) — importa una lista de baneados
    /fbanstat [usuario] [IDFed]   — consulta baneos de federación

Diseño (ver database.py para el esquema):
    feds              — una fila por federación (fed_id, nombre, creador, FChat)
    fed_admins        — administradores ACEPTADOS de cada federación
    fed_groups        — qué grupo pertenece a qué federación (uno c/u)
    fed_bans          — el registro de baneos de cada federación
    fed_promote_invites — invitaciones de administrador pendientes/resueltas
    fed_imports       — historial de importaciones por CSV

Aplicación del baneo en los grupos:
    - /fban banea de inmediato en TODOS los grupos que ya pertenezcan a
      la federación (ban_chat_member en cada uno).
    - /joinfed sincroniza al revés: en cuanto un grupo se une a una fed,
      se le aplican ahí mismo todos los baneos ya existentes de esa fed.
    - Para nuevos ingresos futuros, el chequeo vive en
      handlers/greetings.py (on_new_members) y handlers/join_requests.py
      (on_chat_join_request): antes de dar la bienvenida a alguien, se
      revisa si está fbaneado en la federación de ESE grupo, y si es así
      se lo banea/rechaza en el momento en vez de saludarlo.
    - /import SÍ aplica los baneos importados en todos los grupos que ya
      pertenezcan a la federación (por si alguno de los importados ya
      estaba adentro de esos grupos desde antes) — pero lo hace en
      SEGUNDO PLANO, con pausa entre cada llamada y un mensaje de
      progreso que se va editando, para no trabarse ni pegarle de golpe
      a la API de Telegram con listas grandes (ver
      _apply_import_bans_background).
"""
from __future__ import annotations

import asyncio
import csv
import html
import io
import logging
from datetime import datetime
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import settings
from database import Database
from utils.action_stickers import send_action_sticker
from utils.callbacks import safe_callback
from utils.formatting import error, success
from utils.parsing import resolve_target
from utils.permissions import (
    check_bot_rights,
    check_executor_is_admin,
    check_group_owner_or_cofounder,
    is_owner,
)

logger = logging.getLogger(__name__)


def _mention(user_id: int, name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{html.escape(name or str(user_id))}</a>'


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


async def _resolve_fed_context(
    update: Update, db: Database, args: list[str]
) -> tuple[Optional[str], list[str]]:
    """Para comandos que pueden usarse DENTRO de un grupo vinculado a una
    fed (usa esa fed automáticamente) O en cualquier lado indicando el
    IDFed a mano como primer argumento. Devuelve (fed_id o None, resto de
    los argumentos sin consumir)."""
    chat = update.effective_chat
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        fed_id = await db.get_group_fed(chat.id)
        if fed_id:
            return fed_id, args
    if args and args[0].upper().startswith("FED-"):
        return args[0].strip().upper(), args[1:]
    return None, args


async def _require_fed_owner(message, fed, user_id: int) -> bool:
    if fed["owner_id"] == user_id or is_owner(user_id):
        return True
    await message.reply_text(error("Solo el creador de la federación puede hacer esto."))
    return False


# --------------------------------------------------------------------- #
# /newfed — wizard de un solo paso (pide el nombre)
# --------------------------------------------------------------------- #
async def newfed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["fed_awaiting"] = "name"
    await update.effective_message.reply_text(
        "🏛 ¿Cómo querés llamar a la nueva federación? Escribime el nombre "
        "(o /cancelarfed para no crear ninguna)."
    )


async def cancelarfed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.pop("fed_awaiting", None):
        await update.effective_message.reply_text("Cancelado.")
    else:
        await update.effective_message.reply_text("No tenías ninguna federación a medio crear.")


async def try_consume_fed_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("fed_awaiting") != "name":
        return False
    message = update.effective_message
    if message is None or not message.text:
        return False

    name = message.text.strip()[:64]
    context.user_data.pop("fed_awaiting", None)
    if not name:
        await message.reply_text(error("El nombre no puede estar vacío. Probá /newfed de nuevo."))
        return True

    db = _get_db(context)
    fed_id = await db.create_fed(name, update.effective_user.id)
    await message.reply_text(
        "✅ <b>Federación creada correctamente.</b>\n"
        f"Nombre: {html.escape(name)}\n"
        f"ID: <code>{fed_id}</code>",
        parse_mode="HTML",
    )
    return True


# --------------------------------------------------------------------- #
# /joinfed IDFed — vincula el grupo actual
# --------------------------------------------------------------------- #
async def joinfed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text(error("Este comando se usa dentro del grupo que querés vincular a una federación."))
        return
    if not context.args:
        await message.reply_text(error("Usá /joinfed IDFed (ej. /joinfed FED-8K29X)."))
        return

    fed_id = context.args[0].strip().upper()
    fed = await db.get_fed(fed_id)
    if fed is None:
        await message.reply_text(error(f"No existe ninguna federación con el ID {fed_id}."))
        return
    if not await _require_fed_owner(message, fed, user.id):
        return

    bot_rights = await check_bot_rights(context.bot, chat.id)
    if not bot_rights.allowed:
        await message.reply_text(error(bot_rights.reason))
        return

    current = await db.get_group_fed(chat.id)
    if current == fed_id:
        await message.reply_text(error("Este grupo ya pertenece a esta federación."))
        return

    await db.join_group_to_fed(chat.id, fed_id)
    # /joinfed "normal" siempre vuelve todo a la normalidad, incluso si
    # este grupo había apagado el sistema de fed con /nofed antes.
    await db.clear_fed_disabled(chat.id)

    # Sincronizamos al revés: aplicamos ya mismo todos los baneos que ya
    # tenía la federación, para que este grupo quede al día de una.
    existing_bans = await db.get_fed_bans(fed_id)
    applied = 0
    for row in existing_bans:
        try:
            await context.bot.ban_chat_member(chat.id, row["user_id"])
            applied += 1
        except TelegramError:
            pass

    extra = f"\n\nSe aplicaron {applied}/{len(existing_bans)} baneos ya existentes de la federación en este grupo." if existing_bans else ""
    await message.reply_text(
        success(f"Este grupo ahora forma parte de la federación <b>{html.escape(fed['name'])}</b>.") + extra,
        parse_mode="HTML",
    )
    await send_action_sticker(context.bot, chat.id, settings.sticker_completado)

    if fed["fchat_id"] and fed["fchat_id"] != chat.id:
        try:
            await context.bot.send_message(
                fed["fchat_id"],
                f"🔗 El grupo <b>{html.escape(chat.title or str(chat.id))}</b> se unió a la federación "
                f"<b>{html.escape(fed['name'])}</b>.",
                parse_mode="HTML",
            )
        except TelegramError:
            pass


# --------------------------------------------------------------------- #
# /nofed — APAGA por completo el sistema de fed en ESTE grupo: lo saca de
# su federación actual (si tiene una) y además lo marca como "apagado" en
# fed_disabled_groups. Mientras esté marcado, /fban usado adentro de este
# grupo no hace absolutamente nada y no contesta nada (ni "no pertenece a
# ninguna federación" ni ningún otro mensaje) — antes de esto, /nofed solo
# desvinculaba al grupo, pero /fban seguía respondiendo con esos mensajes.
# /joinfed IDFed (uso normal) es lo único que saca al grupo de esta lista
# y devuelve el comportamiento a la normalidad.
# Lo puede usar el dueño real del grupo (creator de Telegram) o un
# cofundador (admin con todos los permisos clave) — a propósito, sin
# necesidad de ser admin de la federación.
# --------------------------------------------------------------------- #
async def nofed_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text(error("Este comando solo funciona dentro de un grupo."))
        return

    perm = await check_group_owner_or_cofounder(context.bot, chat.id, user.id)
    if not perm.allowed:
        await message.reply_text(error(perm.reason))
        return

    fed_id = await db.get_group_fed(chat.id)
    fed = await db.get_fed(fed_id) if fed_id else None

    if fed_id is not None:
        await db.leave_group_fed(chat.id)

    already_off = await db.is_fed_disabled(chat.id)
    await db.set_fed_disabled(chat.id)

    if fed:
        fed_name = html.escape(fed["name"])
        text = (
            f"Este grupo salió de la federación <b>{fed_name}</b> y el sistema de fed quedó "
            "apagado acá: /fban ya no va a hacer ni decir nada en este chat hasta que uses "
            "/joinfed de nuevo."
        )
    elif already_off:
        text = "El sistema de fed ya estaba apagado en este chat."
    else:
        text = (
            "Sistema de fed apagado en este chat: /fban ya no va a hacer ni decir nada acá "
            "hasta que uses /joinfed de nuevo."
        )
    await message.reply_text(success(text), parse_mode="HTML")
    await send_action_sticker(context.bot, chat.id, settings.sticker_completado)

    if fed and fed["fchat_id"] and fed["fchat_id"] != chat.id:
        try:
            await context.bot.send_message(
                fed["fchat_id"],
                f"🚪 El grupo <b>{html.escape(chat.title or str(chat.id))}</b> salió de la federación "
                f"<b>{html.escape(fed['name'])}</b> (usando /nofed).",
                parse_mode="HTML",
            )
        except TelegramError:
            pass


# --------------------------------------------------------------------- #
# /fchat IDFed — hace del chat actual el FChat de esa federación
# --------------------------------------------------------------------- #
async def fchat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)

    if not context.args:
        await message.reply_text(error("Usá /fchat IDFed dentro del chat que querés usar como FChat."))
        return
    fed_id = context.args[0].strip().upper()
    fed = await db.get_fed(fed_id)
    if fed is None:
        await message.reply_text(error(f"No existe ninguna federación con el ID {fed_id}."))
        return
    if not await _require_fed_owner(message, fed, user.id):
        return

    await db.set_fed_chat(fed_id, chat.id)
    await message.reply_text(
        success(f"Este chat ahora es el FChat de la federación <b>{html.escape(fed['name'])}</b>."),
        parse_mode="HTML",
    )
    await send_action_sticker(context.bot, chat.id, settings.sticker_completado)


# --------------------------------------------------------------------- #
# /fban usuario [motivo]  (o respondiendo a un mensaje: /fban [motivo])
# --------------------------------------------------------------------- #
async def fban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)
    args = list(context.args or [])
    is_private = chat.type == ChatType.PRIVATE

    if is_private:
        if not args or not args[0].upper().startswith("FED-"):
            await message.reply_text(
                error("Desde el privado usá /fban IDFed usuario motivo (o respondiendo a un mensaje: /fban IDFed motivo).")
            )
            return
        fed_id = args.pop(0).strip().upper()
    else:
        fed_id = await db.get_group_fed(chat.id)
        if fed_id is None:
            if await db.is_fed_disabled(chat.id):
                # El grupo apagó el sistema de fed con /nofed: no hacemos
                # ni decimos nada hasta que se use /joinfed de nuevo.
                return
            await message.reply_text(error("Este grupo no pertenece a ninguna federación. Usá /joinfed IDFed primero."))
            return

    fed = await db.get_fed(fed_id)
    if fed is None:
        await message.reply_text(error(f"No existe ninguna federación con el ID {fed_id}."))
        return

    if not (await db.is_fed_admin(fed_id, user.id) or is_owner(user.id)):
        await message.reply_text(error("No sos administrador de esta federación."))
        return
    if is_private and fed["owner_id"] != user.id and not is_owner(user.id):
        await message.reply_text(error("Solo el creador de la federación puede usar /fban desde el privado."))
        return

    resolved = await resolve_target(update, db, args)
    if isinstance(resolved, str):
        await message.reply_text(error(resolved))
        return

    if await db.is_fed_banned(fed_id, resolved.user_id):
        await message.reply_text(error(f"{resolved.display_name} ya está baneado de esta federación."))
        return

    reason = " ".join(resolved.remaining_args).strip() or None
    await db.add_fed_ban(
        fed_id, resolved.user_id, resolved.username, resolved.display_name, reason, user.id, imported=False
    )

    group_ids = await db.get_fed_groups(fed_id)
    banned_in = 0
    failed_groups: list[tuple[int, str]] = []
    for gid in group_ids:
        try:
            await context.bot.ban_chat_member(gid, resolved.user_id)
            banned_in += 1
        except TelegramError as exc:
            logger.info("No pude aplicar el FBAN en el grupo %s: %s", gid, exc)
            failed_groups.append((gid, str(exc)))

    text = (
        "🚫 <b>Usuario baneado de la federación</b>\n\n"
        f"👤 Usuario: {_mention(resolved.user_id, resolved.display_name)}\n"
        f"🛡 Federación: {html.escape(fed['name'])}\n"
        f"📝 Motivo: {html.escape(reason) if reason else 'Sin especificar'}\n\n"
        f"👮 Administrador de la federación: {_mention(user.id, user.first_name)}"
    )

    # Siempre mostramos cuántos grupos recibieron el baneo de verdad —
    # antes esto solo se mostraba si /fban se ejecutaba desde el privado,
    # así que desde un grupo el mensaje decía "baneado" aunque el baneo
    # real en Telegram hubiera fallado (típicamente porque el bot no es
    # admin ahí, o le falta el permiso de restringir miembros).
    status_lines = [f"\n\n📊 Aplicado en {banned_in}/{len(group_ids)} grupos de la federación."]
    if failed_groups:
        status_lines.append(
            "⚠️ No se pudo banear en:\n" + "\n".join(
                f"• <code>{gid}</code>: {html.escape(reason)}" for gid, reason in failed_groups[:5]
            )
        )
        if len(failed_groups) > 5:
            status_lines.append(f"…y {len(failed_groups) - 5} más.")
        status_lines.append(
            "Lo más común es que el bot no sea administrador ahí, o le falte el permiso "
            "\"Restringir miembros\" — revisalo con /grupos."
        )
    extra = "\n".join(status_lines)

    # Lugar 1: donde se ejecutó el comando.
    await message.reply_text(text + extra, parse_mode="HTML")
    await send_action_sticker(context.bot, chat.id, settings.sticker_completado)

    # Lugar 2: el FChat, si está configurado y no es el mismo chat de arriba.
    if fed["fchat_id"] and fed["fchat_id"] != chat.id:
        try:
            await context.bot.send_message(fed["fchat_id"], text, parse_mode="HTML")
        except TelegramError as exc:
            logger.info("No pude notificar el FBAN en el FChat de %s: %s", fed_id, exc)

    # Lugar 3: privado del creador — SOLO si fue el creador quien lo hizo
    # Y lo hizo desde un GRUPO (si ya estaba en su privado, ya lo vio ahí
    # mismo arriba; si fue OTRO administrador desde un grupo, no se le
    # manda copia extra al creador).
    if not is_private and user.id == fed["owner_id"]:
        try:
            await context.bot.send_message(fed["owner_id"], text, parse_mode="HTML")
        except TelegramError:
            pass


# --------------------------------------------------------------------- #
# /fedpromote usuario — invita a administrar la federación (con botones)
# --------------------------------------------------------------------- #
async def fedpromote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    db = _get_db(context)

    fed_id, rest = await _resolve_fed_context(update, db, list(context.args or []))
    if fed_id is None:
        await message.reply_text(
            error("Especificá el IDFed (ej. /fedpromote FED-XXXXXX @usuario) o usá este comando dentro de un grupo de la federación.")
        )
        return
    fed = await db.get_fed(fed_id)
    if fed is None:
        await message.reply_text(error(f"No existe ninguna federación con el ID {fed_id}."))
        return
    if not await _require_fed_owner(message, fed, user.id):
        return

    resolved = await resolve_target(update, db, rest)
    if isinstance(resolved, str):
        await message.reply_text(error(resolved))
        return

    if resolved.user_id == fed["owner_id"]:
        await message.reply_text(error("Esa persona ya es el creador de la federación."))
        return
    if await db.is_fed_admin(fed_id, resolved.user_id):
        await message.reply_text(error(f"{resolved.display_name} ya es administrador de esta federación."))
        return
    if await db.get_pending_invite(fed_id, resolved.user_id):
        await message.reply_text(error(f"{resolved.display_name} ya tiene una invitación pendiente de esta federación."))
        return

    invite_id = await db.create_promote_invite(fed_id, resolved.user_id, user.id)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Aceptar", callback_data=f"fedp:acc:{invite_id}"),
        InlineKeyboardButton("❌ Rechazar", callback_data=f"fedp:rej:{invite_id}"),
    ]])
    try:
        await context.bot.send_message(
            resolved.user_id,
            f"🛡 ¿Te gustaría formar parte de la federación <b>{html.escape(fed['name'])}</b> como administrador?",
            parse_mode="HTML", reply_markup=keyboard,
        )
    except TelegramError:
        await message.reply_text(
            error(f"No pude escribirle a {resolved.display_name} por privado — necesita iniciar un chat conmigo primero.")
        )
        return

    await message.reply_text(success(f"Le mandé la invitación a {resolved.display_name}. Falta que la acepte."))


@safe_callback
async def fedpromote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    db = _get_db(context)
    _, action, invite_id_raw = query.data.split(":")
    invite_id = int(invite_id_raw)

    invite = await db.get_promote_invite(invite_id)
    if invite is None or invite["status"] != "pending":
        await query.answer("Esta invitación ya no está disponible.", show_alert=True)
        return
    if query.from_user.id != invite["user_id"]:
        await query.answer("Esta invitación no es para vos.", show_alert=True)
        return

    fed = await db.get_fed(invite["fed_id"])
    fed_name = html.escape(fed["name"]) if fed else invite["fed_id"]

    if action == "acc":
        await db.resolve_invite(invite_id, "accepted")
        await db.add_fed_admin(invite["fed_id"], invite["user_id"])
        await query.edit_message_text(
            f"✅ Ahora formás parte de los administradores de la federación <b>{fed_name}</b>.",
            parse_mode="HTML",
        )
        notify = f"✅ {_mention(query.from_user.id, query.from_user.first_name)} aceptó la invitación y ahora administra la federación <b>{fed_name}</b>."
    else:
        await db.resolve_invite(invite_id, "rejected")
        await query.edit_message_text("❌ Rechazaste la invitación.")
        notify = f"❌ {_mention(query.from_user.id, query.from_user.first_name)} rechazó la invitación a administrar la federación <b>{fed_name}</b>."

    if fed and fed["fchat_id"]:
        try:
            await context.bot.send_message(fed["fchat_id"], notify, parse_mode="HTML")
        except TelegramError:
            pass
    await query.answer()


# --------------------------------------------------------------------- #
# /feddemote usuario — le quita la administración de la federación
# --------------------------------------------------------------------- #
async def feddemote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    db = _get_db(context)

    fed_id, rest = await _resolve_fed_context(update, db, list(context.args or []))
    if fed_id is None:
        await message.reply_text(error("Especificá el IDFed o usá este comando dentro de un grupo de la federación."))
        return
    fed = await db.get_fed(fed_id)
    if fed is None:
        await message.reply_text(error(f"No existe ninguna federación con el ID {fed_id}."))
        return
    if not await _require_fed_owner(message, fed, user.id):
        return

    resolved = await resolve_target(update, db, rest)
    if isinstance(resolved, str):
        await message.reply_text(error(resolved))
        return

    if resolved.user_id == fed["owner_id"]:
        await message.reply_text(error("El creador de la federación no puede ser removido."))
        return
    if not await db.is_fed_admin(fed_id, resolved.user_id):
        await message.reply_text(error(f"{resolved.display_name} no es administrador de esta federación."))
        return

    await db.remove_fed_admin(fed_id, resolved.user_id)
    await message.reply_text(
        success(f"{resolved.display_name} ya no es administrador de la federación {fed['name']}.")
    )
    if fed["fchat_id"]:
        try:
            await context.bot.send_message(
                fed["fchat_id"],
                f"➖ {_mention(resolved.user_id, resolved.display_name)} fue removido de los administradores "
                f"de la federación <b>{html.escape(fed['name'])}</b>.",
                parse_mode="HTML",
            )
        except TelegramError:
            pass


# --------------------------------------------------------------------- #
# /import — respondiendo a un .csv
# --------------------------------------------------------------------- #
def _csv_get(row: dict, field_map: dict, *names: str) -> Optional[str]:
    for name in names:
        col = field_map.get(name)
        if col is not None:
            val = row.get(col)
            if val:
                return val.strip()
    return None


async def _apply_import_bans_background(
    context: ContextTypes.DEFAULT_TYPE, *, fed_name: str, group_ids: list[int],
    user_ids: list[int], progress_chat_id: int,
) -> None:
    """Aplica en Telegram (ban_chat_member) cada usuario recién importado
    en cada grupo que ya pertenece a la federación — así, si alguno de
    los importados ya estaba adentro de alguno de esos grupos, queda
    expulsado de verdad y no solo "registrado" en la base. Corre en
    segundo plano (no bloquea la respuesta de /import) con una pausa
    entre cada llamada para no pegarle de golpe a los límites de la API
    de Telegram, y va editando un mensaje de progreso."""
    total = len(user_ids) * len(group_ids)
    if total == 0:
        return

    progress_message = None
    try:
        progress_message = await context.bot.send_message(
            progress_chat_id,
            f"📤 Aplicando {len(user_ids)} baneos importados en {len(group_ids)} grupo(s) "
            f"de <b>{html.escape(fed_name)}</b>...\n0/{total}",
            parse_mode="HTML",
        )
    except TelegramError as exc:
        logger.info("No pude mandar el progreso de la importación: %s", exc)

    applied = 0
    done = 0
    for target_id in user_ids:
        for gid in group_ids:
            done += 1
            try:
                await context.bot.ban_chat_member(gid, target_id)
                applied += 1
            except TelegramError as exc:
                logger.info("No pude aplicar el baneo importado de %s en %s: %s", target_id, gid, exc)

            if progress_message and (done % 25 == 0 or done == total):
                try:
                    await progress_message.edit_text(
                        f"📤 Aplicando baneos importados en los grupos de <b>{html.escape(fed_name)}</b>...\n"
                        f"{done}/{total} — ✅ {applied} aplicados",
                        parse_mode="HTML",
                    )
                except TelegramError:
                    pass
            await asyncio.sleep(0.05)

    if progress_message:
        try:
            await progress_message.edit_text(
                f"✅ Listo — se aplicaron {applied}/{total} baneos importados en los grupos de "
                f"<b>{html.escape(fed_name)}</b>. (Los que no se aplicaron probablemente ya se habían "
                "ido del grupo, o el bot no tiene permiso de restringir miembros ahí.)",
                parse_mode="HTML",
            )
        except TelegramError:
            pass


async def import_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    db = _get_db(context)

    fed_id, _rest = await _resolve_fed_context(update, db, list(context.args or []))
    if fed_id is None:
        await message.reply_text(
            error("Especificá el IDFed (ej. /import FED-XXXXXX, respondiendo al .csv) o usá este comando dentro de un grupo de la federación.")
        )
        return
    fed = await db.get_fed(fed_id)
    if fed is None:
        await message.reply_text(error(f"No existe ninguna federación con el ID {fed_id}."))
        return
    if not (await db.is_fed_admin(fed_id, user.id) or is_owner(user.id)):
        await message.reply_text(error("No sos administrador de esta federación."))
        return

    doc = message.reply_to_message.document if message.reply_to_message else None
    if doc is None or not (doc.file_name or "").lower().endswith(".csv"):
        await message.reply_text(error("Respondé a un archivo .csv con /import."))
        return

    file = await context.bot.get_file(doc.file_id)
    raw = bytes(await file.download_as_bytearray())
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")

    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames
    except csv.Error:
        await message.reply_text(error("No pude leer el CSV, revisá que el formato sea válido."))
        return
    if not fieldnames:
        await message.reply_text(error("El CSV está vacío o no tiene encabezados."))
        return

    field_map = {f.strip().lower(): f for f in fieldnames}
    total_ok = 0
    total_dup = 0
    total_invalid = 0
    newly_banned_ids: list[int] = []

    for row in reader:
        raw_id = _csv_get(row, field_map, "user_id", "userid", "id")
        if not raw_id or not raw_id.lstrip("-").isdigit():
            total_invalid += 1
            continue
        target_id = int(raw_id)
        username = _csv_get(row, field_map, "username", "user")
        first_name = _csv_get(row, field_map, "name", "first_name", "usuario") or username or str(target_id)
        reason = _csv_get(row, field_map, "reason", "motivo")

        ok = await db.add_fed_ban(fed_id, target_id, username, first_name, reason, user.id, imported=True)
        if ok:
            total_ok += 1
            newly_banned_ids.append(target_id)
        else:
            total_dup += 1

    await db.record_fed_import(fed_id, user.id, total_ok)

    lines = ["✅ <b>Importación de baneados exitosa</b>", "", f"Total: {total_ok} usuarios"]
    if total_dup:
        lines.append(f"⚠️ {total_dup} ya estaban baneados en esta federación (omitidos).")
    if total_invalid:
        lines.append(f"⚠️ {total_invalid} filas inválidas (sin ID de usuario reconocible, omitidas).")

    group_ids = await db.get_fed_groups(fed_id)
    if newly_banned_ids and group_ids:
        lines.append(
            f"\n⏳ Aplicando estos baneos en los {len(group_ids)} grupo(s) que ya tiene la federación, "
            "en segundo plano (te aviso acá mismo cuando termine)."
        )
    await message.reply_text("\n".join(lines), parse_mode="HTML")

    if newly_banned_ids and group_ids:
        context.application.create_task(
            _apply_import_bans_background(
                context, fed_name=fed["name"], group_ids=group_ids,
                user_ids=newly_banned_ids, progress_chat_id=message.chat_id,
            )
        )

    if fed["fchat_id"]:
        try:
            await context.bot.send_message(
                fed["fchat_id"],
                f"📥 Se importaron {total_ok} baneados a la federación <b>{html.escape(fed['name'])}</b> "
                f"por {_mention(user.id, user.first_name)}.",
                parse_mode="HTML",
            )
        except TelegramError:
            pass


# --------------------------------------------------------------------- #
# /fbanstat [usuario] [IDFed]
# --------------------------------------------------------------------- #
def _format_fban_detail(row: dict, admin_name: str) -> str:
    origin = "📥 Origen: Importado" if row["imported"] else "📌 Origen: FBAN manual"
    date_str = datetime.fromtimestamp(row["banned_at"]).strftime("%d/%m/%Y %H:%M")
    who = f"@{row['username']}" if row["username"] else (row["first_name"] or str(row["user_id"]))
    return (
        "🚫 <b>Información del FBAN</b>\n\n"
        f"👤 Usuario: {html.escape(who)}\n"
        f"🆔 ID: <code>{row['user_id']}</code>\n"
        f"🛡 Federación: {html.escape(row['fed_name'])}\n"
        f"🔑 IDFed: <code>{row['fed_id']}</code>\n"
        f"📝 Motivo: {html.escape(row['reason']) if row['reason'] else 'Sin especificar'}\n"
        f"👮 Administrador: {html.escape(admin_name)}\n"
        f"📅 Fecha: {date_str}\n\n"
        f"{origin}"
    )


async def fedsync_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/fedsync IDFed — re-aplica TODOS los baneos que ya tiene registrados
    la federación (vengan de /fban o de una importación vieja) en los
    grupos que ya pertenecen a ella. Sirve para el caso de "importé una
    lista antes de que esto se aplicara solo" o "agregué un montón de
    baneos y quiero forzar que se revisen todos los grupos de nuevo".
    No hace falta chequear membresía antes: banear a alguien que no está
    en el grupo no falla ni cuesta más, Telegram simplemente lo deja
    preventivamente bloqueado."""
    message = update.effective_message
    user = update.effective_user
    db = _get_db(context)

    fed_id, _rest = await _resolve_fed_context(update, db, list(context.args or []))
    if fed_id is None:
        await message.reply_text(error("Especificá el IDFed o usá este comando dentro de un grupo de la federación."))
        return
    fed = await db.get_fed(fed_id)
    if fed is None:
        await message.reply_text(error(f"No existe ninguna federación con el ID {fed_id}."))
        return
    if not (await db.is_fed_admin(fed_id, user.id) or is_owner(user.id)):
        await message.reply_text(error("No sos administrador de esta federación."))
        return

    group_ids = await db.get_fed_groups(fed_id)
    if not group_ids:
        await message.reply_text(error("Esta federación todavía no tiene ningún grupo vinculado. Usá /joinfed primero."))
        return

    bans = await db.get_fed_bans(fed_id)
    if not bans:
        await message.reply_text(error("Esta federación no tiene ningún baneo registrado todavía."))
        return

    user_ids = [row["user_id"] for row in bans]
    total = len(user_ids) * len(group_ids)
    await message.reply_text(
        f"⏳ Re-aplicando {len(user_ids)} baneos de la federación en {len(group_ids)} grupo(s) "
        f"({total} llamadas a Telegram en total, con pausa entre cada una). Puede tardar un buen "
        "rato con listas grandes — te aviso el progreso acá mismo."
    )
    context.application.create_task(
        _apply_import_bans_background(
            context, fed_name=fed["name"], group_ids=group_ids,
            user_ids=user_ids, progress_chat_id=message.chat_id,
        )
    )


async def fbanstat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)
    args = list(context.args or [])

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        check = await check_executor_is_admin(context.bot, chat.id, user.id)
        if not check.allowed:
            await message.reply_text(error("Este comando solo puede ser utilizado por administradores."))
            return

    # /fbanstat ID_USUARIO IDFed -> detalle específico de esa federación
    if len(args) >= 2 and args[1].upper().startswith("FED-") and not message.reply_to_message:
        resolved = await resolve_target(update, db, [args[0]])
        if isinstance(resolved, str):
            await message.reply_text(error(resolved))
            return
        fed_id = args[1].upper()
        row = await db.get_fed_ban(fed_id, resolved.user_id)
        if row is None:
            await message.reply_text(error(f"{resolved.display_name} no está baneado en esa federación."))
            return
        admin_name = await db.get_user_display_name(row["banned_by"]) or str(row["banned_by"])
        await message.reply_text(_format_fban_detail(row, admin_name), parse_mode="HTML")
        return

    resolved = await resolve_target(update, db, args)
    if isinstance(resolved, str):
        await message.reply_text(error(resolved))
        return

    rows = await db.get_fed_bans_for_user(resolved.user_id)
    if not rows:
        await message.reply_text(f"✅ {resolved.display_name} no tiene baneos en ninguna federación.")
        return

    if len(rows) == 1:
        row = rows[0]
        admin_name = await db.get_user_display_name(row["banned_by"]) or str(row["banned_by"])
        await message.reply_text(_format_fban_detail(row, admin_name), parse_mode="HTML")
        return

    lines = [
        "🔎 <b>Estado de federación</b>", "",
        f"👤 Usuario: {_mention(resolved.user_id, resolved.display_name)}", "",
        "🚫 Federaciones donde está baneado:",
    ]
    for row in rows:
        lines.append(f"• {html.escape(row['fed_name'])} — <code>{row['fed_id']}</code>")
    await message.reply_text("\n".join(lines), parse_mode="HTML")


# --------------------------------------------------------------------- #
# Chequeo reutilizable para handlers/greetings.py y handlers/join_requests.py
# --------------------------------------------------------------------- #
async def enforce_fed_ban_on_join(context: ContextTypes.DEFAULT_TYPE, group_id: int, user_id: int) -> bool:
    """Si el grupo pertenece a una federación y el usuario está fbaneado
    ahí, lo banea del grupo ahora mismo y devuelve True (para que el
    llamador se salte la bienvenida/aprobación normal). Nunca lanza:
    cualquier error de Telegram al intentar banear queda solo logueado."""
    db: Database = context.application.bot_data["db"]
    fed_id = await db.get_group_fed(group_id)
    if not fed_id:
        return False
    if not await db.is_fed_banned(fed_id, user_id):
        return False
    try:
        await context.bot.ban_chat_member(group_id, user_id)
    except TelegramError as exc:
        logger.info("No pude aplicar el FBAN automático a %s en %s: %s", user_id, group_id, exc)
    return True
