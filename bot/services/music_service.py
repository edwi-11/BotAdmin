"""
services/music_service.py
Servicio de CEO Music: agrupa los proveedores activos, mezcla sus resultados y
descarga el audio con un límite de descargas simultáneas.
"""
from __future__ import annotations

import asyncio
import logging
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

    async def download(self, track: Track) -> bytes:
        provider = self.providers.get(track.provider)
        if provider is None:
            raise ProviderError(f"{track.provider.title()} no está disponible en este momento.")
        async with self._slots:
            return await provider.download(track, MAX_AUDIO_BYTES)
