"""Fetching pages with a browser's TLS fingerprint, but without a browser.

Most of what :class:`~bot.services.parser.fetcher.PageFetcher` gets a 403 for
is not rejected because of *what* it asked for, but because of how the
connection looked: httpx has a TLS and HTTP/2 fingerprint no real browser
produces, and the large listing portals key on exactly that. Scrapling (over
curl_cffi) replays Chrome's handshake and header order, which gets past a good
share of those pages at roughly the cost of a plain HTTP request.

That makes this the cheap middle rung between the HTTP fetcher and
:class:`~bot.services.parser.browser.BrowserFetcher`: still one request and no
page render, so it is tried first when a page comes back blocked, and the
browser is spent only on what survives it (see
:mod:`bot.services.parser.routing`).

What it deliberately does not do is run JavaScript. A page that 200s with an
empty shell and fills itself in from XHR is a browser's job, not this one's --
here it simply extracts to nothing and the ranker sees no text, which is the
same outcome as any other unreadable page.

Scrapling is an optional dependency: importing this module without it is fine,
and so is constructing the fetcher. :func:`~bot.services.parser.routing.build_fetcher`
is what refuses to start, at boot, with a :class:`ConfigurationError` naming
the fix.
"""

from __future__ import annotations

import asyncio
from typing import Any

from bot.config import ParserSettings
from bot.exceptions import ConfigurationError
from bot.logging_conf import get_logger
from bot.models.result import PageContent
from bot.services.parser.extractor import extract_text

log = get_logger(__name__)

_HTML_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")


class StealthFetcher:
    """Fetches URLs through Scrapling's curl_cffi client, impersonating a browser."""

    def __init__(self, settings: ParserSettings) -> None:
        self.settings = settings
        self._semaphore = asyncio.Semaphore(settings.stealth_concurrency)
        self._get: Any = None

    # -- lifecycle ---------------------------------------------------------

    def _ensure_client(self) -> Any:
        """Return Scrapling's async GET, importing it on first use.

        The import is deferred rather than done at module scope so that a
        deployment with the stealth fetcher off carries no Scrapling import at
        all, exactly as the browser fetcher treats Playwright.
        """
        if self._get is not None:
            return self._get

        try:
            from scrapling.fetchers import AsyncFetcher
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ConfigurationError(
                "PARSER_STEALTH_ENABLED is on but Scrapling is not installed; "
                'pip install "scrapling[fetchers]"'
            ) from exc

        self._get = AsyncFetcher.get
        return self._get

    async def preflight(self) -> None:
        """Prove at start-up that Scrapling imports, rather than on first search.

        Only the import is checked: unlike the browser there is no binary to
        download and no runtime to launch, so an import that succeeds is the
        whole of the install being present.
        """
        self._ensure_client()
        log.info("parser.stealth.preflight_ok", impersonate=self.settings.stealth_impersonate)

    async def aclose(self) -> None:
        """Nothing to tear down: Scrapling closes its one-off session per request."""
        return None

    # -- fetching ----------------------------------------------------------

    async def fetch_many(self, urls: list[str]) -> dict[str, PageContent]:
        """Fetch *urls* concurrently, keyed by the URL as given. One entry per input."""
        if not urls:
            return {}

        async def one(url: str) -> PageContent:
            async with self._semaphore:
                return await self.fetch(url)

        pages = await asyncio.gather(*(one(url) for url in urls))
        ok = sum(1 for page in pages if page.ok)
        log.info("parser.stealth.batch.done", requested=len(urls), extracted=ok)
        return {page.url: page for page in pages}

    async def fetch(self, url: str) -> PageContent:
        """Fetch a single URL. Never raises."""
        try:
            get = self._ensure_client()
        except ConfigurationError as exc:
            log.warning("parser.stealth.unavailable", url=url, error=str(exc))
            return PageContent(url=url, error="stealth fetcher unavailable")

        request: dict[str, Any] = {
            "impersonate": self.settings.stealth_impersonate,
            "stealthy_headers": True,
            "timeout": self.settings.stealth_timeout_seconds,
            # Scrapling retries CurlErrors three times a second apart by
            # default. A page we are about to escalate to a browser anyway is
            # not worth three seconds of waiting, so the budget is spent there
            # instead.
            "retries": 1,
        }
        if self.settings.stealth_proxy_url:
            request["proxy"] = self.settings.stealth_proxy_url

        try:
            response = await get(url, **request)
        except Exception as exc:  # noqa: BLE001 - documented never to raise: one failed page
            log.debug("parser.stealth.fetch.failed", url=url, error=str(exc))
            return PageContent(url=url, error=f"stealth fetch failed: {type(exc).__name__}")

        status = getattr(response, "status", None)
        if status is not None and status >= 400:
            # Carried through as `status`, not just an error string, so a page
            # this rung could not get either is still recognisably `blocked`
            # and reaches the browser.
            return PageContent(url=url, error=f"HTTP {status}", status=status)

        content_type = _content_type(response)
        if content_type and not content_type.startswith(_HTML_CONTENT_TYPES):
            return PageContent(
                url=url, error=f"unsupported content type {content_type!r}", status=status
            )

        html = _html_of(response)
        if len(html) > self.settings.max_bytes:
            html = html[: self.settings.max_bytes]

        page = extract_text(
            html,
            url=url,
            final_url=_final_url(response, url),
            max_chars=self.settings.max_chars,
        )
        page.status = status
        return page


def _content_type(response: Any) -> str:
    """The response's content type, lowercased and without parameters."""
    headers = getattr(response, "headers", None) or {}
    try:
        items = headers.items()
    except AttributeError:  # pragma: no cover - not a mapping, nothing to read
        return ""
    for key, value in items:
        if str(key).lower() == "content-type":
            return str(value).split(";")[0].strip().lower()
    return ""


def _html_of(response: Any) -> str:
    """The response body as text.

    ``body`` is the bytes as they arrived, which is what we want -- but a
    Scrapling ``Response`` is also a parsed selector, so ``html_content`` is
    there as a fallback for the case where the raw body did not survive.
    """
    body = getattr(response, "body", None)
    if isinstance(body, bytes):
        encoding = getattr(response, "encoding", None) or "utf-8"
        return body.decode(encoding, errors="replace")
    if isinstance(body, str) and body:
        return body

    try:
        return str(response.html_content)
    except Exception:  # noqa: BLE001 - a body we cannot read is an empty page, not a crash
        log.debug("parser.stealth.body_unreadable", url=getattr(response, "url", None))
        return ""


def _final_url(response: Any, url: str) -> str | None:
    """Where the response actually came from, when it differs from *url*."""
    final = getattr(response, "url", None)
    return str(final) if final and str(final) != url else None
