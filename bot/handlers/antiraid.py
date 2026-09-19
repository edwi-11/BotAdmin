"""
handlers/antiraid.py
Protección contra expulsiones masivas ("kick all"): detecta cuando una
misma persona saca a muchos miembros en pocos segundos, corta el ataque
lo antes posible y deja registrado a quién sacaron para poder
reinvitarlos después.

Cómo detecta:
    Telegram manda un update `chat_member` por CADA cambio de estado de
    CADA miembro, y — esto es lo importante — incluye `from_user`: quién
    provocó ese cambio. Así se puede distinguir "se fue solo" de "lo
    sacó fulano", y contar cuántas bajas lleva cada autor.

    Si un mismo autor acumula `threshold` expulsiones dentro de
    `window_secs` segundos, se considera raid.

Qué hace al detectarlo:
 1. Intenta DEGRADAR al atacante en el momento (quitarle todos los
    permisos de admin), que es lo único que corta el ataque de verdad.
 2. Avisa en el grupo y por privado a los propietarios del bot, con el
    nombre y el ID del atacante.
 3. Deja registradas todas las bajas para que después se pueda usar
    /recuperar y obtener la lista de quiénes fueron sacados.

Límite REAL e importante (de Telegram, no del bot): un bot solo puede
degradar a administradores que ese mismo bot promovió. Si al atacante lo
promovió una persona (o es el creador del grupo), Telegram NO deja que el
bot lo degrade, por más permisos que tenga. En ese caso el bot avisa igual
al instante y registra todo, pero frenarlo tiene que hacerlo un humano.
Por eso el aviso incluye qué hacer a mano.

Comandos:
    /antiraid            — ver estado y configuración
    /antiraid on|off     — activar/desactivar
    /antiraid <número>   — cambiar el umbral de expulsiones
    /recuperar [horas]   — lista de los sacados (por defecto, últimas 24h)
"""
from __future__ import annotations

import html
import logging
import time

from telegram import ChatMemberUpdated, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import settings
from database import Database
from utils.formatting import error, success
from utils.permissions import check_executor_is_admin, is_owner

logger = logging.getLogger(__name__)

# Estados que significan "ya no está en el grupo".
_GONE_STATUSES = (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)

# Para no mandar 40 alertas seguidas durante el mismo ataque: una alerta
# por (grupo, atacante) cada _ALERT_COOLDOWN segundos.
_ALERT_COOLDOWN = 300
_ALERT_KEY = "antiraid_last_alert"


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _mention(user_id: int, name: str | None) -> str:
    return f'<a href="tg://user?id={user_id}">{html.escape(name or str(user_id))}</a>'


async def on_member_removed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler de ChatMemberHandler.CHAT_MEMBER: mira cada baja de miembro
    y decide si estamos ante un raid."""
    result: ChatMemberUpdated | None = update.chat_member
    if result is None:
        return

    chat = result.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status
    victim = result.new_chat_member.user
    actor = result.from_user

    # Solo nos interesan las BAJAS (estaba adentro y ahora no).
    if new_status not in _GONE_STATUSES or old_status in _GONE_STATUSES:
        return
    # Si se fue por su cuenta, no es una expulsión.
    if actor is None or actor.id == victim.id:
        return
    # Si el que sacó fue el propio bot (por /ban, /kick, warns, fban...),
    # es moderación legítima nuestra, no un ataque.
    if actor.id == context.bot.id:
        return

    db = _get_db(context)
    row = await db.get_antiraid_settings(chat.id)
    # Por defecto viene ACTIVADO aunque el grupo nunca lo haya configurado:
    # una protección que hay que acordarse de encender no sirve de nada
    # justo el día que pasa el ataque.
    if row is not None and not row["enabled"]:
        return
    threshold = row["threshold"] if row else 5
    window_secs = row["window_secs"] if row else 30
    action = row["action"] if row else "demote"

    await db.record_removal(
        chat.id, actor.id, actor.first_name, victim.id, victim.first_name, victim.username,
    )

    recientes = await db.count_removals_by_actor(chat.id, actor.id, int(time.time()) - window_secs)
    if recientes < threshold:
        return

    # --- Es un raid ---
    cooldowns: dict[tuple[int, int], float] = context.application.bot_data.setdefault(_ALERT_KEY, {})
    key = (chat.id, actor.id)
    now = time.time()
    if now - cooldowns.get(key, 0) < _ALERT_COOLDOWN:
        return  # ya avisamos hace poco por este mismo atacante
    cooldowns[key] = now

    logger.warning(
        "RAID detectado en %s (%s): %s (%s) sacó a %s miembros en %ss",
        chat.title, chat.id, actor.first_name, actor.id, recientes, window_secs,
    )

    demoted = False
    demote_error = ""
    if action == "demote":
        try:
            await context.bot.promote_chat_member(
                chat.id, actor.id,
                can_change_info=False, can_delete_messages=False, can_invite_users=False,
                can_restrict_members=False, can_pin_messages=False, can_promote_members=False,
                can_manage_chat=False, can_manage_video_chats=False,
            )
            demoted = True
        except TelegramError as exc:
            demote_error = str(exc)
            logger.warning("No pude degradar al atacante %s en %s: %s", actor.id, chat.id, exc)

    if demoted:
        estado = "✅ Le quité todos los permisos de administrador."
    else:
        estado = (
            "⚠️ <b>No pude quitarle los permisos.</b> Telegram solo me deja degradar a "
            "administradores que yo mismo promoví; a este lo promovió una persona (o es el "
            "creador del grupo). <b>Hay que sacarlo a mano, ya mismo:</b>\n"
            "Info del grupo → Administradores → tocar su nombre → quitarle el cargo."
        )
        if demote_error:
            estado += f"\n<i>(Detalle: {html.escape(demote_error)})</i>"

    alerta = (
        "🚨 <b>EXPULSIONES MASIVAS DETECTADAS</b>\n\n"
        f"👤 Responsable: {_mention(actor.id, actor.first_name)}\n"
        f"🆔 ID: <code>{actor.id}</code>\n"
        f"📊 Sacó a <b>{recientes}</b> miembros en {window_secs} segundos.\n\n"
        f"{estado}\n\n"
        "📋 Guardé la lista de todos los que sacó: usá <code>/recuperar</code> en el grupo "
        "para verla y poder reinvitarlos."
    )

    try:
        await context.bot.send_message(chat.id, alerta, parse_mode="HTML")
    except TelegramError as exc:
        logger.warning("No pude avisar del raid en el grupo %s: %s", chat.id, exc)

    encabezado = f"🚨 Raid en <b>{html.escape(chat.title or str(chat.id))}</b>\n\n"
    for owner_id in settings.owner_ids:
        try:
            await context.bot.send_message(owner_id, encabezado + alerta, parse_mode="HTML")
        except TelegramError:
            pass


# --------------------------------------------------------------------- #
# /antiraid
# --------------------------------------------------------------------- #
async def antiraid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text(error("Este comando se usa dentro del grupo."))
        return

    check = await check_executor_is_admin(context.bot, chat.id, user.id)
    if not check.allowed and not is_owner(user.id):
        await message.reply_text(error("Solo los administradores pueden tocar el antiraid."))
        return

    args = context.args or []
    if args:
        arg = args[0].lower()
        if arg in ("on", "si", "sí", "activar"):
            await db.set_antiraid(chat.id, enabled=True)
            await message.reply_text(success("Antiraid activado."))
            return
        if arg in ("off", "no", "desactivar"):
            await db.set_antiraid(chat.id, enabled=False)
            await message.reply_text(
                success("Antiraid desactivado.") +
                "\n\n⚠️ Sin esto no voy a detectar expulsiones masivas en este grupo."
            )
            return
        if arg.isdigit():
            n = int(arg)
            if not 2 <= n <= 50:
                await message.reply_text(error("El umbral tiene que estar entre 2 y 50."))
                return
            await db.set_antiraid(chat.id, threshold=n)
            await message.reply_text(success(f"Umbral cambiado: aviso cuando alguien saque a {n} o más en poco tiempo."))
            return
        await message.reply_text(error("Usá /antiraid on, /antiraid off, o /antiraid <número> para el umbral."))
        return

    row = await db.get_antiraid_settings(chat.id)
    enabled = bool(row["enabled"]) if row else True
    threshold = row["threshold"] if row else 5
    window_secs = row["window_secs"] if row else 30

    await message.reply_text(
        "🛡 <b>Antiraid</b>\n\n"
        f"Estado: {'✅ Activado' if enabled else '❌ Desactivado'}\n"
        f"Umbral: <b>{threshold}</b> expulsiones en <b>{window_secs}</b> segundos\n\n"
        "Si alguien supera eso, le quito los permisos de administrador (cuando puedo), "
        "aviso acá y al propietario, y guardo la lista de los expulsados para "
        "poder reinvitarlos con /recuperar.\n\n"
        "<i>/antiraid on · /antiraid off · /antiraid &lt;número&gt;</i>",
        parse_mode="HTML",
    )


# --------------------------------------------------------------------- #
# /recuperar
# --------------------------------------------------------------------- #
async def recuperar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text(error("Este comando se usa dentro del grupo."))
        return

    check = await check_executor_is_admin(context.bot, chat.id, user.id)
    if not check.allowed and not is_owner(user.id):
        await message.reply_text(error("Solo los administradores pueden ver esta lista."))
        return

    horas = 24
    if context.args and context.args[0].isdigit():
        horas = max(1, min(720, int(context.args[0])))

    rows = await db.get_recent_removals(chat.id, int(time.time()) - horas * 3600)
    if not rows:
        await message.reply_text(f"No registré ninguna expulsión en las últimas {horas} horas.")
        return

    # Agrupamos por responsable, que es lo que realmente querés ver.
    por_actor: dict[int, list] = {}
    nombres: dict[int, str] = {}
    for row in rows:
        por_actor.setdefault(row["actor_id"], []).append(row)
        nombres[row["actor_id"]] = row["actor_name"] or str(row["actor_id"])

    lineas = [f"📋 <b>Expulsiones de las últimas {horas}h</b> ({len(rows)} en total)\n"]
    for actor_id, quitados in sorted(por_actor.items(), key=lambda kv: -len(kv[1])):
        lineas.append(
            f"\n👤 Por {_mention(actor_id, nombres[actor_id])} (<code>{actor_id}</code>) — "
            f"<b>{len(quitados)}</b>:"
        )
        for row in quitados[:40]:
            quien = f"@{row['victim_user']}" if row["victim_user"] else (row["victim_name"] or "")
            lineas.append(f"  • {html.escape(quien)} <code>{row['victim_id']}</code>")
        if len(quitados) > 40:
            lineas.append(f"  <i>…y {len(quitados) - 40} más.</i>")

    texto = "\n".join(lineas)
    # Telegram corta a 4096; mandamos por partes si hace falta.
    for i in range(0, len(texto), 3500):
        await message.reply_text(texto[i:i + 3500], parse_mode="HTML")
