"""
handlers/horoscopo.py
Comando: /horoscopo

La primera vez que alguien lo usa, el bot le pide su fecha de nacimiento
(día y mes) para calcular el signo. A partir de ahí queda guardada, así
que las próximas veces devuelve el horóscopo del día directo, sin volver
a preguntar. "/horoscopo cambiar" permite corregir la fecha guardada.
"""
from __future__ import annotations

import calendar
import random
import re
from datetime import date
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from database import Database
from utils.formatting import error, escape_md

_PENDING_KEY = "horoscopo_pending_birthdate"

_DATE_RE = re.compile(r"^\s*(\d{1,2})\s*[/\-. ]\s*(\d{1,2})(?:\s*[/\-. ]\s*\d{2,4})?\s*$")

# (mes_inicio, día_inicio, signo, emoji) — orden cronológico dentro del
# año. Antes del primer inicio (20/1) el signo sigue siendo Capricornio
# (arrastrado desde el 22/12 del año anterior), por eso es el default.
_SIGN_STARTS: list[tuple[int, int, str, str]] = [
    (1, 20, "Acuario", "♒"),
    (2, 19, "Piscis", "♓"),
    (3, 21, "Aries", "♈"),
    (4, 20, "Tauro", "♉"),
    (5, 21, "Géminis", "♊"),
    (6, 21, "Cáncer", "♋"),
    (7, 23, "Leo", "♌"),
    (8, 23, "Virgo", "♍"),
    (9, 23, "Libra", "♎"),
    (10, 23, "Escorpio", "♏"),
    (11, 22, "Sagitario", "♐"),
    (12, 22, "Capricornio", "♑"),
]

_TRAITS: dict[str, str] = {
    "Capricornio": "constante y con los pies bien puestos en la tierra",
    "Acuario": "independiente y con ideas fuera de lo común",
    "Piscis": "sensible y con la intuición muy despierta",
    "Aries": "impulsivo y con ganas de ir siempre para adelante",
    "Tauro": "terco pero de fiar cuando se compromete con algo",
    "Géminis": "curioso y con la cabeza en mil cosas a la vez",
    "Cáncer": "protector con los suyos y algo nostálgico",
    "Leo": "con presencia y ganas de que las cosas salgan a su manera",
    "Virgo": "detallista y exigente, sobre todo consigo mismo",
    "Libra": "buscando siempre el equilibrio y evitando el conflicto",
    "Escorpio": "intenso y directo, no se guarda las cosas a medias",
    "Sagitario": "con ganas de moverse y aprender cosas nuevas",
}

_GENERAL = [
    "Hoy las cosas se acomodan solas si no te apurás de más.",
    "Un imprevisto te va a hacer cambiar de planes, pero para bien.",
    "Es buen día para terminar algo que tenías colgado hace rato.",
    "Vas a tener más paciencia de la habitual, aprovechala.",
    "Alguien te va a pedir un favor; fijate si de verdad podés antes de decir que sí.",
    "El día viene tranquilo, ideal para poner orden en algo que lo necesitaba.",
    "Puede aparecer una buena noticia de algo que ya dabas por perdido.",
    "Vas a tener ganas de hablar más de lo normal; elegí bien con quién.",
]

_AMOR = [
    "En el amor, hoy conviene decir las cosas claras en vez de dar vueltas.",
    "Si estás en pareja, un gesto simple va a valer más que uno grande.",
    "Si estás soltero/a, alguien de tu círculo cercano puede sorprenderte.",
    "No es buen momento para discutir por algo que en el fondo no importa tanto.",
    "Un mensaje que estabas esperando puede llegar hoy o mañana.",
    "Cuidado con los celos de más; hoy conviene confiar un poco más.",
]

_DINERO = [
    "En la plata, mejor esperar antes de gastar en algo grande.",
    "Puede aparecer un ingreso extra que no esperabas.",
    "Buen día para ordenar cuentas pendientes, aunque no sea lo más entretenido.",
    "Evitá prestar plata hoy, mejor esperá a otro momento.",
    "Una idea para ganar un extra te puede rondar la cabeza; anotala.",
    "Vigila un gasto chico que se te puede estar yendo de las manos.",
]

_SALUD = [
    "Bajale un cambio si venís acumulando cansancio.",
    "Tomá más agua de la que tomás normalmente, te vas a sentir mejor.",
    "Un rato al aire libre te va a venir bien para despejarte.",
    "Dormí un poco más temprano si podés, lo vas a notar mañana.",
    "Cuidado con las contracturas si pasás mucho tiempo sentado/a.",
    "El cuerpo te está pidiendo bajar un cambio con las pantallas.",
]

_COLORES = ["rojo", "azul", "verde", "amarillo", "violeta", "naranja", "blanco", "negro", "turquesa", "dorado"]


def _zodiac_sign(day: int, month: int) -> tuple[str, str]:
    current = (month, day)
    sign, emoji = "Capricornio", "♑"  # antes del 20/1 sigue siendo Capricornio
    for start_month, start_day, s, e in _SIGN_STARTS:
        if current >= (start_month, start_day):
            sign, emoji = s, e
        else:
            break  # la lista está ordenada cronológicamente
    return sign, emoji


def _valid_date(day: int, month: int) -> bool:
    if not (1 <= month <= 12):
        return False
    max_day = calendar.monthrange(2000, month)[1]  # 2000 es bisiesto: cubre el 29/02
    return 1 <= day <= max_day


def _daily_horoscope(sign: str, emoji: str, name: str) -> str:
    today = date.today().isoformat()
    rnd = random.Random(f"{sign}-{today}")
    trait = _TRAITS.get(sign, "único/a en su estilo")
    lucky_number = rnd.randint(1, 99)
    lucky_color = rnd.choice(_COLORES)

    lines = [
        f"{emoji} *Horóscopo de hoy — {escape_md(sign)}*",
        "",
        f"{escape_md(name)}, siendo {escape_md(trait)}, así viene tu día:",
        "",
        f"✨ {escape_md(rnd.choice(_GENERAL))}",
        f"❤️ {escape_md(rnd.choice(_AMOR))}",
        f"💰 {escape_md(rnd.choice(_DINERO))}",
        f"🩺 {escape_md(rnd.choice(_SALUD))}",
        "",
        f"🔢 Número de la suerte: *{lucky_number}*",
        f"🎨 Color de la suerte: *{escape_md(lucky_color)}*",
    ]
    return "\n".join(lines)


def _get_db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


async def _send_horoscope(update: Update, context: ContextTypes.DEFAULT_TYPE, day: int, month: int) -> None:
    sign, emoji = _zodiac_sign(day, month)
    name = update.effective_user.first_name or "vos"
    await update.effective_message.reply_text(
        _daily_horoscope(sign, emoji, name), parse_mode="MarkdownV2",
    )


async def horoscopo_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    db = _get_db(context)

    force_reset = bool(context.args) and context.args[0].lower() in ("cambiar", "fecha", "reset")

    stored: Optional[tuple[int, int]] = None if force_reset else await db.get_user_birthdate(user.id)
    if stored is not None:
        await _send_horoscope(update, context, stored[0], stored[1])
        return

    context.user_data[_PENDING_KEY] = True
    await update.effective_message.reply_text(
        "🔮 Para tirarte el horóscopo necesito tu fecha de nacimiento \\(día y mes alcanza, "
        "no hace falta el año\\)\\.\n\n"
        "Respondé por ejemplo: `24/03`\n\n"
        "Escribí /cancelar para cancelar\\.",
        parse_mode="MarkdownV2",
    )


async def try_consume_pending_birthdate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get(_PENDING_KEY):
        return False

    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return False

    if text.lower() in ("/cancelar", "cancelar"):
        context.user_data.pop(_PENDING_KEY, None)
        await message.reply_text("Cancelado.")
        return True

    match = _DATE_RE.match(text)
    if not match:
        await message.reply_text(
            error("Formato inválido. Mandá día y mes así: 24/03 (podés usar / - . o espacio).")
        )
        return True

    day, month = int(match.group(1)), int(match.group(2))
    if not _valid_date(day, month):
        await message.reply_text(error("Esa fecha no existe. Revisá el día y el mes."))
        return True

    context.user_data.pop(_PENDING_KEY, None)
    db = _get_db(context)
    await db.set_user_birthdate(update.effective_user.id, day, month)
    await _send_horoscope(update, context, day, month)
    return True
