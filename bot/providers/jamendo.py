"""
providers/jamendo.py
Integración con la API v3.0 de Jamendo (https://api.jamendo.com/v3.0).
Música con licencias Creative Commons.

Credencial (.env): JAMENDO_CLIENT_ID.
"""
from __future__ import annotations

import httpx

from providers.base import MusicProvider, ProviderError, Track

API_BASE = "https://api.jamendo.com/v3.0"


class JamendoProvider(MusicProvider):
    name = "jamendo"
    label = "Jamendo"

    def __init__(self, client: httpx.AsyncClient, client_id: str) -> None:
        super().__init__(client)
        self._client_id = client_id

    async def _tracks(self, params: dict) -> list[Track]:
        payload = await self._get_json(
            f"{API_BASE}/tracks/",
            params={
                "client_id": self._client_id,
                "format": "json",
                "audioformat": "mp32",
                **params,
            },
        )
        headers = payload.get("headers") if isinstance(payload.get("headers"), dict) else {}
        if headers.get("status") not in (None, "success"):
            # El mensaje de Jamendo no incluye credenciales, pero se limita por si acaso.
            detail = str(headers.get("error_message") or headers.get("warnings") or "error desconocido")
            raise ProviderError(f"Jamendo rechazó la consulta: {detail[:120]}")
        results = payload.get("results")
        if not isinstance(results, list):
            raise ProviderError("Jamendo devolvió una respuesta inesperada.")
        tracks: list[Track] = []
        for raw in results:
            track = self._parse(raw)
            if track is not None:
                tracks.append(track)
        return tracks

    async def search(self, query: str, limit: int) -> list[Track]:
        return await self._tracks({"search": query, "limit": limit})

    async def trending(self, limit: int) -> list[Track]:
        return await self._tracks({"order": "popularity_week", "limit": limit})

    @staticmethod
    def _parse(raw: object) -> Track | None:
        if not isinstance(raw, dict):
            return None
        track_id = raw.get("id")
        title = (raw.get("name") or "").strip()
        audio = (raw.get("audio") or "").strip()  # URL de streaming (vacía si no se permite)
        if not track_id or not title or not audio:
            return None
        try:
            duration = int(raw.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        return Track(
            provider="jamendo",
            track_id=str(track_id),
            title=title,
            artist=(raw.get("artist_name") or "Desconocido").strip(),
            duration=duration,
            stream_url=audio,
            page_url=(raw.get("shareurl") or "").strip(),
            license_url=(raw.get("license_ccurl") or "").strip(),
        )
