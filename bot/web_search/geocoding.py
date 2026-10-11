"""Bounded, cached city centres for user-requested radius searches.

Only the Idealista radius path calls the public Nominatim endpoint. A shared Redis
cache prevents repeat lookups; without that cache a lookup fails closed.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import unicodedata
from collections.abc import Awaitable, Callable

import httpx
from redis.asyncio import Redis

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "real-estate-bot/1.0 (https://github.com/infinitytechmain2024/REAL-ESTATE-BOT)"


def _fold(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(c)).strip()


class CachedCityCenters:
    def __init__(self, redis_url: str, *, client: httpx.AsyncClient | None = None,
                 cache: Redis | None = None, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._cache = cache or (Redis.from_url(redis_url, decode_responses=True) if redis_url else None)
        self._client = client or httpx.AsyncClient(timeout=10, follow_redirects=False)
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()
        if self._cache is not None:
            await self._cache.aclose()

    async def resolve(self, city: str) -> tuple[float, float] | None:
        if self._cache is None or not city.strip() or len(city) > 80:
            return None
        key = "idealista:city-center:es:" + _fold(city)
        async with self._lock:
            try:
                cached = await self._cache.get(key)
            except Exception:  # noqa: BLE001 - no uncached public geocoding on Redis failure
                return None
            if cached:
                try:
                    lat, lon = json.loads(cached)
                    if _coordinates(lat, lon):
                        return float(lat), float(lon)
                except (TypeError, ValueError):
                    pass
            wait = 1.0 - (time.monotonic() - self._last_request)
            if wait > 0:
                await self._sleep(wait)
            self._last_request = time.monotonic()
            try:
                response = await self._client.get(
                    NOMINATIM_URL, params={"city": city, "countrycodes": "es", "format": "jsonv2",
                                            "addressdetails": 1, "limit": 5},
                    headers={"User-Agent": USER_AGENT}, timeout=10)
                response.raise_for_status()
                rows = response.json()
            except (httpx.HTTPError, ValueError):
                return None
            if not isinstance(rows, list):
                return None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                address = row.get("address")
                if not isinstance(address, dict) or address.get("country_code") != "es":
                    continue
                names = (row.get("name"), address.get("city"), address.get("town"), address.get("municipality"))
                if not any(isinstance(name, str) and _fold(name) == _fold(city) for name in names):
                    continue
                lat, lon = row.get("lat"), row.get("lon")
                if not _coordinates(lat, lon):
                    continue
                center = float(lat), float(lon)
                try:
                    await self._cache.setex(key, 30 * 24 * 3600, json.dumps(center))
                except Exception:  # noqa: BLE001 - never use an uncacheable public result
                    return None
                return center
            return None


def _coordinates(lat: object, lon: object) -> bool:
    try:
        a, b = float(lat), float(lon)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(a) and math.isfinite(b) and -90 <= a <= 90 and -180 <= b <= 180
