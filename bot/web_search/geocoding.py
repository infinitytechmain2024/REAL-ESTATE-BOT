"""Bounded, cached city centres for user-requested radius searches.

The portal sources (Idealista, Yaencontre, radius searches) call the public Nominatim endpoint. A shared Redis
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
        found = await self.resolve_area(city)
        return found[0] if found is not None else None

    async def resolve_area(self, city: str) -> tuple[tuple[float, float], Bbox | None] | None:
        """The city's centre and, when Nominatim gave it, its ``boundingbox`` (south, north, west, east). The cache
        holds ``[lat, lon, [south, north, west, east]]``; an entry written before the box (``[lat, lon]``) is still
        a centre, with no box."""
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
                    value = json.loads(cached)
                    lat, lon = value[0], value[1]
                    if isinstance(value, list) and len(value) in (2, 3) and _coordinates(lat, lon):
                        return (float(lat), float(lon)), (_bbox(value[2]) if len(value) == 3 else None)
                except (TypeError, ValueError, IndexError, KeyError):
                    pass
            rows = await self._search({"city": city, "countrycodes": "es", "format": "jsonv2",
                                       "addressdetails": 1, "limit": 5})
            if rows is None:
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
                box = _bbox(row.get("boundingbox"))
                try:
                    await self._cache.setex(key, 30 * 24 * 3600, json.dumps([*center, *([list(box)] if box else [])]))
                except Exception:  # noqa: BLE001 - never use an uncacheable public result
                    return None
                return center, box
            return None

    async def resolve_district(self, district: str, city: str) -> tuple[float, float] | None:
        """PORTAL_DISTRICT_SEARCH (Q26): the centre of a district of ``city`` («Centro», «Chamberí»). Only a Nominatim
        row named ``district`` whose kind is a part of a town (``_DISTRICT_TYPES``) and whose address puts it in
        ``city`` counts: a town of its own («Getafe»), a street or a shop of that name is not a district (None).
        Cached like a city centre (``[lat, lon]``); no cache, no public lookup."""
        if (self._cache is None or not district.strip() or not city.strip() or len(district) > 80
                or len(city) > 80):
            return None
        key = f"idealista:district-center:es:{_fold(city)}:{_fold(district)}"
        async with self._lock:
            try:
                cached = await self._cache.get(key)
            except Exception:  # noqa: BLE001 - no uncached public geocoding on Redis failure
                return None
            if cached:
                try:
                    value = json.loads(cached)
                    if isinstance(value, list) and len(value) == 2 and _coordinates(value[0], value[1]):
                        return float(value[0]), float(value[1])
                except (TypeError, ValueError):
                    pass
            rows = await self._search({"q": f"{district}, {city}", "countrycodes": "es", "format": "jsonv2",
                                       "addressdetails": 1, "limit": 5})
            for row in rows or ():
                if not isinstance(row, dict):
                    continue
                address = row.get("address")
                if not isinstance(address, dict) or address.get("country_code") != "es":
                    continue
                kind = row.get("addresstype")
                name = row.get("name")
                towns = (address.get("city"), address.get("town"), address.get("municipality"))
                if (not isinstance(kind, str) or kind not in _DISTRICT_TYPES
                        or not isinstance(name, str) or _fold(name) != _fold(district)
                        or not any(isinstance(town, str) and _fold(town) == _fold(city) for town in towns)):
                    continue
                lat, lon = row.get("lat"), row.get("lon")
                if not _coordinates(lat, lon):
                    continue
                center = float(lat), float(lon)
                try:
                    await self._cache.setex(key, 30 * 24 * 3600, json.dumps(list(center)))
                except Exception:  # noqa: BLE001 - never use an uncacheable public result
                    return None
                return center
            return None

    async def _search(self, params: dict[str, str | int]) -> list[object] | None:
        """One Nominatim search, at most one per second (the caller holds ``_lock``); None on any failure."""
        wait = 1.0 - (time.monotonic() - self._last_request)
        if wait > 0:
            await self._sleep(wait)
        self._last_request = time.monotonic()
        try:
            response = await self._client.get(NOMINATIM_URL, params=params, headers={"User-Agent": USER_AGENT},
                                              timeout=10)
            response.raise_for_status()
            rows = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        return rows if isinstance(rows, list) else None


# Nominatim ``addresstype`` of a part of a town (Q26): a district, a borough, a neighbourhood.
_DISTRICT_TYPES = frozenset({"city_district", "district", "borough", "suburb", "quarter", "neighbourhood"})

Bbox = tuple[float, float, float, float]


def _bbox(value: object) -> Bbox | None:
    """Nominatim's ``boundingbox`` ``[south, north, west, east]`` (strings in ``jsonv2``) when it is a sane box."""
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    try:
        south, north, west, east = (float(v) for v in value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (_coordinates(south, west) and _coordinates(north, east)) or south > north or west > east:
        return None
    return south, north, west, east


def _coordinates(lat: object, lon: object) -> bool:
    try:
        a, b = float(lat), float(lon)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(a) and math.isfinite(b) and -90 <= a <= 90 and -180 <= b <= 180
