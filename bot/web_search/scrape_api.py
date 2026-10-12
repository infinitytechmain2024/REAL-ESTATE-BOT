"""Optional last fetch layer: a scrape/unlocker API (Zyte, ScraperAPI, Bright Data Web Unlocker ...).

Used only for a page that both plain HTTP and the browser were refused (``worker._read``), and only when
``WEB_SEARCH_SCRAPE_API_URL`` is set. Generic shape: ``GET {api_url}?url=<encoded page url>`` with
``Authorization: Bearer <key>``; the response body is the page's HTML. The same safety rules as the
fetcher apply to what comes back: HTML only, a byte cap, a timeout, no redirects followed. The key is
never logged and never part of an error code.
"""

from __future__ import annotations

import logging
from typing import Protocol
from urllib.parse import quote, urlsplit

import httpx

from .fetcher import FetchedPage, FetchError, _decode

_HTML_TYPES = ("text/html", "application/xhtml+xml")


class _RedactToken(logging.Filter):
    def __init__(self, token: str) -> None:
        super().__init__()
        self._values = (token, quote(token, safe=""))

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = message
        for value in self._values:
            redacted = redacted.replace(value, "[redacted]")
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


class Scraper(Protocol):
    async def fetch(self, url: str) -> FetchedPage: ...


class ScrapeBillingError(FetchError):
    def __init__(self, code: str, credits: int | None) -> None:
        super().__init__(code)
        self.credits = credits


def _credits(headers: httpx.Headers) -> int | None:
    raw = headers.get("Scrape.do-Request-Cost", "")
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 0 <= value <= 1_000_000 else None


class ScrapeApiClient:
    def __init__(self, api_url: str, api_key: str = "", *, timeout_seconds: float = 60, max_bytes: int = 2_000_000,
                 client: httpx.AsyncClient | None = None, auth_mode: str = "bearer", geo_code: str = "es",
                 render: bool = False, super_proxy: bool = False, credit_usd: float = 0.000116) -> None:
        if not api_url.startswith(("https://", "http://")):
            raise ValueError("scrape api url must be http(s)")
        if auth_mode not in ("bearer", "query_token"):
            raise ValueError("unknown scrape api auth mode")
        if auth_mode == "query_token" and (not api_key or urlsplit(api_url).hostname != "api.scrape.do"
                                            or urlsplit(api_url).scheme != "https"):
            raise ValueError("Scrape.do requires a key and HTTPS api.scrape.do endpoint")
        self._log_filter = _RedactToken(api_key) if auth_mode == "query_token" else None
        if self._log_filter:
            logging.getLogger("httpx").addFilter(self._log_filter)
        self._url, self._key, self.max_bytes = api_url, api_key, max_bytes
        self.auth_mode = auth_mode
        self.geo_code, self.render, self.super_proxy = geo_code, render, super_proxy
        self.credit_usd = credit_usd
        self.estimated_credits = 25 if render and super_proxy else 10 if super_proxy else 5 if render else 1
        # Idealista uses Super Proxy regardless of the explicit flag.
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False)

    def __repr__(self) -> str:  # the key must not leak through a log line
        return "ScrapeApiClient(...)"

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        finally:
            if self._log_filter:
                logging.getLogger("httpx").removeFilter(self._log_filter)

    async def fetch(self, url: str) -> FetchedPage:
        separator = "&" if "?" in self._url else "?"
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        params = f"url={quote(url, safe='')}"
        if self.auth_mode == "query_token":
            headers = {}
            params += (f"&token={quote(self._key, safe='')}&geoCode={quote(self.geo_code, safe='')}"
                       f"&render={'true' if self.render else 'false'}&super={'true' if self.super_proxy else 'false'}")
        try:
            async with self._client.stream("GET", f"{self._url}{separator}{params}",
                                           headers=headers) as response:
                credits = _credits(response.headers) if self.auth_mode == "query_token" else None
                if response.status_code >= 300:
                    raise ScrapeBillingError(f"scrape_http_{response.status_code}", credits)
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type and not content_type.startswith(_HTML_TYPES):
                    raise ScrapeBillingError("not_html", credits)
                chunks: list[bytes] = []
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > self.max_bytes:
                        raise ScrapeBillingError("too_large", credits)
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise FetchError(f"scrape_error:{type(exc).__name__}") from None
        return FetchedPage(url, _decode(b"".join(chunks)), credits)
