"""
providers/audius.py
Integración con la API REST de Audius (https://api.audius.co/v1).

Credenciales (.env): AUDIUS_API_KEY y/o AUDIUS_BEARER_TOKEN. El bearer token se envía
en la cabecera Authorization (es el método recomendado por Audius, solo servidor) y
la API key como parámetro api_key.
"""
from __future__ import annotations

import logging

import httpx

from providers.base import MusicProvider, ProviderError, Track

logger = logging.getLogger(__name__)

API_BASE = "https://api.audius.co/v1"
SITE_BASE = "https://audius.co"
APP_NAME = "CEO_Music"


class AudiusProvider(MusicProvider):
    name = "audius"
    label = "Audius"

    def __init__(self, client: httpx.AsyncClient, api_key: str = "", bearer_token: str = "") -> None:
        super().__init__(client)
        self._api_key = api_key
        self._bearer_token = bearer_token

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        return headers

    def _auth_params(self) -> dict[str, str]:
        params = {"app_name": APP_NAME}
        if self._api_key:
            params["api_key"] = self._api_key
        return params

    async def _tracks(self, path: str, params: dict) -> list[Track]:
        payload = await self._get_json(f"{API_BASE}{path}", params={**self._auth_params(), **params})
        raw_tracks = payload.get("data")
        if not isinstance(raw_tracks, list):
            raise ProviderError("Audius devolvió una respuesta inesperada.")
        tracks: list[Track] = []
        for raw in raw_tracks:
            track = self._parse(raw)
            if track is not None:
                tracks.append(track)
        return tracks

    async def search(self, query: str, limit: int) -> list[Track]:
        return await self._tracks("/tracks/search", {"query": query, "limit": limit})

    async def trending(self, limit: int) -> list[Track]:
        return await self._tracks("/tracks/trending", {"time": "week", "limit": limit})

    async def audio_url(self, track: Track) -> str:
        """El endpoint /stream de Audius responde con una redirección al nodo que aloja el mp3.
        Se pide con las credenciales (sin seguirla) y se devuelve la URL final, que Telegram
        puede descargar sin cabeceras de autenticación."""
        try:
            async with self._client.stream(
                "GET",
                track.stream_url,
                params=self._auth_params(),
                headers=self._auth_headers(),
                follow_redirects=False,
            ) as resp:
                location = resp.headers.get("location", "")
                if resp.status_code in (301, 302, 303, 307, 308) and location.startswith("http"):
                    return location
                status = resp.status_code
        except httpx.HTTPError as exc:
            raise ProviderError(f"No pude obtener el audio de Audius ({type(exc).__name__}).") from None
        raise ProviderError(f"Audius no devolvió un enlace de audio (HTTP {status}).")

    @staticmethod
    def _parse(raw: object) -> Track | None:
        if not isinstance(raw, dict):
            return None
        track_id = raw.get("id")
        title = (raw.get("title") or "").strip()
        if not track_id or not title:
            return None
        # Se omiten pistas borradas, no reproducibles o con acceso restringido (premium).
        if raw.get("is_delete") or raw.get("is_streamable") is False:
            return None
        access = raw.get("access")
        if isinstance(access, dict) and access.get("stream") is False:
            return None
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        artist = (user.get("name") or user.get("handle") or "Desconocido").strip()
        permalink = raw.get("permalink") or ""
        try:
            duration = int(raw.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0
        return Track(
            provider="audius",
            track_id=str(track_id),
            title=title,
            artist=artist,
            duration=duration,
            stream_url=f"{API_BASE}/tracks/{track_id}/stream",
            page_url=f"{SITE_BASE}{permalink}" if permalink.startswith("/") else "",
        )
