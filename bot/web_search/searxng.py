"""A small async client for the internal SearXNG service (``GET /search?format=json``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .urls import url_key

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
    """One GET per result page (``pages`` of them), no retries (a failed query is recorded and not searched again)."""

    def __init__(self, base_url: str, *, timeout_seconds: float = 20, max_results: int = 10, pages: int = 1,
                 client: httpx.AsyncClient | None = None) -> None:
        self.max_results = max_results
        self.pages = max(1, min(5, pages))
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=httpx.Timeout(timeout_seconds), trust_env=False,
            # SearXNG's bot detection wants a forwarded-for header even with the limiter off;
            # the only client is this worker on the private Docker network.
            headers={"Accept": "application/json", "X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search(self, query: str, *, language: str | None = None, pages: int | None = None) -> list[SearchHit]:
        """``pageno`` 1..``pages`` one after another; stops when a page brings nothing new.

        Hits are merged and de-duplicated by ``url_key`` (so tracking-parameter spellings of one page
        count once) and cut at ``max_results``. A failure on page 1 raises; on a later page it ends the walk.
        """
        wanted = max(1, min(5, pages if pages is not None else self.pages))
        hits: list[SearchHit] = []
        seen: set[str] = set()
        silent: set[str] = set()
        for pageno in range(1, wanted + 1):
            try:
                payload = await self._page(query, language, pageno)
            except SearchError as exc:
                if pageno == 1:
                    raise
                log.warning("web_search.page_failed %s page=%d", exc.code, pageno)
                break
            silent.update(str(e[0] if isinstance(e, list | tuple) and e else e)
                          for e in payload.get("unresponsive_engines") or [])
            fresh = 0
            for item in payload.get("results") or []:
                url = str(item.get("url") or "").strip() if isinstance(item, dict) else ""
                if not url.startswith(("http://", "https://")):
                    continue
                key = url_key(url)
                if key in seen:
                    continue
                seen.add(key)
                fresh += 1
                hits.append(SearchHit(url, str(item.get("title") or "")[:300], str(item.get("content") or "")[:500]))
                if len(hits) >= self.max_results:
                    break
            if not fresh or len(hits) >= self.max_results:
                break
        if silent:
            log.warning("web_search.engines_silent %s", ",".join(sorted(silent))[:300])
        return hits

    async def _page(self, query: str, language: str | None, pageno: int) -> dict[str, Any]:
        params: dict[str, Any] = {"q": query, "format": "json", "safesearch": 0, "pageno": pageno,
                                  "categories": "general"}
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
        return payload if isinstance(payload, dict) else {}
