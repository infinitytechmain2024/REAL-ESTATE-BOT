"""Optional last fetch layer: a scrape/unlocker API (Zyte, ScraperAPI, Bright Data Web Unlocker ...).

Used only for a page that both plain HTTP and the browser were refused (``worker._read``), and only when
``WEB_SEARCH_SCRAPE_API_URL`` is set. Generic shape: ``GET {api_url}?url=<encoded page url>`` with
``Authorization: Bearer <key>``; the response body is the page's HTML. The same safety rules as the
fetcher apply to what comes back: HTML only, a byte cap, a timeout, no redirects followed. The key is
never logged and never part of an error code.
"""

from __future__ import annotations

from typing import Protocol
from urllib.parse import quote

import httpx

from .fetcher import FetchedPage, FetchError, _decode

_HTML_TYPES = ("text/html", "application/xhtml+xml")


class Scraper(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...


class ScrapeApiClient:
    def __init__(self, api_url: str, api_key: str = "", *, timeout_seconds: float = 60, max_bytes: int = 2_000_000,
                 client: httpx.AsyncClient | None = None) -> None:
        if not api_url.startswith(("https://", "http://")):
            raise ValueError("scrape api url must be http(s)")
        self._url, self._key, self.max_bytes = api_url, api_key, max_bytes
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)

    def __repr__(self) -> str:  # the key must not leak through a log line
        return "ScrapeApiClient(...)"

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, url: str) -> FetchedPage:
        separator = "&" if "?" in self._url else "?"
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        try:
            async with self._client.stream("GET", f"{self._url}{separator}url={quote(url, safe='')}",
                                           headers=headers) as response:
                if response.status_code >= 300:
                    raise FetchError(f"scrape_http_{response.status_code}")
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type and not content_type.startswith(_HTML_TYPES):
                    raise FetchError("not_html")
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > self.max_bytes:
                        raise FetchError("too_large")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise FetchError(f"scrape_error:{type(exc).__name__}") from exc
        return FetchedPage(url, _decode(b"".join(chunks)))
