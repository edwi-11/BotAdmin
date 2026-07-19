"""
handlers/donations.py
/donar — permite donarle Telegram Stars (⭐) al bot.

Las Stars son la moneda nativa de pagos de Telegram: no hace falta ninguna
pasarela ni token de proveedor (`provider_token=""`), y Telegram se encarga
de todo el checkout dentro de la propia app. El bot solo tiene que:

    1) Mostrar botones con montos sugeridos (/donar).
    2) Al tocar un monto, enviar una factura (`send_invoice`) por esa
       cantidad de Stars.
    3) Responder `ok=True` al `pre_checkout_query` (Telegram lo manda justo
       antes de cobrar, hay que contestar en <10s).
    4) Cuando el pago se confirma, Telegram entrega un mensaje especial
       `successful_payment` — ahí se agradece y se deja registro.

Los montos son en Stars enteras (no llevan decimales/centavos como una
moneda real).
"""
from __future__ import annotations

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from database import Database

logger = logging.getLogger(__name__)

# Montos sugeridos (en Stars). Se pueden cambiar libremente.
_AMOUNTS = [20, 50, 100, 500, 1000]

_PAYLOAD_PREFIX = "donar"


def _amounts_keyboard() -> InlineKeyboardMarkup:
    buttons = [InlineKeyboardButton(f"⭐ {amount}", callback_data=f"{_PAYLOAD_PREFIX}:{amount}") for amount in _AMOUNTS]
    # 3 botones en la primera fila, el resto en la segunda.
    rows = [buttons[:3], buttons[3:]]
    rows = [row for row in rows if row]
    return InlineKeyboardMarkup(rows)


async def donar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    await message.reply_text(
        "⭐ <b>Apoya al bot con Telegram Stars</b>\n\n"
        "Elige cuántas estrellas quieres donar. El pago se hace directamente "
        "dentro de Telegram, no se comparte ningún dato tuyo.",
        parse_mode="HTML",
        reply_markup=_amounts_keyboard(),
    )


async def donar_amount_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return

    logger.info("donar_amount_callback disparado con data=%r", query.data)

    try:
        amount = int(query.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("⚠️ Monto inválido.", show_alert=True)
        return

    if amount not in _AMOUNTS:
        await query.answer("⚠️ Monto inválido.", show_alert=True)
        return

    await query.answer()

    chat_id = update.effective_chat.id
    try:
        await context.bot.send_invoice(
            chat_id=chat_id,
            title=f"Donación de {amount} ⭐",
            description="¡Gracias por apoyar al bot! Tu donación ayuda a mantenerlo funcionando.",
            payload=f"{_PAYLOAD_PREFIX}:{amount}:{query.from_user.id}",
            provider_token="",  # vacío: obligatorio para pagos en Telegram Stars (XTR)
            currency="XTR",
            prices=[LabeledPrice(f"{amount} Stars", amount)],
        )
    except Exception as exc:  # noqa: BLE001 — atrapamos TODO para que nunca falle en silencio
        logger.exception("Error enviando la factura de donación (%s Stars): %s", amount, exc)
        try:
            await context.bot.send_message(
                chat_id,
                f"⚠️ No pude generar la donación: <code>{type(exc).__name__}: {exc}</code>",
                parse_mode="HTML",
            )
        except TelegramError:
            pass


async def donar_precheckout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if query is None:
        return
    if not query.invoice_payload.startswith(f"{_PAYLOAD_PREFIX}:"):
        await query.answer(ok=False, error_message="Factura inválida.")
        return
    await query.answer(ok=True)


async def donar_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    payment = message.successful_payment
    if payment is None:
        return

    user = update.effective_user
    db: Database = context.application.bot_data["db"]
    await db.log_donation(
        user_id=user.id,
        name=user.first_name or user.username or "Usuario",
        username=user.username,
        chat_id=update.effective_chat.id if update.effective_chat else None,
        amount=payment.total_amount,  # las Stars no tienen sub-unidad, es el monto real
        charge_id=payment.telegram_payment_charge_id,
    )

    total = await db.get_total_donated_by(user.id)
    await message.reply_text(
        f"🎉 ¡Gracias por tu donación de <b>{payment.total_amount} ⭐</b>!\n"
        f"Llevas donadas <b>{total} ⭐</b> en total. ¡Se aprecia muchísimo! 💛",
        parse_mode="HTML",
    )
