"""
utils/convertir.py
Conversión de archivos al formato de EMOJI PERSONALIZADO de Telegram.

Requisitos oficiales que cumple este módulo (https://core.telegram.org/stickers):
- Imagen estática : exactamente 100x100 px, PNG o WEBP, fondo transparente.
- Video / GIF     : exactamente 100x100 px, WEBM con códec VP9, máximo 3
                    segundos, máximo 30 FPS, SIN audio y ≤ 256 KB.
- Animado (TGS)   : se sube tal cual, no se toca (ver handlers/emoji.py).

Requiere tener `ffmpeg` instalado en el servidor (con soporte libvpx-vp9)
para los videos. Las imágenes solo usan Pillow, que ya es dependencia del bot.

Nada de este módulo habla con Telegram: recibe bytes/rutas y devuelve
bytes, así se puede probar por separado. Los errores esperables se
levantan como `ConversionError` con un mensaje listo para mostrarle al
usuario.
"""
from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

logger = logging.getLogger(__name__)

# --- Límites de Telegram para emojis personalizados ---
TAMANO_EMOJI = 100                 # px, ancho y alto exactos
MAX_SEGUNDOS_VIDEO = 3             # duración máxima del WEBM
MAX_BYTES_VIDEO = 256 * 1024       # 256 KB
MAX_FPS = 30

# Bitrates (kbps) que se van probando, de mayor a menor calidad, hasta que
# el archivo entre en los 256 KB. A 100x100 px y 3 s casi siempre alcanza
# con el primero; la escalera existe por si el contenido es muy "pesado".
_ESCALERA_BITRATE_KBPS = (300, 200, 140, 100, 70, 45, 30)

# ffmpeg es pesado: nunca más de 2 conversiones a la vez en todo el bot,
# así varios usuarios usando /emojis no saturan el CPU del VPS.
_CUPOS_FFMPEG = asyncio.Semaphore(2)
_TIMEOUT_FFMPEG_SEG = 90


class ConversionError(Exception):
    """Error esperable de conversión. `str(exc)` es apto para el usuario."""


class FfmpegNoDisponible(ConversionError):
    """ffmpeg no está instalado (o no está en el PATH) en el servidor."""


# ====================================================================== #
# Imágenes
# ====================================================================== #
def _imagen_a_emoji_sync(datos: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(datos))
        img.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ConversionError("No pude leer esa imagen: el archivo está dañado o no es una imagen válida.") from exc

    # Respeta la rotación que traen las fotos de celular (EXIF).
    img = ImageOps.exif_transpose(img).convert("RGBA")

    # "Contener": entra completa dentro de 100x100 SIN deformarse
    # (mantiene la proporción; si es chica, se agranda hasta llenar).
    img = ImageOps.contain(img, (TAMANO_EMOJI, TAMANO_EMOJI), Image.LANCZOS)

    # Lienzo transparente de 100x100 con la imagen centrada.
    # Se usa alpha_composite (y no paste con máscara) para no arruinar
    # los bordes semitransparentes.
    lienzo = Image.new("RGBA", (TAMANO_EMOJI, TAMANO_EMOJI), (0, 0, 0, 0))
    destino = ((TAMANO_EMOJI - img.width) // 2, (TAMANO_EMOJI - img.height) // 2)
    lienzo.alpha_composite(img, destino)

    salida = io.BytesIO()
    lienzo.save(salida, format="PNG", optimize=True)
    return salida.getvalue()


async def imagen_a_emoji(datos: bytes) -> bytes:
    """Convierte cualquier imagen (JPG/PNG/WEBP/...) a PNG 100x100
    transparente y centrada. Corre en un hilo para no bloquear el bot."""
    return await asyncio.to_thread(_imagen_a_emoji_sync, datos)


# ====================================================================== #
# Video / GIF / sticker de video  ->  WEBM VP9
# ====================================================================== #
def _filtro_video(fps: int) -> str:
    # 1) fps tope  2) escala sin deformar (cabe en 100x100)
    # 3) pasa a un formato con canal alfa  4) rellena con TRANSPARENTE
    #    hasta completar 100x100 (centrado).
    return (
        f"fps={fps},"
        f"scale={TAMANO_EMOJI}:{TAMANO_EMOJI}:force_original_aspect_ratio=decrease:flags=lanczos,"
        "format=yuva420p,"
        f"pad={TAMANO_EMOJI}:{TAMANO_EMOJI}:(ow-iw)/2:(oh-ih)/2:color=black@0"
    )


async def _ejecutar_ffmpeg(argumentos: list[str]) -> None:
    async with _CUPOS_FFMPEG:
        try:
            proceso = await asyncio.create_subprocess_exec(
                "ffmpeg", *argumentos,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise FfmpegNoDisponible(
                "El servidor no tiene ffmpeg instalado, así que no puedo convertir videos."
            ) from exc

        try:
            _, stderr = await asyncio.wait_for(proceso.communicate(), timeout=_TIMEOUT_FFMPEG_SEG)
        except asyncio.TimeoutError as exc:
            proceso.kill()
            await proceso.wait()
            raise ConversionError("La conversión del video tardó demasiado y la cancelé.") from exc

    if proceso.returncode != 0:
        detalle = stderr.decode("utf-8", "replace").strip().splitlines()[-3:]
        logger.warning("ffmpeg falló (código %s): %s", proceso.returncode, " | ".join(detalle))
        raise ConversionError("ffmpeg no pudo procesar ese video (¿archivo dañado o formato raro?).")


async def video_a_emoji_webm(ruta_entrada: Path, ruta_salida: Path, entrada_es_webm_vp9: bool = False) -> bytes:
    """Convierte un video/GIF/animación a WEBM VP9 100x100, ≤3 s, sin
    audio y ≤256 KB. Si el resultado pesa de más, repite bajando el
    bitrate (y al final también los FPS) hasta que entre en el límite.

    `entrada_es_webm_vp9=True` hay que usarlo con stickers de video de
    Telegram: son WEBM con transparencia y el decodificador por defecto de
    ffmpeg pierde el canal alfa; forzando `libvpx-vp9` se conserva.

    Devuelve los bytes del .webm final. Lanza ConversionError si no se
    logra cumplir el límite o si ffmpeg falla.
    """
    ultimo_tamano = 0
    for indice, kbps in enumerate(_ESCALERA_BITRATE_KBPS):
        # En los dos últimos intentos, además bajamos los cuadros por segundo.
        fps = MAX_FPS if indice < len(_ESCALERA_BITRATE_KBPS) - 2 else 15

        argumentos = ["-y", "-hide_banner", "-loglevel", "error"]
        if entrada_es_webm_vp9:
            argumentos += ["-c:v", "libvpx-vp9"]
        argumentos += [
            "-i", str(ruta_entrada),
            "-map", "0:v:0",                  # solo la pista de video
            "-an", "-sn", "-dn",              # sin audio, subtítulos ni datos
            "-t", str(MAX_SEGUNDOS_VIDEO),    # recorta a 3 segundos
            "-vf", _filtro_video(fps),
            "-c:v", "libvpx-vp9",
            "-pix_fmt", "yuva420p",
            "-b:v", f"{kbps}k", "-crf", "32",
            "-auto-alt-ref", "0",             # necesario para conservar transparencia
            "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
            "-f", "webm",
            str(ruta_salida),
        ]
        await _ejecutar_ffmpeg(argumentos)

        ultimo_tamano = ruta_salida.stat().st_size
        if 0 < ultimo_tamano <= MAX_BYTES_VIDEO:
            return ruta_salida.read_bytes()
        logger.info("WEBM de %d bytes con %dk (límite %d): bajo el bitrate", ultimo_tamano, kbps, MAX_BYTES_VIDEO)

    raise ConversionError(
        f"No logré que el video pese menos de 256 KB (el mejor intento pesó {ultimo_tamano // 1024} KB)."
    )
