"""Extra search backends beside SearXNG (Google Programmable Search, SerpAPI) and the merger of several."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from .searxng import SearchError, SearchHit
from .urls import url_key

log = logging.getLogger(__name__)

BACKEND_NAMES = ("searxng", "google_cse", "serpapi")


class SearchBackend(Protocol):
    async def search(self, query: str, *, language: str | None = None, pages: int | None = None) -> list[SearchHit]: ...

    async def aclose(self) -> None: ...


def _check(response: httpx.Response) -> dict[str, Any]:
    if response.status_code in (402, 403, 429):
        raise SearchError("quota")
    if response.status_code != 200:
        raise SearchError(f"http_{response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise SearchError("not_json") from exc
    return payload if isinstance(payload, dict) else {}


async def _get(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> dict[str, Any]:
    # Errors carry only the exception class: httpx messages may contain the URL (and so the key).
    try:
        response = await client.get(url, params=params)
    except httpx.TimeoutException:
        raise SearchError("timeout") from None
    except httpx.HTTPError as exc:
        raise SearchError(f"network_error:{type(exc).__name__}") from None
    return _check(response)


def _hit(url: Any, title: Any, snippet: Any, engine: str) -> SearchHit | None:
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        return None
    return SearchHit(url, str(title or "")[:300], str(snippet or "")[:500], engine)


class GoogleCseClient:
    """Google Programmable Search JSON API; 10 results per page, ``start`` = 1, 11, 21."""

    name = "google_cse"
    URL = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, api_key: str, cx: str, *, max_results: int = 10, timeout: float = 20, pages: int = 3,
                 country: str = "es", client: httpx.AsyncClient | None = None) -> None:
        self._key, self._cx = api_key, cx
        self.max_results, self.pages, self.country = max_results, max(1, min(3, pages)), country
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout), trust_env=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, *, language: str | None = None, pages: int | None = None) -> list[SearchHit]:
        wanted = max(1, min(3, pages if pages is not None else self.pages))
        hits: list[SearchHit] = []
        for page in range(wanted):
            params: dict[str, Any] = {"key": self._key, "cx": self._cx, "q": query, "num": 10,
                                      "start": 1 + 10 * page, "gl": self.country}
            if language:
                params["lr"] = f"lang_{language.split('-')[0].lower()}"
            try:
                payload = await _get(self._client, self.URL, params)
            except SearchError:
                if page == 0:
                    raise
                break
            items = payload.get("items") or []
            if not items:
                break
            for item in items:
                hit = _hit(item.get("link"), item.get("title"), item.get("snippet"), self.name) \
                    if isinstance(item, dict) else None
                if hit:
                    hits.append(hit)
            if len(hits) >= self.max_results:
                break
        return hits[: self.max_results]


class SerpApiClient:
    """SerpAPI (``organic_results``); paid, one request per page of ``num`` results."""

    name = "serpapi"
    URL = "https://serpapi.com/search.json"

    def __init__(self, api_key: str, *, engine: str = "google", max_results: int = 10, timeout: float = 20,
                 pages: int = 1, country: str = "es", client: httpx.AsyncClient | None = None) -> None:
        self._key, self.engine = api_key, engine
        self.max_results, self.pages, self.country = max_results, max(1, min(3, pages)), country
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout), trust_env=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, *, language: str | None = None, pages: int | None = None) -> list[SearchHit]:
        wanted = max(1, min(3, pages if pages is not None else self.pages))
        num = min(10, self.max_results)
        hits: list[SearchHit] = []
        for page in range(wanted):
            params: dict[str, Any] = {"engine": self.engine, "q": query, "api_key": self._key, "num": num,
                                      "start": page * num, "gl": self.country}
            if language:
                params["hl"] = language.split("-")[0].lower()
            try:
                payload = await _get(self._client, self.URL, params)
            except SearchError:
                if page == 0:
                    raise
                break
            items = payload.get("organic_results") or []
            if not items:
                break
            for item in items:
                hit = _hit(item.get("link"), item.get("title"), item.get("snippet"), self.name) \
                    if isinstance(item, dict) else None
                if hit:
                    hits.append(hit)
            if len(hits) >= self.max_results:
                break
        return hits[: self.max_results]


def _name(backend: Any) -> str:
    return getattr(backend, "name", None) or type(backend).__name__


class MergedSearcher:
    """Runs every backend concurrently and merges the hits round-robin, de-duplicated by ``url_key``.

    A backend that fails is logged (``web_search.backend_failed <name>``) and skipped for that query; if all
    fail, the first error is raised. ``daily_caps`` (backend name -> calls per UTC day) are counted in memory
    only, so they reset on restart; a DB-backed counter is a follow-up.
    """

    def __init__(self, backends: list[SearchBackend], *, max_results: int = 30,
                 daily_caps: dict[str, int] | None = None) -> None:
        self.backends = backends
        self.max_results = max_results
        self.daily_caps = daily_caps or {}
        self._day = ""
        self._calls: dict[str, int] = {}

    async def aclose(self) -> None:
        await asyncio.gather(*(b.aclose() for b in self.backends), return_exceptions=True)

    def _allow(self, name: str) -> bool:
        today = datetime.now(UTC).date().isoformat()
        if today != self._day:
            self._day, self._calls = today, {}
        cap = self.daily_caps.get(name)
        if cap is not None and self._calls.get(name, 0) >= cap:
            log.warning("web_search.backend_capped %s", name)
            return False
        self._calls[name] = self._calls.get(name, 0) + 1
        return True

    async def search(self, query: str, *, language: str | None = None, pages: int | None = None) -> list[SearchHit]:
        active = [b for b in self.backends if self._allow(_name(b))]
        results = await asyncio.gather(*(b.search(query, language=language, pages=pages) for b in active),
                                       return_exceptions=True)
        lists: list[list[SearchHit]] = []
        errors: list[BaseException] = []
        for backend, result in zip(active, results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                code = result.code if isinstance(result, SearchError) else type(result).__name__
                log.warning("web_search.backend_failed %s %s", _name(backend), code)
                errors.append(result)
            else:
                lists.append(result)
        if errors and not lists:
            first = errors[0]
            raise first if isinstance(first, SearchError) else SearchError(type(first).__name__)
        merged: list[SearchHit] = []
        seen: set[str] = set()
        for rank in range(max((len(x) for x in lists), default=0)):
            for hits in lists:
                if rank >= len(hits):
                    continue
                key = url_key(hits[rank].url)
                if key in seen:
                    continue
                seen.add(key)
                merged.append(hits[rank])
                if len(merged) >= self.max_results:
                    return merged
        return merged
