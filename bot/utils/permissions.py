"""
utils/permissions.py
Centraliza toda la lógica de permisos:
- Verificación de propietario (owner)
- Verificación de que el bot tiene los permisos necesarios
- Verificación de que el ejecutor es administrador
- Reglas de "quién puede moderar a quién"
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from telegram import Bot, ChatMember
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

from config import settings
from database import Database

REQUIRED_BOT_RIGHTS = {
    "can_restrict_members": "restringir miembros (mute/ban)",
    "can_delete_messages": "eliminar mensajes",
    "can_promote_members": "promover administradores",
    "can_invite_users": "invitar usuarios",
}


def is_owner(user_id: int) -> bool:
    return user_id in settings.owner_ids


@dataclass(slots=True)
class PermissionResult:
    allowed: bool
    reason: str = ""


async def get_member(bot: Bot, chat_id: int, user_id: int) -> Optional[ChatMember]:
    try:
        return await bot.get_chat_member(chat_id, user_id)
    except TelegramError:
        return None


async def check_bot_rights(bot: Bot, chat_id: int) -> PermissionResult:
    """Verifica que el bot sea administrador con los permisos necesarios."""
    me = await bot.get_me()
    member = await get_member(bot, chat_id, me.id)
    if member is None or member.status != ChatMemberStatus.ADMINISTRATOR:
        return PermissionResult(
            False, "El bot no es administrador de este grupo. Otórgale permisos de administrador."
        )

    missing = []
    for attr, human in REQUIRED_BOT_RIGHTS.items():
        if not getattr(member, attr, False):
            missing.append(human)

    if missing:
        return PermissionResult(
            False,
            "Al bot le faltan los siguientes permisos de administrador: " + ", ".join(missing) + ".",
        )
    return PermissionResult(True)


def _has_change_info_permission(member: Optional[ChatMember]) -> bool:
    """Requisito ESTRICTO, usado solo para el panel de configuración
    (/menu, /start): el creador del grupo siempre califica; un
    administrador normal solo si el dueño del grupo le dio el permiso
    'Cambiar info del grupo' (can_change_info)."""
    if member is None:
        return False
    if member.status == ChatMemberStatus.OWNER:
        return True
    if member.status == ChatMemberStatus.ADMINISTRATOR:
        return bool(getattr(member, "can_change_info", False))
    return False


def _is_any_admin(member: Optional[ChatMember]) -> bool:
    """Requisito AMPLIO, usado para los comandos normales de moderación
    (/ban, /kick, /mute, /unmute, /warn, /unwarn, /unban, /delban,
    /delkick, /delwarn): cualquier administrador real de Telegram
    (owner o admin, sin importar qué permisos puntuales tenga)."""
    if member is None:
        return False
    return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR)


async def is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Requisito ESTRICTO (can_change_info). Lo usan los paneles de
    configuración por botones (menú, palabras prohibidas, mensajes
    recurrentes, advertencias, auto-eliminar), NO los comandos normales
    de moderación."""
    if is_owner(user_id):
        return True
    member = await get_member(bot, chat_id, user_id)
    return _has_change_info_permission(member)


async def is_real_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Requisito AMPLIO: cualquier administrador real de Telegram (o el
    propietario del bot). Lo usa can_moderate() para decidir si el
    objetivo de una sanción es "otro admin" (y por lo tanto intocable
    para un admin normal)."""
    if is_owner(user_id):
        return True
    member = await get_member(bot, chat_id, user_id)
    return _is_any_admin(member)


async def check_executor_is_admin(bot: Bot, chat_id: int, user_id: int) -> PermissionResult:
    """Usado por los comandos de moderación: acepta a cualquier
    administrador real del grupo (no exige el permiso puntual de
    'Cambiar info del grupo'). Para el panel /menu se usa
    check_menu_access, que sí exige ese permiso."""
    if is_owner(user_id):
        return PermissionResult(True)
    member = await get_member(bot, chat_id, user_id)
    if _is_any_admin(member):
        return PermissionResult(True)
    return PermissionResult(False, "No tienes permisos de administrador para usar este comando.")


async def check_menu_access(bot: Bot, chat_id: int, user_id: int) -> PermissionResult:
    """Usado EXCLUSIVAMENTE para el panel de configuración (/menu, /start):
    exige el permiso puntual 'Cambiar info del grupo' (o ser el
    propietario del bot). Es deliberadamente más estricto que
    check_executor_is_admin, que ahora acepta a cualquier admin."""
    if is_owner(user_id):
        return PermissionResult(True)
    member = await get_member(bot, chat_id, user_id)
    if _has_change_info_permission(member):
        return PermissionResult(True)
    if member is not None and member.status == ChatMemberStatus.ADMINISTRATOR:
        return PermissionResult(
            False,
            "Eres administrador del grupo, pero para usar el menú del bot el dueño del grupo "
            "debe darte el permiso «Cambiar info del grupo» (Change group info).",
        )
    return PermissionResult(False, "No tienes permisos de administrador para usar este comando.")


async def can_moderate(bot: Bot, chat_id: int, executor_id: int, target_id: int) -> PermissionResult:
    """
    Reglas:
    - Nadie puede moderar al propietario.
    - El propietario puede moderar a cualquiera (incluidos otros administradores).
    - Un administrador normal NO puede moderar a otro administrador ni al propietario.
    - Un administrador normal SÍ puede moderar a miembros comunes.
    """
    if is_owner(target_id):
        return PermissionResult(False, "No se puede moderar al propietario del bot.")

    if target_id == executor_id:
        return PermissionResult(False, "No puedes moderarte a ti mismo.")

    if is_owner(executor_id):
        return PermissionResult(True)

    executor_check = await check_executor_is_admin(bot, chat_id, executor_id)
    if not executor_check.allowed:
        return executor_check

    target_is_admin = await is_real_admin(bot, chat_id, target_id)
    if target_is_admin:
        return PermissionResult(
            False, "Solo el propietario puede moderar o administrar a otros administradores."
        )

    return PermissionResult(True)


async def can_grant_admin(bot: Bot, chat_id: int, executor_id: int, target_id: int) -> PermissionResult:
    """Solo el propietario puede otorgar o revocar administración."""
    if is_owner(target_id):
        return PermissionResult(False, "El propietario ya tiene control total; no aplica.")
    if not is_owner(executor_id):
        return PermissionResult(False, "Solo el propietario puede otorgar o revocar administración.")
    return PermissionResult(True)
