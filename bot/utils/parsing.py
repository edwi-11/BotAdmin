"""
utils/parsing.py
Resuelve el usuario objetivo de un comando a partir de:
- Respuesta a un mensaje
- @username (requiere que el usuario haya sido visto antes por el bot)
- ID numérico de Telegram

También separa los argumentos restantes (duración / motivo) según el comando.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from telegram import Update

from database import Database


@dataclass(slots=True)
class ResolvedTarget:
    user_id: int
    display_name: str
    username: Optional[str]
    remaining_args: list[str]


async def resolve_target(update: Update, db: Database, args: list[str]) -> Optional[ResolvedTarget] | str:
    """
    Intenta resolver al usuario objetivo.
    Devuelve:
        - ResolvedTarget si tiene éxito
        - str con un mensaje de error si falla
    """
    message = update.effective_message
    assert message is not None

    # 1) Prioridad: respuesta a un mensaje
    if message.reply_to_message and message.reply_to_message.from_user:
        target_user = message.reply_to_message.from_user
        await db.upsert_user(target_user.id, target_user.username, target_user.first_name)
        return ResolvedTarget(
            user_id=target_user.id,
            display_name=target_user.first_name,
            username=target_user.username,
            remaining_args=args,
        )

    # 2) Primer argumento: @username o ID
    if not args:
        return (
            "Debes indicar un usuario respondiendo a su mensaje, "
            "mencionándolo con @usuario o indicando su ID."
        )

    token, *rest = args

    if token.startswith("@"):
        user_id = await db.get_user_id_by_username(token)
        if user_id is None:
            return (
                f"No encuentro a {token} en mi base de datos. "
                "El usuario debe haber escrito al menos un mensaje en el grupo "
                "para que pueda identificarlo, o usa su ID / responde a su mensaje."
            )
        display_name = await db.get_user_display_name(user_id) or token
        return ResolvedTarget(
            user_id=user_id,
            display_name=display_name,
            username=token.lstrip("@"),
            remaining_args=rest,
        )

    if token.lstrip("-").isdigit():
        user_id = int(token)
        display_name = await db.get_user_display_name(user_id) or str(user_id)
        return ResolvedTarget(
            user_id=user_id,
            display_name=display_name,
            username=None,
            remaining_args=rest,
        )

    return (
        "Formato de usuario no reconocido. Usa @usuario, un ID numérico, "
        "o responde al mensaje del usuario."
    )
