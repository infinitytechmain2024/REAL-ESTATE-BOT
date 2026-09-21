"""Adapter for the local Google Maps Scraper Kit sidecar."""

from __future__ import annotations

import asyncio
import csv
import io
from typing import Any

import httpx

from bot.config import GoogleMapsSettings
from bot.logging_conf import get_logger
from bot.models.query import ParsedQuery
from bot.models.result import SearchHit
from bot.services.pipeline import SourceSearchResult

log = get_logger(__name__)


class GoogleMapsSource:
    """Create one conservative Maps job and turn its businesses into SearchHits."""

    def __init__(self, settings: GoogleMapsSettings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.timeout_seconds),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, parsed: ParsedQuery) -> SourceSearchResult:
        if self.settings.latitude is None or self.settings.longitude is None:
            return SourceSearchResult(
                failed=True,
                notes=["Google Maps отключён: задайте GOOGLE_MAPS_LATITUDE и GOOGLE_MAPS_LONGITUDE."],
            )
        keywords = _keywords(parsed)
        if not keywords:
            return SourceSearchResult()
        body = {
            "name": "real-estate-bot",
            "keywords": keywords,
            "lang": "en",
            "zoom": 15,
            "lat": str(self.settings.latitude),
            "lon": str(self.settings.longitude),
            "fast_mode": False,
            "radius": self.settings.radius_meters,
            "depth": self.settings.depth,
            "email": True,
            "max_time": int(self.settings.timeout_seconds),
        }
        try:
            response = await self._client.post("/api/v1/jobs", json=body)
            response.raise_for_status()
            job_id = response.json().get("id")
            if not job_id:
                return SourceSearchResult(failed=True, notes=["Google Maps не вернул id задания."])
            rows = await self._poll(job_id)
            return SourceSearchResult(hits=_rows_to_hits(rows, limit=self.settings.max_results))
        except (httpx.HTTPError, ValueError, TimeoutError) as exc:
            log.warning("google_maps.source.failed", error=str(exc))
            return SourceSearchResult(
                failed=True,
                notes=["Google Maps Scraper Kit недоступен; остальные источники продолжили поиск."],
            )

    async def _poll(self, job_id: str) -> list[dict[str, Any]]:
        deadline = asyncio.get_running_loop().time() + self.settings.timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            response = await self._client.get(f"/api/v1/jobs/{job_id}")
            response.raise_for_status()
            status = response.json().get("Status")
            if status == "ok":
                download = await self._client.get(f"/api/v1/jobs/{job_id}/download")
                download.raise_for_status()
                return list(csv.DictReader(io.StringIO(download.text)))
            if status == "failed":
                raise ValueError("Google Maps job failed")
            await asyncio.sleep(self.settings.poll_seconds)
        raise TimeoutError("Google Maps job timed out")


def _keywords(parsed: ParsedQuery) -> list[str]:
    location = parsed.location.as_text()
    base = "real estate agency" if parsed.mode.value == "investors" else "land for sale"
    terms = [" ".join(part for part in (base, location) if part)]
    terms.extend(parsed.keywords[:2])
    return list(dict.fromkeys(term for term in terms if term))


def _rows_to_hits(rows: list[dict[str, Any]], *, limit: int = 30) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for row in rows[:limit]:
        name = str(row.get("title") or "Google Maps listing").strip()
        address = str(row.get("address") or "").strip()
        website = str(row.get("website") or "").strip()
        phone = str(row.get("phone") or "").strip()
        url = website or _maps_url(name, address)
        snippet = " | ".join(part for part in (address, phone, row.get("category"), row.get("emails")) if part)
        hits.append(
            SearchHit(url=url, title=name, snippet=snippet, engines=["google_maps_kit"], score=0.7)
        )
    return hits


def _maps_url(name: str, address: str) -> str:
    from urllib.parse import quote_plus

    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(f'{name} {address}')}"
