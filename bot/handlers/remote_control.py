"""
handlers/remote_control.py
/owner  — el propietario elige, con botones, uno de los grupos donde está
           el bot. A partir de ahí, mientras el "modo remoto" siga activo,
           CUALQUIER comando que el propietario le mande al bot por privado
           se ejecuta como si lo hubiera escrito dentro de ese grupo, y su
           efecto (banear, mutear, fijar, mandar mensajes, /q, lo que sea)
           ocurre ahí — sin que el propietario tenga que estar escribiendo
           en el grupo.
           También se puede saltear los botones pasando el ID del grupo
           directo: /owner -1001234567890
/ready  — apaga el modo remoto. Los comandos por privado vuelven a
           contestarte solo a vos, como siempre.

Cómo funciona por dentro:
- /owner guarda en memoria (bot_data, por id de propietario) qué grupo
  eligió cada uno.
- Un MessageHandler de MÁXIMA prioridad (group=-10, antes que cualquier
  otro handler) revisa cada mensaje privado del propietario: si el modo
  remoto está activo y el mensaje es un comando (que no sea /owner,
  /ready o /start), arma una copia del Update con el chat cambiado al
  grupo elegido y la vuelve a pasar por la Application como si fuera un
  mensaje nuevo — así el handler normal de ese comando (ban, mute, warn,
  send, /q, el panel /menu, etc.) corre exactamente igual que si lo
  hubieras mandado ahí adentro, con efecto real en el grupo.
- Se corta la propagación del update original (ApplicationHandlerStop)
  para que nada más intente procesar ese mismo mensaje en el chat privado.

Limitación: el modo remoto vive en memoria, no en la base de datos — si
el bot se reinicia, se apaga solo y hay que volver a elegir grupo con
/owner.
"""
from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, MessageEntityType
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

from database import Database
from utils.callbacks import safe_callback
from utils.formatting import error, success
from utils.groups import get_verified_groups
from utils.permissions import is_owner

logger = logging.getLogger(__name__)

_REMOTE_KEY = "remote_targets"  # bot_data[_REMOTE_KEY]: {owner_id: (group_id, group_title)}
# Comandos que NUNCA se reenvían (tienen que seguir funcionando en el chat
# privado tal cual, para poder prender/apagar el modo remoto o hablar con
# el bot normalmente).
_EXEMPT_COMMANDS = {"owner", "ready", "start"}


def _targets(context: ContextTypes.DEFAULT_TYPE) -> dict[int, tuple[int, str]]:
    return context.application.bot_data.setdefault(_REMOTE_KEY, {})


async def _activate_remote(
    update: Update, context: ContextTypes.DEFAULT_TYPE, group_id: int
) -> None:
    """Activa el modo remoto directo sobre group_id, verificando primero
    contra Telegram que el bot sigue siendo miembro de ese chat y que es
    realmente un grupo (no un canal ni un privado)."""
    message = update.effective_message
    user = update.effective_user

    try:
        chat = await context.bot.get_chat(group_id)
    except TelegramError as exc:
        await message.reply_text(
            error(f"No pude acceder al grupo {group_id}: {exc}. Revisá el ID con /grupos.")
        )
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.reply_text(error(f"{group_id} no es un grupo (es {chat.type}), no puedo activar el modo remoto ahí."))
        return

    title = chat.title or f"Grupo {group_id}"
    _targets(context)[user.id] = (group_id, title)
    await message.reply_text(
        f"🎮 <b>Modo remoto activado</b> en <b>{title}</b>.\n\n"
        "A partir de ahora, cualquier comando que me mandes por acá (privado) "
        "se va a ejecutar ahí adentro: banear, mutear, fijar, /q, lo que "
        "necesites.\n\n"
        "Escribí /ready cuando quieras dejar de usarlo.",
        parse_mode="HTML",
    )


# --------------------------------------------------------------------- #
# /owner — elegir grupo
# --------------------------------------------------------------------- #
async def owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message

    if not is_owner(user.id):
        await message.reply_text(error("Este comando es solo para el propietario del bot."))
        return

    # Atajo: /owner -1001234567890 activa el modo remoto directo en ese
    # grupo, sin pasar por los botones.
    if context.args:
        raw = context.args[0].strip()
        try:
            group_id = int(raw)
        except ValueError:
            await message.reply_text(
                error(f"'{raw}' no es un ID de grupo válido. Tiene que ser un número (lo sacás de /grupos).")
            )
            return
        await _activate_remote(update, context, group_id)
        return

    db: Database = context.application.bot_data["db"]
    groups = await get_verified_groups(context.bot, db)
    if not groups:
        await message.reply_text("No tengo registrado ningún grupo todavía.")
        return

    buttons = [
        [InlineKeyboardButton(f"👥 {title or f'Grupo {gid}'}", callback_data=f"remote:{gid}")]
        for gid, title in groups
    ]
    active = _targets(context).get(user.id)
    header = (
        f"🎮 Ahora mismo estás controlando <b>{active[1]}</b>.\n\n" if active else ""
    )
    await message.reply_text(
        f"{header}🎮 <b>Modo remoto</b>\nElegí en qué grupo querés que actúen tus próximos comandos "
        "(o mandá /owner &lt;id_del_grupo&gt; directo la próxima vez):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@safe_callback
async def owner_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user

    if not is_owner(user.id):
        await query.answer("🔒 Solo el propietario puede usar esto.", show_alert=True)
        return

    group_id = int(query.data.split(":", 1)[1])
    db: Database = context.application.bot_data["db"]
    title = await db.get_group_title(group_id) or f"Grupo {group_id}"

    _targets(context)[user.id] = (group_id, title)
    await query.answer("Modo remoto activado ✅")
    await query.edit_message_text(
        f"🎮 <b>Modo remoto activado</b> en <b>{title}</b>.\n\n"
        "A partir de ahora, cualquier comando que me mandes por acá (privado) "
        "se va a ejecutar ahí adentro: banear, mutear, fijar, /q, lo que "
        "necesites.\n\n"
        "Escribí /ready cuando quieras dejar de usarlo.",
        parse_mode="HTML",
    )


# --------------------------------------------------------------------- #
# /ready — apagar el modo remoto
# --------------------------------------------------------------------- #
async def ready_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.effective_message

    if not is_owner(user.id):
        await message.reply_text(error("Este comando es solo para el propietario del bot."))
        return

    targets = _targets(context)
    if user.id not in targets:
        await message.reply_text("El modo remoto ya estaba apagado.")
        return

    _, title = targets.pop(user.id)
    await message.reply_text(success(f"Modo remoto desactivado. Ya no vas a controlar {title}."))


# --------------------------------------------------------------------- #
# Reenvío de comandos al grupo elegido
# --------------------------------------------------------------------- #
async def remote_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if message is None or user is None or not is_owner(user.id):
        return

    target = _targets(context).get(user.id)
    if target is None:
        return

    command_entity = next(
        (
            e
            for e in (message.entities or [])
            if e.type == MessageEntityType.BOT_COMMAND and e.offset == 0
        ),
        None,
    )
    if command_entity is None:
        return

    command_text = message.text[command_entity.offset + 1 : command_entity.offset + command_entity.length]
    command_name = command_text.split("@")[0].lower()
    if command_name in _EXEMPT_COMMANDS:
        return

    group_id, group_title = target

    try:
        target_chat = await context.bot.get_chat(group_id)
    except TelegramError as exc:
        await message.reply_text(
            error(f"No pude acceder a {group_title}: {exc}. Puede que ya no esté ahí. Usá /owner de nuevo.")
        )
        raise ApplicationHandlerStop

    # Reconstruimos el mensaje tal cual, pero "sucediendo" en el grupo
    # elegido en vez del chat privado, y volvemos a inyectarlo en la
    # Application como si fuera un mensaje entrante normal. Así el handler
    # real del comando (ban, mute, /q, el que sea) corre sin cambios y
    # actúa sobre el grupo.
    fake_message_dict = message.to_dict()
    fake_message_dict["chat"] = target_chat.to_dict()
    fake_update = Update.de_json({"update_id": update.update_id, "message": fake_message_dict}, context.bot)

    try:
        await context.application.process_update(fake_update)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error reenviando comando remoto (%s -> %s): %s", command_name, group_id, exc)
        await message.reply_text(error(f"No pude ejecutar /{command_name} en {group_title}: {exc}"))
        raise ApplicationHandlerStop

    try:
        await message.reply_text(f"✅ /{command_name} ejecutado en 🎯 {group_title}.")
    except TelegramError:
        pass

    raise ApplicationHandlerStop
