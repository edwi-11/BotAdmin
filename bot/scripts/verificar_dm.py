"""
scripts/verificar_dm.py

Script de mantenimiento, PARA CORRER UNA SOLA VEZ a mano (no lo importa
ni lo llama el bot). Corrige el problema de que miles de usuarios
quedaron con dm_ok = 0 solo porque esa columna se agregó a la tabla
`users` DESPUÉS de que ya existieran, y SQLite le puso 0 por defecto a
todas las filas viejas — sin importar si el bot en realidad sí podía
escribirles.

Qué hace: para cada usuario con dm_ok = 0 (o NULL), le manda un
`send_chat_action` (el indicador de "escribiendo...") en vez de un
mensaje real. Es la forma silenciosa y no invasiva de comprobar si el
bot puede escribirle a alguien: si el usuario nunca bloqueó al bot, esto
funciona sin que le llegue ninguna notificación ni mensaje visible.
Según el resultado:
    - Funciona -> dm_ok = 1 (puede recibir anuncios).
    - Forbidden (lo bloqueó, borró la cuenta, nunca inició el bot en
      realidad) -> se deja en dm_ok = 0.
    - Otro error de Telegram (flood control, etc.) -> se reintenta
      respetando el tiempo de espera que indique la API.

Uso (desde la carpeta bot/, con el venv activado y el bot DETENIDO para
evitar que dos procesos escriban la base al mismo tiempo):

    python scripts/verificar_dm.py

Se puede cortar en cualquier momento con Ctrl+C: ya guardó en la base
todo lo que llevaba procesado hasta ese punto, así que se puede volver a
correr después y sigue con los que faltan (no vuelve a chequear a los
que ya quedaron en dm_ok = 1 o ya se marcaron como bloqueados en esta
misma corrida... salvo que se reinicie el proceso, ver nota más abajo).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telegram import Bot
from telegram.constants import ChatAction
from telegram.error import Forbidden, RetryAfter, TelegramError

from config import load_settings
from database import Database

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("verificar_dm")

# Pausa entre chequeos para no pasarnos del rate limit global de Telegram
# (~30 mensajes/seg a chats distintos). 0.05s = 20/seg, con margen.
DELAY_SECONDS = 0.05
# Cada cuántos usuarios se imprime un resumen de avance.
PROGRESS_EVERY = 200


async def main() -> None:
    settings = load_settings()
    bot = Bot(token=settings.bot_token)
    db = Database(settings.database_path)
    await db.connect()

    cursor = await db.conn.execute(
        "SELECT user_id FROM users WHERE dm_ok = 0 OR dm_ok IS NULL ORDER BY user_id"
    )
    rows = await cursor.fetchall()
    user_ids = [row["user_id"] for row in rows]
    total = len(user_ids)
    logger.info("Voy a reverificar %s usuarios con dm_ok = 0/NULL.", total)

    recuperados = 0
    bloqueados = 0
    errores = 0

    for i, user_id in enumerate(user_ids, start=1):
        while True:
            try:
                await bot.send_chat_action(user_id, ChatAction.TYPING)
                await db.set_dm_ok(user_id, True)
                recuperados += 1
                break
            except Forbidden:
                # Bloqueó al bot, borró la cuenta, o nunca abrió un chat
                # de verdad -> se queda en dm_ok = 0, que ya es el valor
                # actual, no hace falta tocar nada.
                bloqueados += 1
                break
            except RetryAfter as exc:
                logger.warning("Rate limit, esperando %.1fs...", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 0.5)
                continue  # reintenta el mismo user_id
            except TelegramError as exc:
                logger.warning("Error con %s: %s (se deja como estaba)", user_id, exc)
                errores += 1
                break

        if i % PROGRESS_EVERY == 0 or i == total:
            logger.info(
                "Progreso: %s/%s | recuperados=%s | bloqueados=%s | errores=%s",
                i, total, recuperados, bloqueados, errores,
            )

        await asyncio.sleep(DELAY_SECONDS)

    logger.info(
        "Listo. Total procesados=%s | recuperados (dm_ok=1 ahora)=%s | "
        "bloqueados/inaccesibles=%s | errores puntuales=%s",
        total, recuperados, bloqueados, errores,
    )
    await db.conn.close()


if __name__ == "__main__":
    asyncio.run(main())
