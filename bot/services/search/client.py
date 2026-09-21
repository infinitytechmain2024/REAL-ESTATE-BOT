"""Async client for the SearXNG JSON API.

Talks to ``GET /search?format=json`` on the instance vendored in ``searxng/``.
Beyond the HTTP call it does the two things the pipeline always needs: run
several queries concurrently, and merge their hits into one de-duplicated,
ranked list.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import httpx
from pydantic import BaseModel, Field

from bot.config import SearxngSettings
from bot.exceptions import SearchError
from bot.logging_conf import get_logger
from bot.models.query import SearchQuery
from bot.models.result import SearchHit
from bot.utils.urls import domain_of, url_hash

log = get_logger(__name__)


class SearchBatch(BaseModel):
    """The hits of one round of searching, with the counts behind them.

    The counts exist so the bot can tell the user what actually happened
    instead of a number that is really just a cap: ``raw`` entries came back
    from the engines, they collapsed to ``unique`` distinct URLs, and
    ``truncated`` says whether the cap then cut the list short. Reporting
    ``len(hits)`` alone is what made every answer claim the same suspiciously
    round figure.
    """

    hits: list[SearchHit] = Field(default_factory=list)
    raw: int = 0
    """Entries returned by the engines, before de-duplication."""
    unique: int = 0
    """Distinct URLs after merging and dropping blocked domains."""
    blocked: int = 0
    """Hits dropped because their host is on SEARXNG_BLOCKED_DOMAINS."""
    truncated: bool = False
    """Whether SEARXNG_MAX_HITS cut the merged list short."""
    failed_queries: int = 0
    """Queries that errored out; their hits are missing from this batch."""


class SearXNGClient:
    """Thin, retrying wrapper around the SearXNG search endpoint."""

    def __init__(self, settings: SearxngSettings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.url,
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=True,
            headers={
                "Accept": "application/json",
                # SearXNG's bot detection logs an error for every request that
                # arrives without a forwarded-for header, even with the limiter
                # off. We are the only client and we are on loopback.
                "X-Forwarded-For": "127.0.0.1",
            },
        )
        self._semaphore = asyncio.Semaphore(settings.concurrency)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        """Whether the instance answers ``/healthz``. Used at start-up."""
        try:
            response = await self._client.get("/healthz", timeout=5.0)
        except httpx.HTTPError as exc:
            log.warning("searxng.health.failed", url=self.settings.url, error=str(exc))
            return False
        return response.status_code == 200

    async def search(self, query: SearchQuery) -> list[SearchHit]:
        """Run one query. Returns an empty list rather than raising on a miss."""
        params: dict[str, Any] = {
            "q": query.query,
            "format": "json",
            "safesearch": 0,
        }
        if self.settings.engines:
            params["engines"] = ",".join(self.settings.engines)
        if query.language and query.language != "all":
            params["language"] = query.language
        if query.pages > 1:
            params["pageno"] = query.pages

        payload = await self._get_with_retries(params)

        hits: list[SearchHit] = []
        for item in payload.get("results", []):
            hit = _to_hit(item, query.query)
            if hit is not None:
                hits.append(hit)

        unresponsive = payload.get("unresponsive_engines") or []
        log.info(
            "searxng.query.done",
            query=query.query,
            hits=len(hits),
            unresponsive=[e[0] if isinstance(e, list) else e for e in unresponsive],
        )
        return hits

    async def search_many(self, queries: list[SearchQuery]) -> SearchBatch:
        """Run *queries* concurrently and merge the results.

        A failing query is logged and skipped: partial results beat no results.
        Everything fails only if every query failed, which means SearXNG itself
        is down and the user should be told.
        """
        if not queries:
            return SearchBatch()

        failures = 0

        async def one(query: SearchQuery) -> list[SearchHit]:
            nonlocal failures
            async with self._semaphore:
                try:
                    return await self.search(query)
                except SearchError as exc:
                    log.warning("searxng.query.failed", query=query.query, error=str(exc))
                    failures += 1
                    return []

        batches = await asyncio.gather(*(one(q) for q in queries))
        # Distinguish "nothing matched" from "the service is broken": an empty
        # result set is a valid answer, an unreachable instance is not.
        if all(not batch for batch in batches) and not await self.health():
            raise SearchError(f"SearXNG at {self.settings.url} is not responding")

        weights = {q.query: q.weight for q in queries}
        raw_hits = [hit for batch in batches for hit in batch]
        batch = self.merge_batch(raw_hits, weights=weights, limit=self.settings.max_hits)
        batch.failed_queries = failures
        if batch.truncated:
            # Worth its own line: a saturated cap is exactly what makes every
            # answer report the same number, and it means results were thrown
            # away before anything read them.
            log.info(
                "searxng.merge.truncated",
                unique=batch.unique,
                cap=self.settings.max_hits,
                dropped=batch.unique - len(batch.hits),
            )
        return batch

    def merge(
        self,
        hits: list[SearchHit],
        *,
        weights: dict[str, float] | None = None,
        limit: int | None = None,
    ) -> list[SearchHit]:
        """De-duplicate by ``url_hash``, drop blocked domains, sort by score."""
        return self.merge_batch(hits, weights=weights, limit=limit).hits

    def merge_batch(
        self,
        hits: list[SearchHit],
        *,
        weights: dict[str, float] | None = None,
        limit: int | None = None,
    ) -> SearchBatch:
        """:meth:`merge`, plus the counts that describe what it did.

        A URL found by several queries or engines is a stronger signal than one
        found by a single query, so duplicates accumulate score and merge their
        engine lists instead of being discarded outright.

        Hits from a trusted source (a Facebook group post we read ourselves)
        skip the blocked-domain filter: that list is there to keep social-media
        noise out of *engine* results, and it would otherwise silently delete
        every post the Facebook source just went and fetched.
        """
        weights = weights or {}
        merged: dict[str, SearchHit] = {}
        blocked = 0

        for hit in hits:
            if not hit.source.trusted and self.is_blocked(hit.url):
                blocked += 1
                continue
            key = hit.url_hash
            weight = weights.get(hit.query or "", 1.0)
            existing = merged.get(key)
            if existing is None:
                merged[key] = hit.model_copy(update={"score": hit.score * weight})
                continue

            existing.score += hit.score * weight
            # Prefer the tidier spelling of the same URL -- it is what the user
            # will see and click.
            if _url_preference(hit.url) < _url_preference(existing.url):
                existing.url = hit.url
            for engine in hit.engines:
                if engine not in existing.engines:
                    existing.engines.append(engine)
            # Keep the longest snippet: engines truncate differently and the
            # LLM ranks better with more context.
            if len(hit.snippet) > len(existing.snippet):
                existing.snippet = hit.snippet
            if not existing.title and hit.title:
                existing.title = hit.title
            # A URL the engines also found, that we additionally read inside a
            # group, keeps the read text and the stronger provenance.
            if hit.source.trusted:
                existing.source = hit.source
                if hit.content and not existing.content:
                    existing.content = hit.content

        ordered = sorted(merged.values(), key=lambda h: h.score, reverse=True)
        kept = ordered[:limit] if limit else ordered
        return SearchBatch(
            hits=kept,
            raw=len(hits),
            unique=len(ordered),
            blocked=blocked,
            truncated=bool(limit) and len(ordered) > len(kept),
        )

    def is_blocked(self, url: str) -> bool:
        """Whether *url* is on a blocked host (suffix match, so subdomains count)."""
        host = domain_of(url)
        if not host:
            return True
        return any(
            host == blocked or host.endswith(f".{blocked}")
            for blocked in self.settings.blocked_domains
        )

    async def _get_with_retries(self, params: dict[str, Any]) -> dict[str, Any]:
        """GET /search, retrying transient failures."""
        last_error: Exception | None = None

        for attempt in range(self.settings.max_retries + 1):
            try:
                response = await self._client.get("/search", params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                status = exc.response.status_code
                # 4xx other than 429 will not improve on retry.
                if status < 500 and status != 429:
                    raise SearchError(
                        f"SearXNG returned HTTP {status} for {params.get('q')!r}"
                    ) from exc
            except httpx.HTTPError as exc:
                last_error = exc
            except ValueError as exc:  # json decode
                raise SearchError("SearXNG returned a non-JSON body") from exc

            if attempt < self.settings.max_retries:
                await asyncio.sleep(1.0 * (2**attempt))

        raise SearchError(f"SearXNG request failed: {last_error}")


def _url_preference(url: str) -> tuple[int, int, int]:
    """Sort key picking the nicest spelling of one document (lower is better).

    Engines return the same page as ``http://www.x.com/a/?utm=1`` and
    ``https://x.com/a``. All of them hash identically, but only one gets shown,
    so prefer https, then the fewest query parameters, then the shortest.
    """
    return (
        0 if url.startswith("https://") else 1,
        url.count("&") + (1 if "?" in url else 0),
        len(url),
    )


def _to_hit(item: dict[str, Any], query: str) -> SearchHit | None:
    """Convert one SearXNG result entry into a :class:`SearchHit`.

    SearXNG mixes result types in the same array (web results, infoboxes,
    images); anything without a usable URL is dropped.
    """
    url = (item.get("url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        return None

    published: dt.datetime | None = None
    raw_date = item.get("publishedDate")
    if isinstance(raw_date, str) and raw_date:
        try:
            published = dt.datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
        except ValueError:
            published = None

    engines = item.get("engines") or ([item["engine"]] if item.get("engine") else [])

    return SearchHit(
        url=url,
        title=item.get("title") or "",
        snippet=item.get("content") or "",
        engines=[str(e) for e in engines],
        # SearXNG's own score is a small float; keep it but floor it at a
        # positive value so a scoreless hit still participates in ranking.
        score=float(item.get("score") or 0.0) or 0.1,
        published_at=published,
        query=query,
    )


__all__ = ["SearXNGClient", "SearchBatch", "url_hash"]
