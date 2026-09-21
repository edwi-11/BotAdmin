"""
handlers/wa_stickers.py
/wasticker (alias /towa) — respondiendo a un sticker, genera un archivo
.wastickers descargable: el formato real que usa la app "Sticker Maker"
(Viko & Co) para importar packs a WhatsApp (y a iMessage en iOS).

Por qué funciona "solo tocar el archivo":
    .wastickers es, por dentro, un .zip con una estructura fija (ver más
    abajo) que Sticker Maker registró ante Android/iOS como un tipo de
    archivo que sabe abrir. Por eso, cuando alguien toca el archivo que
    manda este comando, el teléfono le ofrece abrirlo directamente con
    esa app — no es magia ni cosa nuestra, es el mismo mecanismo que usan
    los bots dedicados a esto (ej. @tgtowabot) y varias herramientas de
    código abierto (ver la constante _REFERENCE_FORMAT_DOCS más abajo).
    El bot NO puede controlar qué apps tiene instaladas cada usuario: si
    no tienen Sticker Maker (u otra app compatible con .wastickers), el
    teléfono va a ofrecer descargar el archivo o abrirlo como un .zip
    común, no hay forma de evitar eso desde acá.

Estructura interna exacta de un .wastickers (formato Viko & Co, la que
lee Sticker Maker):
    author.txt      — texto plano, el autor del pack
    title.txt       — texto plano, el nombre del pack
    icon.png        — 96x96px, ícono que se ve en la bandeja de WhatsApp
    <n>.webp        — cada figurita, 512x512px, fondo transparente

Reglas que hay que respetar si o si (si no, Sticker Maker rechaza el
archivo o WhatsApp lo importa mal):
    - Mínimo 3 stickers por pack (si hay menos, se completa con
      figuritas en blanco transparentes).
    - Máximo 30 stickers por archivo .wastickers (si el pack de origen
      tiene más, se reparte en varios archivos, cada uno instalable por
      separado).
    - Cada sticker tiene que ser EXACTAMENTE 512x512, con la imagen
      centrada y el resto transparente (no alcanza con que un lado mida
      512, como sí permite Telegram) — por eso esto no reusa
      directamente el conversor de handlers/kang.py, que arma stickers
      rectangulares para Telegram.

Limitación real (no es un bug, es cómo son los stickers): solo se pueden
convertir figuritas ESTÁTICAS (PNG/WEBP). Las animadas (.tgs) y de video
(.webm) no se pueden pasar a una imagen fija sin perder la animación —
si el pack tiene alguna, se las salta y avisa cuántas quedaron afuera.
"""
from __future__ import annotations

import io
import logging
import zipfile

from PIL import Image
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

STICKER_SIZE = 512
ICON_SIZE = 96
MAX_PER_FILE = 30
MIN_PER_FILE = 3


def _to_square_512(raw: bytes) -> bytes:
    """Redimensiona manteniendo proporción y la centra sobre un lienzo
    512x512 transparente — el requisito estricto de WhatsApp (a
    diferencia de Telegram, que solo pide que UN lado mida 512)."""
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    w, h = img.size
    if w >= h:
        new_w, new_h = STICKER_SIZE, max(1, round(h * STICKER_SIZE / w))
    else:
        new_h, new_w = STICKER_SIZE, max(1, round(w * STICKER_SIZE / h))
    img = img.resize((new_w, new_h), Image.LANCZOS)

    canvas = Image.new("RGBA", (STICKER_SIZE, STICKER_SIZE), (0, 0, 0, 0))
    canvas.paste(img, ((STICKER_SIZE - new_w) // 2, (STICKER_SIZE - new_h) // 2), img)

    buf = io.BytesIO()
    canvas.save(buf, format="WEBP", quality=90, method=6)
    return buf.getvalue()


def _blank_sticker() -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (STICKER_SIZE, STICKER_SIZE), (0, 0, 0, 0)).save(buf, format="WEBP")
    return buf.getvalue()


def _make_icon(raw: bytes) -> bytes:
    img = Image.open(io.BytesIO(raw)).convert("RGBA")
    img.thumbnail((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
    canvas = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0, 0, 0, 0))
    canvas.paste(img, ((ICON_SIZE - img.width) // 2, (ICON_SIZE - img.height) // 2), img)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def _build_wastickers_file(title: str, author: str, icon_png: bytes, stickers_webp: list[bytes]) -> bytes:
    """Arma el .zip con la estructura exacta que espera Sticker Maker."""
    stickers = list(stickers_webp)
    while len(stickers) < MIN_PER_FILE:
        stickers.append(_blank_sticker())

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("author.txt", author)
        zf.writestr("title.txt", title)
        zf.writestr("icon.png", icon_png)
        for i, webp in enumerate(stickers):
            zf.writestr(f"{i}.webp", webp)
    return buf.getvalue()


def _safe_filename(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in name).strip("_")
    return (cleaned or "stickers")[:40]


async def wasticker_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    replied = message.reply_to_message

    if replied is None or replied.sticker is None:
        await message.reply_text(
            "Respondé a un sticker con /wasticker (o /towa) y te armo un archivo para "
            "importarlo a WhatsApp con la app Sticker Maker."
        )
        return

    sticker = replied.sticker
    author = (update.effective_user.first_name or "CEO Bot")[:40]

    status = await message.reply_text("⏳ Armando el archivo...")

    # --- Reunir las figuritas a convertir: si el sticker pertenece a un
    # pack, exportamos el pack entero; si es suelto, solo esa figurita. ---
    if sticker.set_name:
        try:
            sticker_set = await context.bot.get_sticker_set(sticker.set_name)
        except TelegramError as exc:
            await status.edit_text(f"⚠️ No pude leer el paquete de ese sticker: {exc}")
            return
        title = sticker_set.title
        candidates = list(sticker_set.stickers)
    else:
        title = "Sticker"
        candidates = [sticker]

    static_candidates = [s for s in candidates if not s.is_animated and not s.is_video]
    skipped = len(candidates) - len(static_candidates)
    if not static_candidates:
        await status.edit_text(
            "⚠️ Ese paquete es animado o de video — no se puede convertir a stickers fijos de "
            "WhatsApp sin perder la animación, así que no genero nada. Funciona con paquetes "
            "estáticos (PNG/WEBP)."
        )
        return

    # --- Descargar y convertir (icono = primer sticker) ---
    try:
        icon_file = await context.bot.get_file(static_candidates[0].file_id)
        icon_raw = bytes(await icon_file.download_as_bytearray())
        icon_png = _make_icon(icon_raw)
    except TelegramError as exc:
        await status.edit_text(f"⚠️ No pude descargar el ícono del pack: {exc}")
        return

    webp_stickers: list[bytes] = []
    for s in static_candidates:
        try:
            f = await context.bot.get_file(s.file_id)
            raw = bytes(await f.download_as_bytearray())
            webp_stickers.append(_to_square_512(raw))
        except TelegramError as exc:
            logger.info("No pude convertir un sticker de %s: %s", sticker.set_name, exc)

    if not webp_stickers:
        await status.edit_text("⚠️ No pude descargar ninguna figurita del paquete.")
        return

    # --- Repartir en archivos de máximo 30, como exige el formato ---
    chunks = [webp_stickers[i:i + MAX_PER_FILE] for i in range(0, len(webp_stickers), MAX_PER_FILE)]
    base_name = _safe_filename(title)

    for idx, chunk in enumerate(chunks, start=1):
        suffix = f"-{idx}" if len(chunks) > 1 else ""
        file_title = f"{title}{suffix}"[:64]
        data = _build_wastickers_file(file_title, author, icon_png, chunk)
        caption = f"📦 {file_title} ({len(chunk)} figuritas)"
        if idx == 1:
            caption += (
                "\n\nTocá el archivo de arriba y abrilo con **Sticker Maker** → "
                "Añadir a WhatsApp.\n\n"
                "¿No la tenés instalada?\n"
                "📱 Android: https://play.google.com/store/apps/details?id=com.marsvard.stickermakerforwhatsapp\n"
                "🍎 iOS: https://apps.apple.com/app/sticker-maker-studio/id1443326857"
            )
        await context.bot.send_document(
            message.chat_id,
            document=io.BytesIO(data),
            filename=f"{base_name}{suffix}.wastickers",
            caption=caption,
        )

    aviso = ""
    if skipped:
        aviso += f"\n\n⚠️ Se saltearon {skipped} figuritas animadas/de video (no se pueden convertir)."
    if len(chunks) > 1:
        aviso += f"\n\n📋 El pack se dividió en {len(chunks)} archivos porque tiene más de {MAX_PER_FILE} figuritas — hay que instalarlos uno por uno."

    await status.delete()
    if aviso:
        await message.reply_text(aviso.strip())
