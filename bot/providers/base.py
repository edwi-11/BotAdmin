"""
providers/base.py
Modelo común (Track), error común (ProviderError) y clase base de los proveedores
musicales de CEO Music. Cada proveedor (Audius, Jamendo) solo implementa
search() y trending(); la descarga del audio es común.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx


class ProviderError(Exception):
    """Error controlado de un proveedor. Su mensaje es seguro de mostrar al usuario
    (nunca contiene URLs con credenciales)."""


@dataclass(frozen=True)
class Track:
    provider: str          # "audius" | "jamendo"
    track_id: str
    title: str
    artist: str
    duration: int          # segundos; 0 si se desconoce
    stream_url: str        # URL de audio SIN credenciales (el proveedor añade las suyas al descargar)
    page_url: str = ""     # Página pública de la canción
    license_url: str = ""  # Licencia (Creative Commons en Jamendo)

    @property
    def duration_text(self) -> str:
        if self.duration <= 0:
            return ""
        minutes, seconds = divmod(self.duration, 60)
        return f"{minutes}:{seconds:02d}"


class MusicProvider(ABC):
    name: str = ""    # identificador interno (se usa en callbacks y comandos)
    label: str = ""   # nombre visible para el usuario

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    @abstractmethod
    async def search(self, query: str, limit: int) -> list[Track]: ...

    @abstractmethod
    async def trending(self, limit: int) -> list[Track]: ...

    # --- Ganchos para credenciales (los proveedores los sobrescriben) ---
    def _auth_headers(self) -> dict[str, str]:
        return {}

    def _auth_params(self) -> dict[str, str]:
        return {}

    async def audio_url(self, track: Track) -> str:
        """URL directa de audio que Telegram pueda descargar por sí mismo (modo inline)."""
        return track.stream_url

    # --- Utilidades HTTP comunes ---
    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        """GET que devuelve JSON. Convierte cualquier fallo en ProviderError sin
        incluir la URL (las URLs de la API pueden llevar la API key)."""
        try:
            resp = await self._client.get(url, params=params, headers=self._auth_headers())
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"{self.label} respondió con error HTTP {exc.response.status_code}."
            ) from None
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"No pude conectar con {self.label} ({type(exc).__name__})."
            ) from None
        except ValueError:
            raise ProviderError(f"{self.label} devolvió una respuesta inválida.") from None
        if not isinstance(data, dict):
            raise ProviderError(f"{self.label} devolvió una respuesta inesperada.")
        return data

    async def download(self, track: Track, max_bytes: int) -> bytes:
        """Descarga el audio completo en memoria (Telegram no puede enviar la URL si
        requiere cabeceras de autenticación). Respeta el tope max_bytes."""
        try:
            async with self._client.stream(
                "GET",
                track.stream_url,
                params=self._auth_params() or None,
                headers=self._auth_headers(),
                follow_redirects=True,
            ) as resp:
                resp.raise_for_status()
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ProviderError("La canción es demasiado grande para enviarla por Telegram.")
                buffer = bytearray()
                async for chunk in resp.aiter_bytes(65536):
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        raise ProviderError("La canción es demasiado grande para enviarla por Telegram.")
        except httpx.HTTPStatusError as exc:
            raise ProviderError(
                f"{self.label} no permitió descargar la canción (HTTP {exc.response.status_code})."
            ) from None
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"Falló la descarga desde {self.label} ({type(exc).__name__})."
            ) from None
        if not buffer:
            raise ProviderError(f"{self.label} devolvió un audio vacío.")
        return bytes(buffer)
