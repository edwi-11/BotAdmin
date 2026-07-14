"""
handlers/economy.py
Sistema completo de economía del grupo: monedas, juegos diarios con dados
animados de Telegram, empleos, robos, banco, tienda y ranking.

Todo el saldo es POR GRUPO (igual que warnings/freed_users): las monedas de
un usuario en un grupo no se comparten con otro grupo.

Comandos:
    /saldo (alias /perfil, /economia)   — ver perfil económico
    /diario                              — bono diario con racha
    /baloncesto /futbol /dardos /bolos /tragamonedas
                                          — 1 tirada gratis al día c/u
    /trabajos                            — ver empleos disponibles
    /trabajo <clave>                     — elegir empleo
    /renunciar                           — dejar el empleo actual
    /cobrar                              — cobrar el sueldo cuando esté listo
    /robar (respuesta | @user | ID)      — intentar robarle monedas a alguien
    /transferir (respuesta | @user) cant — enviar monedas a otro usuario
    /depositar <cant|todo>               — guardar monedas en el banco
    /retirar <cant|todo>                 — sacar monedas del banco
    /tienda                              — ver objetos disponibles
    /comprar <clave>                     — comprar un objeto
    /ranking                             — top 10 más ricos del grupo
"""
from __future__ import annotations

import logging
import random
import time

from telegram import Update
from telegram.constants import DiceEmoji, ParseMode
from telegram.ext import ContextTypes

from database import Database, EconomyProfile
from utils.formatting import error, humanize_seconds, mention, success, warning
from utils.parsing import resolve_target

logger = logging.getLogger(__name__)


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def _now() -> int:
    return int(time.time())


# --------------------------------------------------------------------- #
# Configuración de juegos con dados animados
# --------------------------------------------------------------------- #
# Cada juego define: emoji de Telegram, cooldown (1 vez al día = 86400s),
# y una función que traduce el valor del dado (aleatorio, lo decide
# Telegram del lado del servidor) en una recompensa de monedas + XP.
GAME_COOLDOWN = 86400  # 24h


def _basketball_reward(value: int) -> tuple[int, int, str]:
    if value in (4, 5):
        coins = random.randint(80, 150)
        return coins, coins // 8, "¡Encestaste! 🏀🔥"
    return 0, 2, "Tiraste y no entró. Más suerte mañana."


def _football_reward(value: int) -> tuple[int, int, str]:
    if value in (4, 5):
        coins = random.randint(80, 150)
        return coins, coins // 8, "¡GOOOL! ⚽🥅"
    return 0, 2, "El balón se fue desviado."


def _darts_reward(value: int) -> tuple[int, int, str]:
    if value == 6:
        coins = random.randint(150, 250)
        return coins, coins // 6, "¡Diana perfecta! 🎯💥"
    if value in (4, 5):
        coins = random.randint(60, 120)
        return coins, coins // 6, "Buen tiro, cerca del centro."
    return 0, 2, "El dardo casi ni tocó el tablero."


def _bowling_reward(value: int) -> tuple[int, int, str]:
    if value == 6:
        coins = random.randint(150, 250)
        return coins, coins // 6, "¡STRIKE! 🎳🎉"
    if value in (4, 5):
        coins = random.randint(50, 100)
        return coins, coins // 6, "Tumbaste varios pinos."
    return 0, 2, "Casi todos los pinos siguen de pie."


_SLOT_JACKPOT_VALUES = {1, 22, 43, 64}  # tres símbolos iguales


def _slot_reward(value: int) -> tuple[int, int, str]:
    if value in _SLOT_JACKPOT_VALUES:
        coins = random.randint(300, 500)
        return coins, coins // 5, "¡JACKPOT! Tres símbolos iguales 🎰🤑"
    return 0, 2, "No hubo combinación ganadora esta vez."


GAMES: dict[str, dict] = {
    "basket": {
        "command": "baloncesto", "label": "🏀 Baloncesto", "emoji": DiceEmoji.BASKETBALL,
        "reward_fn": _basketball_reward,
    },
    "futbol": {
        "command": "futbol", "label": "⚽ Fútbol", "emoji": DiceEmoji.FOOTBALL,
        "reward_fn": _football_reward,
    },
    "dardos": {
        "command": "dardos", "label": "🎯 Dardos", "emoji": DiceEmoji.DARTS,
        "reward_fn": _darts_reward,
    },
    "bolos": {
        "command": "bolos", "label": "🎳 Bolos", "emoji": DiceEmoji.BOWLING,
        "reward_fn": _bowling_reward,
    },
    "slot": {
        "command": "tragamonedas", "label": "🎰 Tragamonedas", "emoji": DiceEmoji.SLOT_MACHINE,
        "reward_fn": _slot_reward,
    },
}


def _make_game_handler(key: str):
    game = GAMES[key]

    async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        user = update.effective_user
        message = update.effective_message
        if chat.type not in ("group", "supergroup"):
            await message.reply_text(error("Este comando solo funciona en grupos."))
            return

        db = _get_db(context)
        action = f"game:{key}"
        last_ts = await db.get_cooldown(chat.id, user.id, action)
        remaining = GAME_COOLDOWN - (_now() - last_ts)
        if remaining > 0:
            await message.reply_text(
                warning(
                    f"Ya jugaste a {game['label']} hoy. Podrás volver a intentarlo en "
                    f"*{humanize_seconds(remaining)}*."
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        dice_msg = await context.bot.send_dice(chat_id=chat.id, emoji=game["emoji"])
        value = dice_msg.dice.value

        coins, xp, phrase = game["reward_fn"](value)
        await db.set_cooldown(chat.id, user.id, action)
        if coins > 0:
            await db.add_balance(chat.id, user.id, coins)
        if xp > 0:
            await db.add_xp(chat.id, user.id, xp)

        if coins > 0:
            text = f"{phrase}\n💰 Ganaste *{coins}* monedas \\(\\+{xp} XP\\)\\."
        else:
            text = f"{phrase}\n💰 No ganaste monedas esta vez \\(\\+{xp} XP por participar\\)\\."
        await message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

    return _handler


baloncesto_command = _make_game_handler("basket")
futbol_command = _make_game_handler("futbol")
dardos_command = _make_game_handler("dardos")
bolos_command = _make_game_handler("bolos")
tragamonedas_command = _make_game_handler("slot")


# --------------------------------------------------------------------- #
# /diario — bono diario con racha
# --------------------------------------------------------------------- #
DAILY_BASE = 100
DAILY_STREAK_BONUS = 20   # por cada día consecutivo, hasta el tope
DAILY_STREAK_CAP = 10
DAILY_RESET_GRACE = 172800  # si pasan más de 48h sin reclamar, se rompe la racha


async def diario_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return

    db = _get_db(context)
    profile = await db.get_economy(chat.id, user.id)
    elapsed = _now() - profile.last_daily

    if elapsed < GAME_COOLDOWN:
        await message.reply_text(
            warning(f"Ya reclamaste tu bono diario hoy. Vuelve en *{humanize_seconds(GAME_COOLDOWN - elapsed)}*."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    new_streak = profile.daily_streak + 1 if elapsed < DAILY_RESET_GRACE else 1
    new_streak = min(new_streak, DAILY_STREAK_CAP)
    bonus = DAILY_BASE + DAILY_STREAK_BONUS * (new_streak - 1)

    await db.add_balance(chat.id, user.id, bonus)
    await db.add_xp(chat.id, user.id, 15)
    await db.set_daily(chat.id, user.id, new_streak, _now())

    text = (
        f"🎁 *Bono diario reclamado*\n"
        f"💰 \\+{bonus} monedas \\(racha: {new_streak} día\\(s\\)\\)\n"
        f"✨ \\+15 XP"
    )
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


# --------------------------------------------------------------------- #
# Trabajos
# --------------------------------------------------------------------- #
JOBS: dict[str, dict] = {
    "repartidor": {"label": "🛵 Repartidor", "min_level": 1, "pay": (50, 120), "cooldown": 3600},
    "mesero":     {"label": "🍽 Mesero",      "min_level": 2, "pay": (100, 200), "cooldown": 2 * 3600},
    "chofer":     {"label": "🚕 Chofer",      "min_level": 3, "pay": (150, 280), "cooldown": 3 * 3600},
    "programador":{"label": "💻 Programador", "min_level": 5, "pay": (250, 450), "cooldown": 4 * 3600},
    "abogado":    {"label": "⚖️ Abogado",     "min_level": 7, "pay": (400, 650), "cooldown": 6 * 3600},
    "empresario": {"label": "💼 Empresario",  "min_level": 10, "pay": (700, 1100), "cooldown": 8 * 3600},
}


async def trabajos_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return

    db = _get_db(context)
    profile = await db.get_economy(chat.id, user.id)

    lines = ["💼 *Empleos disponibles*", ""]
    for key, job in JOBS.items():
        lock = "🔒 " if profile.level < job["min_level"] else ""
        pay_min, pay_max = job["pay"]
        lines.append(
            f"{lock}{job['label']} — `/trabajo {key}`\n"
            f"    Sueldo: {pay_min}\\-{pay_max} 💰 cada {humanize_seconds(job['cooldown'])} "
            f"\\(nivel mín\\. {job['min_level']}\\)"
        )
    current = f"\n👷 Tu empleo actual: *{JOBS[profile.job]['label']}*" if profile.job in JOBS else \
        "\n👷 No tienes empleo actualmente."
    lines.append(current)
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


async def trabajo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return

    if not context.args:
        await message.reply_text(error("Indica el empleo. Usa /trabajos para ver la lista."))
        return

    key = context.args[0].strip().lower()
    job = JOBS.get(key)
    if not job:
        await message.reply_text(error(f"No existe el empleo `{key}`. Usa /trabajos para ver la lista."),
                                  parse_mode=ParseMode.MARKDOWN_V2)
        return

    db = _get_db(context)
    profile = await db.get_economy(chat.id, user.id)
    if profile.level < job["min_level"]:
        await message.reply_text(
            error(f"Necesitas nivel {job['min_level']} para trabajar de {job['label']}. Tu nivel: {profile.level}.")
        )
        return

    await db.set_job(chat.id, user.id, key)
    await db.set_cooldown(chat.id, user.id, "work", 0)  # puede cobrar de inmediato al empezar
    await message.reply_text(success(f"Ahora trabajas de {job['label']}. Usa /cobrar cuando tengas el sueldo listo."))


async def renunciar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)
    profile = await db.get_economy(chat.id, user.id)
    if not profile.job:
        await message.reply_text(warning("No tienes ningún empleo."))
        return
    await db.set_job(chat.id, user.id, None)
    await message.reply_text(success("Renunciaste a tu empleo."))


async def cobrar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return

    db = _get_db(context)
    profile = await db.get_economy(chat.id, user.id)
    job = JOBS.get(profile.job) if profile.job else None
    if not job:
        await message.reply_text(error("No tienes empleo. Usa /trabajos para elegir uno."))
        return

    last_ts = await db.get_cooldown(chat.id, user.id, "work")
    remaining = job["cooldown"] - (_now() - last_ts)
    if remaining > 0:
        await message.reply_text(
            warning(f"Todavía no te toca cobrar. Vuelve en *{humanize_seconds(remaining)}*."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    pay = random.randint(*job["pay"])
    await db.add_balance(chat.id, user.id, pay)
    await db.add_xp(chat.id, user.id, 10)
    await db.set_cooldown(chat.id, user.id, "work")
    await message.reply_text(
        success(f"Cobraste tu sueldo de {job['label']}: +{pay} monedas (+10 XP).")
    )


# --------------------------------------------------------------------- #
# /robar
# --------------------------------------------------------------------- #
STEAL_COOLDOWN = 4 * 3600
STEAL_TARGET_IMMUNITY = 6 * 3600  # tras ser robado, protegido un rato
STEAL_MIN_VICTIM_BALANCE = 100
STEAL_MAX_LEVEL_ABOVE = 3   # no puedes robarle a alguien mucho más fuerte
STEAL_MAX_LEVEL_BELOW = 4   # "no es válido" robarle a alguien mucho más débil


async def robar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return

    db = _get_db(context)
    resolved = await resolve_target(update, db, context.args)
    if isinstance(resolved, str):
        await message.reply_text(error(resolved))
        return
    if resolved.user_id == user.id:
        await message.reply_text(error("No puedes robarte a ti mismo."))
        return

    attacker = await db.get_economy(chat.id, user.id)
    victim = await db.get_economy(chat.id, resolved.user_id)

    last_ts = await db.get_cooldown(chat.id, user.id, "steal")
    remaining = STEAL_COOLDOWN - (_now() - last_ts)
    if remaining > 0:
        await message.reply_text(
            warning(f"Tienes que esperar *{humanize_seconds(remaining)}* para volver a robar."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if victim.shield_until > _now():
        await message.reply_text(
            error(f"{resolved.display_name} tiene un 🛡 escudo anti\\-robo activo\\. No puedes robarle ahora\\."),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    victim_immune_ts = await db.get_cooldown(chat.id, resolved.user_id, "robbed_immunity")
    if _now() - victim_immune_ts < STEAL_TARGET_IMMUNITY:
        await message.reply_text(error(f"{resolved.display_name} ya fue robado hace poco. Dale un descanso."))
        return

    if victim.balance < STEAL_MIN_VICTIM_BALANCE:
        await message.reply_text(
            error(f"{resolved.display_name} no tiene suficiente efectivo encima como para valer la pena robarle.")
        )
        return

    if victim.level > attacker.level + STEAL_MAX_LEVEL_ABOVE:
        await message.reply_text(
            error(f"{resolved.display_name} es demasiado fuerte para ti (nivel {victim.level} vs tu nivel {attacker.level}).")
        )
        return

    if victim.level < attacker.level - STEAL_MAX_LEVEL_BELOW:
        await message.reply_text(
            error(f"{resolved.display_name} es demasiado débil, no es digno de robarle (nivel {victim.level}).")
        )
        return

    await db.set_cooldown(chat.id, user.id, "steal")

    # % de éxito: base 45%, +5% por cada nivel de ventaja del atacante, acotado 15-80%.
    level_diff = attacker.level - victim.level
    success_chance = max(15, min(80, 45 + level_diff * 5))
    won = random.randint(1, 100) <= success_chance

    if won:
        stolen = int(victim.balance * random.uniform(0.10, 0.25))
        stolen = max(stolen, 1)
        await db.add_balance(chat.id, resolved.user_id, -stolen)
        await db.add_balance(chat.id, user.id, stolen)
        await db.add_xp(chat.id, user.id, 15)
        await db.set_cooldown(chat.id, resolved.user_id, "robbed_immunity")
        text = (
            f"🕵️ *¡Robo exitoso!*\n"
            f"{mention(user.id, user.first_name)} le robó *{stolen}* monedas a "
            f"{mention(resolved.user_id, resolved.display_name)} \\.\n"
            f"Probabilidad de éxito: {success_chance}%"
        )
    else:
        fine = max(10, int(attacker.balance * random.uniform(0.05, 0.15)))
        await db.add_balance(chat.id, user.id, -fine)
        text = (
            f"🚨 *¡Te atraparon!*\n"
            f"{mention(user.id, user.first_name)} intentó robarle a "
            f"{mention(resolved.user_id, resolved.display_name)} y falló\\.\n"
            f"Pagó una multa de *{fine}* monedas\\.\n"
            f"Probabilidad de éxito que tenía: {success_chance}%"
        )
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


# --------------------------------------------------------------------- #
# /transferir
# --------------------------------------------------------------------- #
async def transferir_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return

    db = _get_db(context)
    resolved = await resolve_target(update, db, context.args)
    if isinstance(resolved, str):
        await message.reply_text(error(resolved))
        return
    if resolved.user_id == user.id:
        await message.reply_text(error("No puedes transferirte monedas a ti mismo."))
        return
    if not resolved.remaining_args:
        await message.reply_text(error("Indica la cantidad a transferir. Ej: /transferir @usuario 100"))
        return

    try:
        amount = int(resolved.remaining_args[0])
    except ValueError:
        await message.reply_text(error("La cantidad debe ser un número."))
        return
    if amount <= 0:
        await message.reply_text(error("La cantidad debe ser mayor a 0."))
        return

    sender = await db.get_economy(chat.id, user.id)
    if amount > sender.balance:
        await message.reply_text(error(f"No tienes suficiente efectivo. Tu saldo: {sender.balance} monedas."))
        return

    await db.add_balance(chat.id, user.id, -amount)
    await db.add_balance(chat.id, resolved.user_id, amount)
    text = (
        f"💸 {mention(user.id, user.first_name)} le transfirió *{amount}* monedas a "
        f"{mention(resolved.user_id, resolved.display_name)}\\."
    )
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


# --------------------------------------------------------------------- #
# Banco
# --------------------------------------------------------------------- #
async def _parse_amount(arg: str, available: int) -> int | None:
    if arg.lower() in ("todo", "all", "max"):
        return available
    try:
        value = int(arg)
    except ValueError:
        return None
    return value if value > 0 else None


async def depositar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)
    profile = await db.get_economy(chat.id, user.id)

    if not context.args:
        await message.reply_text(error("Indica la cantidad. Ej: /depositar 200 (o /depositar todo)"))
        return
    amount = await _parse_amount(context.args[0], profile.balance)
    if amount is None:
        await message.reply_text(error("Cantidad inválida."))
        return

    updated = await db.bank_deposit(chat.id, user.id, amount)
    if updated is None:
        await message.reply_text(error(f"No tienes {amount} monedas en efectivo para depositar."))
        return
    await message.reply_text(
        success(f"Depositaste {amount} monedas. Banco: {updated.bank} 🏦 | Efectivo: {updated.balance} 💰")
    )


async def retirar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    db = _get_db(context)
    profile = await db.get_economy(chat.id, user.id)

    if not context.args:
        await message.reply_text(error("Indica la cantidad. Ej: /retirar 200 (o /retirar todo)"))
        return
    amount = await _parse_amount(context.args[0], profile.bank)
    if amount is None:
        await message.reply_text(error("Cantidad inválida."))
        return

    updated = await db.bank_withdraw(chat.id, user.id, amount)
    if updated is None:
        await message.reply_text(error(f"No tienes {amount} monedas en el banco para retirar."))
        return
    await message.reply_text(
        success(f"Retiraste {amount} monedas. Banco: {updated.bank} 🏦 | Efectivo: {updated.balance} 💰")
    )


# --------------------------------------------------------------------- #
# Tienda
# --------------------------------------------------------------------- #
SHOP_ITEMS: dict[str, dict] = {
    "escudo": {
        "label": "🛡 Escudo anti-robo (12h)", "price": 300,
        "description": "Nadie puede robarte durante 12 horas.",
    },
    "escudo24": {
        "label": "🛡 Escudo anti-robo (24h)", "price": 500,
        "description": "Nadie puede robarte durante 24 horas.",
    },
}


async def tienda_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = ["🛒 *Tienda*", ""]
    for key, item in SHOP_ITEMS.items():
        lines.append(f"*{item['label']}* — {item['price']} 💰\n    {item['description']}\n    `/comprar {key}`")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)


async def comprar_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return
    if not context.args:
        await message.reply_text(error("Indica qué comprar. Usa /tienda para ver la lista."))
        return

    key = context.args[0].strip().lower()
    item = SHOP_ITEMS.get(key)
    if not item:
        await message.reply_text(error("Ese objeto no existe. Usa /tienda para ver la lista."))
        return

    db = _get_db(context)
    profile = await db.get_economy(chat.id, user.id)
    if profile.balance < item["price"]:
        await message.reply_text(error(f"Te faltan monedas. Necesitas {item['price']}, tienes {profile.balance}."))
        return

    await db.add_balance(chat.id, user.id, -item["price"])
    if key in ("escudo", "escudo24"):
        hours = 12 if key == "escudo" else 24
        base_ts = max(profile.shield_until, _now())  # si ya tenía uno activo, se suma
        until = base_ts + hours * 3600
        await db.set_shield(chat.id, user.id, until)
        await message.reply_text(success(f"Compraste {item['label']}. Escudo activo por {hours}h."))
        return

    await message.reply_text(success(f"Compraste {item['label']}."))


# --------------------------------------------------------------------- #
# Perfil / ranking
# --------------------------------------------------------------------- #
async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return

    db = _get_db(context)
    if context.args or message.reply_to_message:
        resolved = await resolve_target(update, db, context.args)
        if isinstance(resolved, str):
            await message.reply_text(error(resolved))
            return
        target_id, target_name = resolved.user_id, resolved.display_name
    else:
        target_id, target_name = update.effective_user.id, update.effective_user.first_name

    profile = await db.get_economy(chat.id, target_id)
    job_label = JOBS[profile.job]["label"] if profile.job in JOBS else "Sin empleo"
    shield = f"🛡 activo ({humanize_seconds(profile.shield_until - _now())})" if profile.shield_until > _now() else "sin escudo"

    text = (
        f"👤 *Perfil de {mention(target_id, target_name)}*\n"
        f"💰 Efectivo: *{profile.balance}*\n"
        f"🏦 Banco: *{profile.bank}*\n"
        f"📈 Nivel *{profile.level}* \\({profile.xp_into_level}/100 XP\\)\n"
        f"💼 Empleo: {job_label}\n"
        f"🛡 {shield}"
    )
    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def ranking_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message
    if chat.type not in ("group", "supergroup"):
        await message.reply_text(error("Este comando solo funciona en grupos."))
        return

    db = _get_db(context)
    top = await db.get_leaderboard(chat.id, limit=10)
    if not top:
        await message.reply_text("Todavía nadie tiene monedas en este grupo.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 *Ranking de riqueza del grupo*", ""]
    for i, profile in enumerate(top):
        name = await db.get_user_display_name(profile.user_id) or str(profile.user_id)
        icon = medals[i] if i < 3 else f"{i + 1}\\."
        total = profile.balance + profile.bank
        lines.append(f"{icon} {mention(profile.user_id, name)} — *{total}* 💰 \\(nivel {profile.level}\\)")
    await message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
