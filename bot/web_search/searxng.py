"""A small async client for the internal SearXNG service (``GET /search?format=json``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""


class SearchError(RuntimeError):
    """SearXNG did not answer usefully; ``code`` is safe to log."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Searcher(Protocol):
    async def search(self, query: str, *, language: str | None = None) -> list[SearchHit]: ...


class SearxngClient:
    """One GET per query, no retries (a failed query is recorded and not searched again)."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 20, max_results: int = 10,
                 client: httpx.AsyncClient | None = None) -> None:
        self.max_results = max_results
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout_seconds), trust_env=False,
            # SearXNG's bot detection wants a forwarded-for header even with the limiter off;
            # the only client is this worker on the private Docker network.
            headers={"Accept": "application/json", "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, *, language: str | None = None) -> list[SearchHit]:
        params: dict[str, Any] = {"q": query, "format": "json", "safesearch": 0, "pageno": 1, "categories": "general"}
        if language:
            params["language"] = language
        try:
            response = await self._client.get("/search", params=params)
        except httpx.TimeoutException as exc:
            raise SearchError("timeout") from exc
        except httpx.HTTPError as exc:
            raise SearchError(f"network_error:{type(exc).__name__}") from exc
        if response.status_code != 200:
            raise SearchError(f"http_{response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SearchError("not_json") from exc
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for item in payload.get("results") or []:
            url = str(item.get("url") or "").strip() if isinstance(item, dict) else ""
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            seen.add(url)
            hits.append(SearchHit(url, str(item.get("title") or "")[:300], str(item.get("content") or "")[:500]))
            if len(hits) >= self.max_results:
                break
        silent = [str(e[0] if isinstance(e, list | tuple) and e else e) for e in payload.get("unresponsive_engines") or []]
        if silent:
            log.warning("web_search.engines_silent %s", ",".join(sorted(set(silent)))[:300])
        return hits
