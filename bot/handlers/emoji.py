"""
handlers/emoji.py
/emoji y /emojis — convierten archivos y stickers en EMOJIS PERSONALIZADOS
de Telegram (custom emoji), guardados en packs propios del usuario.

/emoji
    Se usa de dos formas:
      a) Mandando una foto, video o animación (GIF) con "/emoji" en el pie
         de foto (caption).
      b) Respondiendo a una foto/video/animación/sticker con /emoji.
    Se puede agregar un emoji asociado: "/emoji 😎" (por defecto 🙂).
    - Imagen   -> PNG 100x100 transparente, centrada sin deformar.
    - Video/GIF-> WEBM VP9 100x100, máx. 3 s, sin audio, ≤ 256 KB.
    - Sticker animado (TGS) -> se sube tal cual.
    Si el usuario no tiene pack, se crea (createNewStickerSet con
    sticker_type="custom_emoji"); si ya tiene, se agrega (addStickerToSet).
    Nombre del pack: emojis_<user_id>_by_<bot>   Título: "Emojis de <nombre>".

    Nota técnica: Telegram no deja mezclar formatos dentro de un mismo pack,
    así que cada usuario puede tener hasta 3 packs propios:
        imágenes   -> emojis_<id>_by_<bot>
        videos     -> emojis_<id>_vid_by_<bot>
        animados   -> emojis_<id>_anim_by_<bot>
    (y "_2", "_3"... cuando uno llega al máximo de 200 emojis).

/emojis
    Respondiendo a un sticker: convierte el PACK COMPLETO de ese sticker en
    emojis personalizados, copiando el emoji de cada uno. Si el pack supera
    200 se divide en varios. Corre en segundo plano con un mensaje de
    progreso ("Convirtiendo 12/40…"), con pausas entre llamadas y reintento
    automático ante el error 429 (retry_after).

Límites anti-abuso (ver constantes más abajo):
    - /emojis: 1 conversión de pack a la vez por usuario (y tope global).
    - /emoji : se procesan de a una por usuario, con máximo de pendientes.

Registro en main.py: register_emoji_handlers(application)
"""
from __future__ import annotations

import asyncio
import logging
import re
import tempfile
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from telegram import InputFile, InputSticker, Message, Update
from telegram.constants import StickerFormat, StickerType
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from database import Database
from utils import emoji_db
from utils.convertir import (
    ConversionError,
    FfmpegNoDisponible,
    imagen_a_emoji,
    video_a_emoji_webm,
)

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------- #
EMOJI_POR_DEFECTO = "🙂"
MAX_EMOJIS_POR_PACK = 200            # límite de Telegram para packs de emojis personalizados
MAX_DESCARGA_BYTES = 20 * 1024 * 1024  # límite de getFile para bots (20 MB)

PAUSA_ENTRE_LLAMADAS = 0.6           # segundos entre cada llamada a la API en /emojis
MAX_REINTENTOS_429 = 6               # reintentos ante "Too Many Requests"
MAX_REINTENTOS_RED = 2               # reintentos ante timeouts / caídas de red
INTERVALO_PROGRESO = 3.0             # segundos mínimos entre ediciones del mensaje de progreso

MAX_PENDIENTES_EMOJI = 3             # /emoji encolados por usuario a la vez
MAX_PACKS_SIMULTANEOS = 3            # conversiones de pack (/emojis) en todo el bot a la vez

_FORMATOS_DB = ("static", "video", "animated")
_FORMATO_TELEGRAM = {
    "static": StickerFormat.STATIC,
    "video": StickerFormat.VIDEO,
    "animated": StickerFormat.ANIMATED,
}
_EXTENSION = {"static": "png", "video": "webm", "animated": "tgs"}

# Patrón del pie de foto: "/emoji", "/emoji 😎" o "/emoji@MiBot 😎" (pero NO "/emojis").
_PATRON_CAPTION = re.compile(r"^/emoji(?:@(\w+))?(?:\s|$)", re.IGNORECASE)

# --------------------------------------------------------------------- #
# Estado en memoria (control de abuso)
# --------------------------------------------------------------------- #
_locks_usuario: dict[int, asyncio.Lock] = {}      # serializa los /emoji de cada usuario
_pendientes_emoji: dict[int, int] = {}            # cuántos /emoji tiene en cola cada usuario
_packs_en_curso: set[int] = set()                 # usuarios con un /emojis corriendo ahora
_en_curso: set[int] = set()                       # chats con un /emoji o /emojis corriendo ahora (un bloqueo por chat)
_tareas_segundo_plano: set[asyncio.Task] = set()  # referencias fuertes para que no las borre el GC


def _lock_de(user_id: int) -> asyncio.Lock:
    lock = _locks_usuario.get(user_id)
    if lock is None:
        lock = _locks_usuario[user_id] = asyncio.Lock()
    return lock


# --------------------------------------------------------------------- #
# Modelo de "fuente": qué archivo hay que convertir
# --------------------------------------------------------------------- #
@dataclass(slots=True)
class Fuente:
    tipo: str                 # 'imagen' | 'video' | 'sticker_estatico' | 'sticker_video' | 'sticker_tgs'
    file_id: str
    file_size: Optional[int]
    emoji_original: Optional[str] = None


def _buscar_fuente(msg: Optional[Message]) -> Optional[Fuente]:
    """Mira un mensaje y devuelve qué archivo convertible trae (o None).
    El orden importa: un GIF de Telegram trae tanto `animation` como
    `document`, así que `animation` se revisa antes que `document`."""
    if msg is None:
        return None
    if msg.animation:
        return Fuente("video", msg.animation.file_id, msg.animation.file_size)
    if msg.video:
        return Fuente("video", msg.video.file_id, msg.video.file_size)
    if msg.sticker:
        st = msg.sticker
        if st.is_animated:
            tipo = "sticker_tgs"
        elif st.is_video:
            tipo = "sticker_video"
        else:
            tipo = "sticker_estatico"
        return Fuente(tipo, st.file_id, st.file_size, st.emoji)
    if msg.photo:
        foto = msg.photo[-1]  # la de mayor resolución
        return Fuente("imagen", foto.file_id, foto.file_size)
    doc = msg.document
    if doc:
        mime = (doc.mime_type or "").lower()
        if mime == "image/gif" or mime.startswith("video/"):
            return Fuente("video", doc.file_id, doc.file_size)
        if mime.startswith("image/"):
            return Fuente("imagen", doc.file_id, doc.file_size)
    return None


def _formato_de(tipo: str) -> str:
    """Formato de pack ('static' | 'video' | 'animated') según el tipo de fuente."""
    if tipo in ("video", "sticker_video"):
        return "video"
    if tipo == "sticker_tgs":
        return "animated"
    return "static"


# --------------------------------------------------------------------- #
# Utilidades de texto / nombres
# --------------------------------------------------------------------- #
def _extraer_emojis(argumentos: list[str]) -> list[str]:
    """Toma los argumentos del comando y se queda con los que parecen
    emojis (descarta palabras sueltas ASCII como 'hola' o '123').
    Si no queda ninguno devuelve el emoji por defecto. Máximo 20."""
    emojis = [a for a in argumentos if a and not (a.isascii() and a.isalnum())]
    return emojis[:20] or [EMOJI_POR_DEFECTO]


def _nombre_para_titulo(user) -> str:
    return (user.first_name or user.username or "usuario").strip() or "usuario"


def _titulo_personal(user, fmt: str, volumen: int) -> str:
    titulo = f"Emojis de {_nombre_para_titulo(user)}"
    if fmt == "video":
        titulo += " (video)"
    elif fmt == "animated":
        titulo += " (animados)"
    if volumen > 1:
        titulo += f" {volumen}"
    return titulo[:64]  # Telegram corta los títulos a 64 caracteres


def _nombre_pack_personal(user_id: int, fmt: str, volumen: int, bot_username: str) -> str:
    base = f"emojis_{user_id}"
    if fmt == "video":
        base += "_vid"
    elif fmt == "animated":
        base += "_anim"
    if volumen > 1:
        base += f"_{volumen}"
    return f"{base}_by_{bot_username}"


def _slug(texto: str) -> str:
    """Deja solo letras/números/guion bajo (sin guiones bajos repetidos)."""
    return re.sub(r"[^A-Za-z0-9]+", "_", texto).strip("_")


def _nombre_pack_convertido(user_id: int, slug_origen: str, parte: int, hay_varias: bool,
                            extra: str, bot_username: str) -> str:
    """Nombre de un pack creado por /emojis, p. ej. emojis_123_gatitos_2_by_bot.
    Telegram limita el nombre a 64 caracteres: se recorta el slug si hace falta."""
    sufijo = f"_{parte}" if hay_varias else ""
    if extra:
        sufijo += f"_{extra}"
    fijo = f"emojis_{user_id}_" + "{slug}" + f"{sufijo}_by_{bot_username}"
    espacio = 64 - len(fijo.replace("{slug}", ""))
    slug = (slug_origen[: max(espacio, 1)] or "x").strip("_") or "x"
    return fijo.replace("{slug}", slug)


def _enlace(nombre_pack: str) -> str:
    return f"https://t.me/addemoji/{nombre_pack}"


# --------------------------------------------------------------------- #
# Llamadas a Telegram con reintentos (429 / red)
# --------------------------------------------------------------------- #
def _segundos_de(retry_after: Any) -> float:
    """`RetryAfter.retry_after` es int en PTB 22.x (y timedelta en versiones futuras)."""
    if isinstance(retry_after, timedelta):
        return retry_after.total_seconds()
    return float(retry_after)


async def _con_reintentos(funcion: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
    """Ejecuta una llamada a la API. Ante 429 espera el `retry_after` que
    pide Telegram y reintenta; ante timeouts/caídas de red reintenta pocas
    veces. Cualquier otro error se propaga tal cual."""
    errores_red = 0
    for intento in range(1, MAX_REINTENTOS_429 + 1):
        try:
            return await funcion(*args, **kwargs)
        except RetryAfter as exc:
            if intento == MAX_REINTENTOS_429:
                raise
            espera = _segundos_de(exc.retry_after) + 1
            logger.info("429 de Telegram: espero %.0f s (intento %d)", espera, intento)
            await asyncio.sleep(espera)
        except (TimedOut, NetworkError) as exc:
            # BadRequest hereda de NetworkError en PTB: ese no se reintenta.
            if isinstance(exc, BadRequest):
                raise
            errores_red += 1
            if errores_red > MAX_REINTENTOS_RED:
                raise
            logger.info("Error de red (%s): reintento %d", exc, errores_red)
            await asyncio.sleep(2 * errores_red)


async def _editar(msg: Optional[Message], texto: str) -> None:
    """Edita un mensaje sin que un fallo de edición rompa el proceso
    (mensaje borrado, 'message is not modified', límite de ediciones...)."""
    if msg is None:
        return
    try:
        await msg.edit_text(texto, disable_web_page_preview=True)
    except TelegramError as exc:
        logger.debug("No pude editar el mensaje de progreso: %s", exc)


# --------------------------------------------------------------------- #
# Mapeo de errores de Telegram a mensajes claros para el usuario
# --------------------------------------------------------------------- #
def _es_usuario_sin_start(exc: TelegramError) -> bool:
    t = str(exc).lower()
    return any(p in t for p in (
        "peer_id_invalid", "user not found", "user_id_invalid",
        "bot was blocked", "can't initiate conversation", "user is deactivated",
    )) or isinstance(exc, Forbidden)


def _mensaje_error_telegram(exc: TelegramError, bot_username: str) -> str:
    t = str(exc).lower()
    if _es_usuario_sin_start(exc):
        return (
            "⚠️ Primero tenés que iniciar el bot: abrí un chat privado conmigo "
            f"(https://t.me/{bot_username}?start=emoji) y mandame /start. Después repetí el comando."
        )
    if "stickers_too_much" in t or "too many stickers" in t:
        return "⚠️ Ese pack ya está lleno (máximo 200 emojis)."
    if "already occupied" in t or "name is already" in t:
        return "⚠️ Ya existe un pack con ese nombre."
    if "sticker_video_big" in t or "file is too big" in t:
        return "⚠️ El archivo resultante es demasiado pesado para Telegram."
    if "sticker_png_dimensions" in t or "dimensions" in t:
        return "⚠️ Telegram rechazó las dimensiones del archivo."
    if "sticker_png_nopng" in t or "wrong file type" in t or "wrong file" in t:
        return "⚠️ Telegram rechazó el tipo de archivo."
    if "emoji" in t and ("invalid" in t or "wrong" in t):
        return "⚠️ Ese emoji no es válido. Probá con otro (por ejemplo: /emoji 😎)."
    if "premium" in t:
        return "⚠️ Telegram exige Premium para esa operación con emojis personalizados."
    return f"⚠️ Telegram devolvió un error: {exc}"


# --------------------------------------------------------------------- #
# Descarga + conversión de una fuente a un InputSticker
# --------------------------------------------------------------------- #
async def _descargar(bot, file_id: str, destino: Path, file_size: Optional[int]) -> None:
    if file_size and file_size > MAX_DESCARGA_BYTES:
        raise ConversionError("El archivo pesa más de 20 MB y Telegram no deja que los bots lo descarguen.")
    try:
        archivo = await _con_reintentos(bot.get_file, file_id)
        await _con_reintentos(archivo.download_to_drive, custom_path=destino)
    except BadRequest as exc:
        if "too big" in str(exc).lower():
            raise ConversionError("El archivo pesa más de 20 MB y Telegram no deja que los bots lo descarguen.") from exc
        raise


async def _fuente_a_input_sticker(bot, fuente: Fuente, emojis: list[str], carpeta: Path,
                                  indice: int = 0) -> tuple[InputSticker, str]:
    """Descarga la fuente, la convierte al formato de emoji personalizado y
    devuelve (InputSticker, formato_db). `carpeta` es un directorio temporal
    que el llamador borra al terminar."""
    entrada = carpeta / f"in_{indice}"
    fmt = _formato_de(fuente.tipo)
    await _descargar(bot, fuente.file_id, entrada, fuente.file_size)

    if fmt == "static":
        datos = await imagen_a_emoji(entrada.read_bytes())
    elif fmt == "video":
        salida = carpeta / f"out_{indice}.webm"
        # Los stickers de video de Telegram son WEBM con transparencia:
        # hay que decodificarlos con libvpx-vp9 para no perder el alfa.
        datos = await video_a_emoji_webm(entrada, salida, entrada_es_webm_vp9=(fuente.tipo == "sticker_video"))
        salida.unlink(missing_ok=True)
    else:  # animated (.tgs): se sube tal cual
        datos = entrada.read_bytes()

    entrada.unlink(missing_ok=True)  # liberamos disco apenas se puede
    archivo = InputFile(datos, filename=f"emoji_{indice}.{_EXTENSION[fmt]}", attach=True)
    return InputSticker(sticker=archivo, emoji_list=emojis, format=_FORMATO_TELEGRAM[fmt]), fmt


# --------------------------------------------------------------------- #
# /emoji — agregar UN emoji al pack personal del usuario
# --------------------------------------------------------------------- #
async def _agregar_a_pack_personal(bot, db: Database, user, fmt: str,
                                   sticker: InputSticker) -> tuple[str, bool]:
    """Agrega el sticker al pack personal del usuario para ese formato,
    creando el pack si no existe y abriendo un volumen nuevo si se llenó.
    Devuelve (nombre_del_pack, se_creo_pack_nuevo)."""
    bot_username = bot.username
    ultimo = await emoji_db.get_latest_emoji_pack(db, user.id, fmt)

    volumen, existe = 1, False
    if ultimo is not None:
        volumen, existe = ultimo["volume"], True
        if ultimo["sticker_count"] >= MAX_EMOJIS_POR_PACK:
            volumen, existe = volumen + 1, False  # lleno -> volumen nuevo

    # Bucle acotado: cubre "el pack ya existía en Telegram pero no en mi
    # base", "se llenó sin que lo supiera" y "el usuario borró el pack".
    for _ in range(5):
        nombre = _nombre_pack_personal(user.id, fmt, volumen, bot_username)
        try:
            if existe:
                await _con_reintentos(bot.add_sticker_to_set, user_id=user.id, name=nombre,
                                      sticker=sticker, write_timeout=60, read_timeout=30)
                await emoji_db.bump_emoji_pack_count(db, nombre, 1)
                return nombre, False

            await _con_reintentos(
                bot.create_new_sticker_set, user_id=user.id, name=nombre,
                title=_titulo_personal(user, fmt, volumen), stickers=[sticker],
                sticker_type=StickerType.CUSTOM_EMOJI, write_timeout=60, read_timeout=30,
            )
            await emoji_db.create_emoji_pack(db, nombre, user.id, "personal", fmt, volumen,
                                       _titulo_personal(user, fmt, volumen), None, 1)
            return nombre, True

        except BadRequest as exc:
            t = str(exc).lower()
            if not existe and "already occupied" in t:
                # Nombre duplicado: el pack ya existe en Telegram pero no estaba
                # en mi base. Lo "adopto": leo cuántos emojis tiene y sigo.
                conjunto = await _con_reintentos(bot.get_sticker_set, nombre)
                cantidad = len(conjunto.stickers)
                await emoji_db.create_emoji_pack(db, nombre, user.id, "personal", fmt, volumen,
                                           conjunto.title, None, cantidad)
                if cantidad >= MAX_EMOJIS_POR_PACK:
                    volumen += 1
                else:
                    existe = True
                continue
            if existe and ("stickers_too_much" in t or "too many stickers" in t):
                await emoji_db.set_emoji_pack_count(db, nombre, MAX_EMOJIS_POR_PACK)
                volumen, existe = volumen + 1, False
                continue
            if existe and ("stickerset_invalid" in t or "sticker set not found" in t or "stickerset_not_modified" in t):
                # El usuario borró el pack desde Telegram: lo volvemos a crear.
                existe = False
                continue
            raise

    raise ConversionError("No pude dejar el emoji en ningún pack después de varios intentos.")


async def _tarea_emoji(context: ContextTypes.DEFAULT_TYPE, user, fuente: Fuente,
                       emojis: list[str], estado: Message) -> None:
    """Trabajo en segundo plano de un /emoji: convierte y agrega al pack."""
    bot, db = context.bot, context.application.bot_data["db"]
    try:
        await emoji_db.asegurar_tabla(db)  # crea la tabla de packs la primera vez
        async with _lock_de(user.id):  # de a uno por usuario: evita crear el pack dos veces a la vez
            # Carpeta temporal: se borra sola al salir del bloque, pase lo que pase.
            with tempfile.TemporaryDirectory(prefix="emoji_") as tmp:
                input_sticker, fmt = await _fuente_a_input_sticker(bot, fuente, emojis, Path(tmp))
                nombre, creado = await _agregar_a_pack_personal(bot, db, user, fmt, input_sticker)

        extra = ""
        if fuente.tipo == "video":
            extra = "\n🎞 (Si duraba más de 3 s, lo recorté a 3 s.)"
        await _editar(estado, (
            f"✅ {'Pack creado y emoji agregado' if creado else 'Emoji agregado a tu pack'} ({' '.join(emojis)}).{extra}\n"
            f"👉 {_enlace(nombre)}"
        ))
    except FfmpegNoDisponible as exc:
        logger.error("ffmpeg no está instalado: %s", exc)
        await _editar(estado, f"⚠️ {exc} Avisale al administrador del bot.")
    except ConversionError as exc:
        await _editar(estado, f"⚠️ {exc}")
    except TelegramError as exc:
        logger.warning("Error de Telegram en /emoji (usuario %s): %s", user.id, exc)
        await _editar(estado, _mensaje_error_telegram(exc, bot.username))
    except Exception:  # noqa: BLE001 - nunca dejar el mensaje de progreso colgado
        logger.exception("Error inesperado en /emoji (usuario %s)", user.id)
        await _editar(estado, "⚠️ Ocurrió un error inesperado al convertir. Probá de nuevo en un rato.")
    finally:
        _en_curso.discard(estado.chat_id)  # libera el bloqueo del chat SIEMPRE
        restantes = _pendientes_emoji.get(user.id, 1) - 1
        if restantes <= 0:
            _pendientes_emoji.pop(user.id, None)
        else:
            _pendientes_emoji[user.id] = restantes


def _lanzar(context: ContextTypes.DEFAULT_TYPE, corrutina: Awaitable[Any], update: Update) -> None:
    """Lanza una tarea en segundo plano SIN bloquear al bot (el handler
    vuelve enseguida y puede seguir atendiendo otros updates)."""
    tarea = context.application.create_task(corrutina, update=update)
    _tareas_segundo_plano.add(tarea)
    tarea.add_done_callback(_tareas_segundo_plano.discard)


async def _iniciar_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         fuente: Fuente, argumentos: list[str]) -> None:
    message, user = update.effective_message, update.effective_user
    chat_id = message.chat_id

    if chat_id in _en_curso:
        await message.reply_text("⏳ Ya hay una conversión en curso en este chat. Esperá a que termine.")
        return
    if _pendientes_emoji.get(user.id, 0) >= MAX_PENDIENTES_EMOJI:
        await message.reply_text("⏳ Ya tenés varias conversiones en curso. Esperá a que terminen y probá de nuevo.")
        return
    _en_curso.add(chat_id)  # se reserva el chat ANTES de cualquier await
    _pendientes_emoji[user.id] = _pendientes_emoji.get(user.id, 0) + 1

    try:
        estado = await message.reply_text("⏳ Convirtiendo a emoji…")
    except BaseException:
        _en_curso.discard(chat_id)
        _pendientes_emoji[user.id] = max(_pendientes_emoji.get(user.id, 1) - 1, 0)
        raise
    _lanzar(context, _tarea_emoji(context, user, fuente, _extraer_emojis(argumentos), estado), update)


_AYUDA_EMOJI = (
    "Uso de /emoji:\n"
    "• Mandá una foto, video o GIF con /emoji en el pie de foto, o\n"
    "• respondé a una foto, video, GIF o sticker con /emoji.\n"
    "Podés elegir el emoji asociado: /emoji 😎 (por defecto 🙂)."
)


async def emoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/emoji respondiendo a un archivo (el comando va en el texto del mensaje)."""
    message, user = update.effective_message, update.effective_user
    if message is None or user is None or user.is_bot:
        return
    fuente = _buscar_fuente(message.reply_to_message)
    if fuente is None:
        await message.reply_text(_AYUDA_EMOJI)
        return
    await _iniciar_emoji(update, context, fuente, list(context.args or []))


async def emoji_caption_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/emoji escrito en el pie de foto de una foto, video o animación.
    (CommandHandler solo lee el texto del mensaje, no los pies de foto,
    por eso esto va aparte con un filtro de caption.)"""
    message, user = update.effective_message, update.effective_user
    if message is None or user is None or user.is_bot or not message.caption:
        return
    coincide = _PATRON_CAPTION.match(message.caption)
    if coincide is None:
        return
    dirigido_a = coincide.group(1)
    if dirigido_a and dirigido_a.lower() != (context.bot.username or "").lower():
        return  # "/emoji@OtroBot": es para otro bot

    fuente = _buscar_fuente(message)
    if fuente is None:
        await message.reply_text(_AYUDA_EMOJI)
        return
    argumentos = message.caption.split()[1:]
    await _iniciar_emoji(update, context, fuente, argumentos)


# --------------------------------------------------------------------- #
# /emojis — convertir un pack completo de stickers
# --------------------------------------------------------------------- #
class _Progreso:
    """Edita el mensaje 'Convirtiendo 12/40…' sin pasarse del límite de
    ediciones de Telegram (como mucho una cada INTERVALO_PROGRESO s)."""

    def __init__(self, mensaje: Message, total: int) -> None:
        self._mensaje, self._total = mensaje, total
        self._ultimo = 0.0

    async def avanzar(self, hecho: int, forzar: bool = False) -> None:
        ahora = time.monotonic()
        if forzar or ahora - self._ultimo >= INTERVALO_PROGRESO:
            self._ultimo = ahora
            await _editar(self._mensaje, f"⏳ Convirtiendo {hecho}/{self._total}…")


async def _crear_pack_convertido(bot, db: Database, user, titulo_origen: str, slug_origen: str,
                                 parte: int, hay_varias: bool, total_partes: int, fmt: str,
                                 origen: str, primer_sticker: InputSticker) -> str:
    """Crea un pack de /emojis con su primer sticker. Si el nombre ya está
    ocupado (el usuario ya había convertido ese mismo pack antes) prueba
    con un sufijo único para no pisar nada. Devuelve el nombre final."""
    titulo = f"{titulo_origen} (emoji)"
    if hay_varias:
        marca = f" {parte}/{total_partes}"
        titulo = f"{titulo_origen[: 64 - len(marca) - 8]} (emoji){marca}"
    titulo = titulo[:64]

    extra = ""
    for intento in range(4):
        nombre = _nombre_pack_convertido(user.id, slug_origen, parte, hay_varias, extra, bot.username)
        try:
            await _con_reintentos(
                bot.create_new_sticker_set, user_id=user.id, name=nombre, title=titulo,
                stickers=[primer_sticker], sticker_type=StickerType.CUSTOM_EMOJI,
                write_timeout=60, read_timeout=30,
            )
            await emoji_db.create_emoji_pack(db, nombre, user.id, "converted", fmt, parte, titulo, origen, 1)
            return nombre
        except BadRequest as exc:
            if "already occupied" in str(exc).lower():
                extra = format(int(time.time()) + intento, "x")[-5:]  # sufijo corto y único
                continue
            raise
    raise ConversionError("No encontré un nombre libre para el pack. Probá de nuevo en un momento.")


async def _tarea_emojis(context: ContextTypes.DEFAULT_TYPE, user, nombre_set_origen: str,
                        estado: Message) -> None:
    """Trabajo en segundo plano de /emojis: convierte todo el pack."""
    bot, db = context.bot, context.application.bot_data["db"]
    try:
        await emoji_db.asegurar_tabla(db)  # crea la tabla de packs la primera vez
        conjunto = await _con_reintentos(bot.get_sticker_set, nombre_set_origen)
        stickers = list(conjunto.stickers)
        total = len(stickers)
        if total == 0:
            await _editar(estado, "⚠️ Ese pack está vacío.")
            return

        trozos = [stickers[i:i + MAX_EMOJIS_POR_PACK] for i in range(0, total, MAX_EMOJIS_POR_PACK)]
        hay_varias = len(trozos) > 1
        slug_origen = _slug(re.sub(r"_by_\w+$", "", conjunto.name, flags=re.IGNORECASE)) or "pack"
        progreso = _Progreso(estado, total)
        await progreso.avanzar(0, forzar=True)

        enlaces: list[str] = []
        hecho = fallidos = 0
        ultimo_error_conversion = ""

        with tempfile.TemporaryDirectory(prefix="emojis_") as tmp:
            carpeta = Path(tmp)
            for parte, trozo in enumerate(trozos, start=1):
                nombre_pack: Optional[str] = None
                for sticker in trozo:
                    hecho += 1
                    try:
                        emoji = sticker.emoji or EMOJI_POR_DEFECTO
                        fuente = Fuente(
                            "sticker_tgs" if sticker.is_animated else ("sticker_video" if sticker.is_video else "sticker_estatico"),
                            sticker.file_id, sticker.file_size, sticker.emoji,
                        )
                        # Copia el emoji asociado de cada sticker original.
                        input_sticker, fmt = await _fuente_a_input_sticker(bot, fuente, [emoji], carpeta, indice=hecho)

                        if nombre_pack is None:
                            nombre_pack = await _crear_pack_convertido(
                                bot, db, user, conjunto.title, slug_origen, parte, hay_varias,
                                len(trozos), fmt, conjunto.name, input_sticker,
                            )
                            enlaces.append(_enlace(nombre_pack))
                        else:
                            await _con_reintentos(bot.add_sticker_to_set, user_id=user.id, name=nombre_pack,
                                                  sticker=input_sticker, write_timeout=60, read_timeout=30)
                            await emoji_db.bump_emoji_pack_count(db, nombre_pack, 1)

                    except FfmpegNoDisponible:
                        raise  # sin ffmpeg no tiene sentido seguir con los demás
                    except ConversionError as exc:
                        fallidos += 1
                        ultimo_error_conversion = str(exc)
                        logger.info("Sticker %d/%d de %s no se pudo convertir: %s", hecho, total, nombre_set_origen, exc)
                    except TelegramError as exc:
                        if _es_usuario_sin_start(exc):
                            raise  # error fatal: no tiene caso seguir
                        fallidos += 1
                        logger.info("Sticker %d/%d de %s rechazado por Telegram: %s", hecho, total, nombre_set_origen, exc)

                    await progreso.avanzar(hecho)
                    await asyncio.sleep(PAUSA_ENTRE_LLAMADAS)  # pausa corta entre llamadas

        convertidos = hecho - fallidos
        if convertidos <= 0:
            detalle = f"\n{ultimo_error_conversion}" if ultimo_error_conversion else ""
            await _editar(estado, f"⚠️ No pude convertir ningún sticker de ese pack.{detalle}")
            return

        lineas = [f"✅ Listo: {convertidos}/{total} stickers convertidos a emojis."]
        if fallidos:
            lineas.append(f"⚠️ {fallidos} sticker{'s' if fallidos != 1 else ''} no se pudo convertir." if fallidos == 1
                          else f"⚠️ {fallidos} stickers no se pudieron convertir.")
        lineas.append("")
        lineas += [f"👉 {e}" for e in enlaces]
        await _editar(estado, "\n".join(lineas))

    except FfmpegNoDisponible as exc:
        logger.error("ffmpeg no está instalado: %s", exc)
        await _editar(estado, f"⚠️ {exc} Avisale al administrador del bot.")
    except ConversionError as exc:
        await _editar(estado, f"⚠️ {exc}")
    except TelegramError as exc:
        logger.warning("Error de Telegram en /emojis (usuario %s): %s", user.id, exc)
        await _editar(estado, _mensaje_error_telegram(exc, bot.username))
    except Exception:  # noqa: BLE001
        logger.exception("Error inesperado en /emojis (usuario %s)", user.id)
        await _editar(estado, "⚠️ Ocurrió un error inesperado al convertir el pack. Probá de nuevo en un rato.")
    finally:
        _packs_en_curso.discard(user.id)  # libera el cupo del usuario SIEMPRE
        _en_curso.discard(estado.chat_id)  # libera el bloqueo del chat SIEMPRE


async def emojis_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/emojis respondiendo a un sticker: convierte todo su pack."""
    message, user = update.effective_message, update.effective_user
    if message is None or user is None or user.is_bot:
        return

    replied = message.reply_to_message
    if replied is None or replied.sticker is None:
        await message.reply_text("Respondé a un sticker con /emojis y convierto el pack completo en emojis personalizados.")
        return
    if not replied.sticker.set_name:
        await message.reply_text("Ese sticker no pertenece a ningún pack. Para uno suelto usá /emoji.")
        return

    # --- Límites anti-abuso ---
    chat_id = message.chat_id
    if chat_id in _en_curso:
        await message.reply_text("⏳ Ya hay una conversión en curso en este chat. Esperá a que termine.")
        return
    if user.id in _packs_en_curso:
        await message.reply_text("⏳ Ya tenés una conversión de pack en curso. Esperá a que termine.")
        return
    if len(_packs_en_curso) >= MAX_PACKS_SIMULTANEOS:
        await message.reply_text("⏳ Hay muchas conversiones de packs en este momento. Probá de nuevo en un minuto.")
        return

    _en_curso.add(chat_id)  # se reserva el chat ANTES de cualquier await
    _packs_en_curso.add(user.id)  # se reserva el cupo ANTES de cualquier await
    try:
        estado = await message.reply_text("⏳ Preparando la conversión…")
    except BaseException:
        _packs_en_curso.discard(user.id)
        _en_curso.discard(chat_id)
        raise
    _lanzar(context, _tarea_emojis(context, user, replied.sticker.set_name, estado), update)


# --------------------------------------------------------------------- #
# Registro de handlers (se llama una vez desde main.py)
# --------------------------------------------------------------------- #
def register_emoji_handlers(application: Application) -> None:
    """Registra /emoji, /emojis y el /emoji en pie de foto.
    IMPORTANTE: debe llamarse ANTES de registrar el router de mensajes
    libres (on_message_router), porque en un mismo grupo solo corre el
    primer handler que coincide."""
    application.add_handler(CommandHandler("emoji", emoji_command, block=False))
    application.add_handler(CommandHandler("emojis", emojis_command, block=False))
    application.add_handler(
        MessageHandler(
            filters.CaptionRegex(_PATRON_CAPTION)
            & (filters.PHOTO | filters.VIDEO | filters.ANIMATION | filters.Document.ALL),
            emoji_caption_handler,
            block=False,
        )
    )
