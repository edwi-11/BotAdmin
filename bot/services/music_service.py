"""
services/music_service.py
Servicio de CEO Music: agrupa los proveedores activos, mezcla sus resultados y
descarga el audio con un límite de descargas simultáneas.
"""
from __future__ import annotations

import asyncio
import logging
import time
from itertools import zip_longest
from typing import Awaitable, Callable

import httpx

from providers.audius import AudiusProvider
from providers.base import MusicProvider, ProviderError, Track
from providers.jamendo import JamendoProvider
from services.music_config import MusicSettings

logger = logging.getLogger(__name__)

PER_PROVIDER_LIMIT = 10                    # Resultados pedidos a cada proveedor
MAX_PARALLEL_DOWNLOADS = 3
MAX_AUDIO_BYTES = 48 * 1024 * 1024         # Telegram permite subir hasta 50 MB como bot
INLINE_MAX_RESULTS = 20
INLINE_CACHE_TTL = 120                     # Segundos que se reutiliza una búsqueda inline igual
INLINE_CACHE_MAX = 100
INLINE_RESOLVE_TIMEOUT = 6.0               # Segundos máximos para resolver la URL de cada audio


class MusicService:
    def __init__(self, settings: MusicSettings) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=10.0),
            headers={"User-Agent": "CEO-Music/1.0"},
            follow_redirects=True,
        )
        self.providers: dict[str, MusicProvider] = {}
        if settings.audius_enabled:
            self.providers["audius"] = AudiusProvider(
                self._client, settings.audius_api_key, settings.audius_bearer_token
            )
        if settings.jamendo_enabled:
            self.providers["jamendo"] = JamendoProvider(self._client, settings.jamendo_client_id)
        self._slots = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)
        self._inline_cache: dict[str, tuple[float, list[tuple[Track, str]]]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    def label_of(self, provider_name: str) -> str:
        provider = self.providers.get(provider_name)
        return provider.label if provider else provider_name.title()

    def _select(self, provider: str | None) -> list[MusicProvider]:
        if provider is None:
            return list(self.providers.values())
        if provider not in self.providers:
            raise ProviderError(f"{provider.title()} no está disponible en este momento.")
        return [self.providers[provider]]

    async def _collect(
        self, call: Callable[[MusicProvider], Awaitable[list[Track]]], provider: str | None
    ) -> list[Track]:
        targets = self._select(provider)
        outcomes = await asyncio.gather(*(call(p) for p in targets), return_exceptions=True)
        lists: list[list[Track]] = []
        failures: list[str] = []
        for target, outcome in zip(targets, outcomes):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            if isinstance(outcome, BaseException):
                logger.warning("[%s] consulta fallida: %s", target.name, outcome)
                failures.append(str(outcome) if isinstance(outcome, ProviderError) else f"Error con {target.label}.")
            else:
                lists.append(outcome)
        if not lists:
            raise ProviderError(failures[0] if failures else "No hay proveedores disponibles.")
        # Intercala los resultados para que ningún proveedor domine la lista.
        mixed = [t for group in zip_longest(*lists) for t in group if t is not None]
        return mixed

    async def search(self, query: str, provider: str | None = None) -> list[Track]:
        return await self._collect(lambda p: p.search(query, PER_PROVIDER_LIMIT), provider)

    async def trending(self, provider: str | None = None) -> list[Track]:
        return await self._collect(lambda p: p.trending(PER_PROVIDER_LIMIT), provider)

    async def inline_tracks(self, query: str) -> list[tuple[Track, str]]:
        """Canciones para el modo inline (@bot texto): pares (Track, URL directa de audio).
        Sin texto devuelve las tendencias. Reutiliza resultados recientes de la misma consulta."""
        key = query.lower()
        now = time.monotonic()
        cached = self._inline_cache.get(key)
        if cached and now - cached[0] < INLINE_CACHE_TTL:
            return cached[1]

        tracks = await (self.search(query) if query else self.trending())
        tracks = tracks[:INLINE_MAX_RESULTS]

        async def resolve(track: Track) -> tuple[Track, str] | None:
            provider = self.providers[track.provider]
            try:
                return track, await asyncio.wait_for(provider.audio_url(track), INLINE_RESOLVE_TIMEOUT)
            except Exception as exc:  # una pista que falle no debe tumbar todo el listado
                logger.warning("[%s] inline: sin URL de audio (%s)", track.provider, type(exc).__name__)
                return None

        items = [r for r in await asyncio.gather(*(resolve(t) for t in tracks)) if r is not None]
        self._inline_cache[key] = (now, items)
        while len(self._inline_cache) > INLINE_CACHE_MAX:
            self._inline_cache.pop(next(iter(self._inline_cache)))
        return items

    async def download(self, track: Track) -> bytes:
        provider = self.providers.get(track.provider)
        if provider is None:
            raise ProviderError(f"{track.provider.title()} no está disponible en este momento.")
        async with self._slots:
            return await provider.download(track, MAX_AUDIO_BYTES)
