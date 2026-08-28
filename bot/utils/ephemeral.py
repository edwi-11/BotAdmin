"""
utils/ephemeral.py
Mensajes efímeros en grupos (novedad de Bot API 10.1, junio 2026): permiten
que el bot mande un mensaje dentro de un grupo que SOLO ve una persona en
particular (ni el resto del grupo, ni queda en el historial general de
nadie más).

python-telegram-bot 22.8 (el que usa este bot, ver requirements.txt) todavía
no trae wrappers nativos para esto -- la librería cubre oficialmente hasta
Bot API 10.0, y los mensajes efímeros llegaron en la 10.1/10.2. Por eso acá
se llama al endpoint HTTP de Telegram directamente con httpx (que el bot ya
usa como dependencia para Gemini/Groq), en vez de esperar a que
python-telegram-bot lo soporte.

Si en algún momento python-telegram-bot agrega soporte nativo (con un
parámetro `ephemeral_message_parameters` en `bot.send_message`), lo ideal
sería migrar a eso y borrar este archivo.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.telegram.org/bot{token}"
_TIMEOUT = httpx.Timeout(15.0)


async def send_ephemeral_notice(
    bot_token: str,
    chat_id: int,
    receiver_user_id: int,
    text: str,
    *,
    parse_mode: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Manda un mensaje efímero en `chat_id` que solo puede ver
    `receiver_user_id` (y el bot). Piensa en esto como un "susurro" dentro
    del grupo: nadie más lo ve ni queda registrado en su historial.

    Devuelve el `Message` (como dict) que contestó Telegram si salió bien,
    o `None` si algo falló (por ejemplo, si el servidor de Bot API en uso
    todavía no soporta esta función). El caller debería tener un plan B
    (mandar un mensaje normal) para cuando esto devuelve `None`, ya que la
    función es deliberadamente "silenciosa" en caso de error para no
    romper el flujo de moderación del bot.
    """
    url = f"{_API_BASE.format(token=bot_token)}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "ephemeral_message_parameters": {"receiver_user_id": receiver_user_id},
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo mandar el mensaje efímero (red/parseo): %s", exc)
        return None

    if not data.get("ok"):
        # Motivos típicos: servidor de Bot API desactualizado (self-hosted),
        # o el usuario ya no está en el chat. No es un error fatal para el
        # bot, así que solo lo dejamos en el log.
        logger.info("sendMessage efímero rechazado por Telegram: %s", data.get("description"))
        return None

    return data.get("result")
