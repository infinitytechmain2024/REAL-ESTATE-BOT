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
            "email": self.settings.extract_emails,
            "max_time": int(self.settings.timeout_seconds),
        }
        try:
            response = await self._client.post("/api/v1/jobs", json=body)
            response.raise_for_status()
            job_id = _json_object(response).get("id")
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
            payload = _json_object(response)
            # The kit currently returns ``Status``; accepting lowercase keeps
            # this adapter tolerant of proxies and future API revisions.
            status = payload.get("Status") or payload.get("status")
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
    """Convert rows to unique hits, applying the cap *after* de-duplication.

    A Maps job can return the same business for overlapping keywords. Applying
    the cap before de-duplication would waste the user's result budget on those
    repeats. ``SearchHit.url_hash`` also removes tracking parameters from
    websites, matching the pipeline's normal URL de-duplication rules.
    """
    if limit <= 0:
        return []

    unique: dict[str, SearchHit] = {}
    for row in rows:
        name = _text(row, "title", "name") or "Google Maps listing"
        address = _text(row, "address")
        website = _text(row, "website", "site", "url")
        phone = _text(row, "phone", "phone_number")
        category = _text(row, "category")
        emails = _text(row, "emails", "email")
        url = website or _maps_url(name, address)
        snippet = " | ".join(part for part in (address, phone, category, emails) if part)
        hit = SearchHit(url=url, title=name, snippet=snippet, engines=["google_maps_kit"], score=0.7)

        existing = unique.get(hit.url_hash)
        if existing is not None:
            # Keep the first canonical URL, while retaining any richer text
            # returned by a duplicate row.
            if len(hit.snippet) > len(existing.snippet):
                existing.snippet = hit.snippet
            if existing.title == "Google Maps listing" and hit.title:
                existing.title = hit.title
            continue
        unique[hit.url_hash] = hit
        if len(unique) >= limit:
            break
    return list(unique.values())


def _text(row: dict[str, Any], *keys: str) -> str:
    """Return the first non-empty row field as a safe display string."""
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        return str(value).strip()
    return ""


def _json_object(response: httpx.Response) -> dict[str, Any]:
    """Parse a sidecar response and reject valid JSON of the wrong shape."""
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Google Maps sidecar returned a non-object JSON response")
    return payload


def _maps_url(name: str, address: str) -> str:
    from urllib.parse import quote_plus

    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(f'{name} {address}')}"
