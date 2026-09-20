"""Choosing between the HTTP fetcher and the browser, per URL.

Two rules, in order:

1. A domain on ``PARSER_BROWSER_DOMAINS`` goes straight to the browser. This
   is for sites known to block plain HTTP clients outright -- trying httpx
   first only wastes a round trip.
2. Everything else is fetched over HTTP, and *escalated* to the browser only
   when the answer looks like bot protection rather than a missing page (see
   :attr:`~bot.models.result.PageContent.blocked`).

With the browser disabled this collapses to the plain HTTP fetcher, which is
what the search-only deployment runs.
"""

from __future__ import annotations

import importlib.util
from typing import Protocol

from bot.config import ParserSettings
from bot.exceptions import ConfigurationError
from bot.logging_conf import get_logger
from bot.models.result import PageContent
from bot.utils.urls import domain_of

log = get_logger(__name__)


class Fetcher(Protocol):
    """What the pipeline needs from anything that turns URLs into text."""

    async def fetch_many(self, urls: list[str]) -> dict[str, PageContent]: ...

    async def fetch(self, url: str) -> PageContent: ...

    async def aclose(self) -> None: ...


class RoutingFetcher:
    """Fronts the HTTP fetcher with an optional browser for blocked sites."""

    def __init__(
        self,
        settings: ParserSettings,
        *,
        http: Fetcher,
        browser: Fetcher | None = None,
    ) -> None:
        self.settings = settings
        self._http = http
        self._browser = browser
        self._browser_domains = {d.lower().lstrip(".") for d in settings.browser_domains}

    async def aclose(self) -> None:
        await self._http.aclose()
        if self._browser is not None:
            await self._browser.aclose()

    def _needs_browser(self, url: str) -> bool:
        """Whether *url*'s domain is one we never bother trying over HTTP."""
        if self._browser is None:
            return False
        domain = (domain_of(url) or "").lower()
        if not domain:
            return False
        # A rule for "idealista.es" should also cover "www.idealista.es".
        return any(domain == rule or domain.endswith(f".{rule}") for rule in self._browser_domains)

    async def fetch_many(self, urls: list[str]) -> dict[str, PageContent]:
        """Fetch *urls*, routing and escalating as described in the module docstring."""
        if not urls:
            return {}

        direct = {url for url in urls if self._needs_browser(url)}
        over_http = [url for url in urls if url not in direct]

        pages: dict[str, PageContent] = {}
        if over_http:
            pages.update(await self._http.fetch_many(over_http))
        if direct and self._browser is not None:
            log.info("parser.route.browser_first", count=len(direct))
            pages.update(await self._browser.fetch_many(sorted(direct)))

        retry = [url for url, page in pages.items() if page.blocked]
        if retry and self._browser is not None:
            log.info("parser.route.escalating", count=len(retry))
            pages.update(await self._browser.fetch_many(retry))
        elif retry:
            log.info(
                "parser.route.blocked",
                count=len(retry),
                detail="set PARSER_BROWSER_ENABLED=true to retry these in a browser",
            )

        return pages

    async def fetch(self, url: str) -> PageContent:
        """Single-URL form of :meth:`fetch_many`."""
        pages = await self.fetch_many([url])
        return pages.get(url) or PageContent(url=url, error="fetcher returned nothing")


def build_fetcher(settings: ParserSettings) -> Fetcher:
    """The fetcher the pipeline should use for *settings*.

    Plain :class:`~bot.services.parser.fetcher.PageFetcher` when the browser is
    off, so the search-only deployment carries no Playwright machinery at all.
    """
    from bot.services.parser.fetcher import PageFetcher

    http = PageFetcher(settings)
    if not settings.browser_enabled:
        return http

    # Checked here so a missing optional dependency is a readable start-up
    # failure, not a surprise in the middle of somebody's search.
    if importlib.util.find_spec("playwright") is None:
        raise ConfigurationError(
            "PARSER_BROWSER_ENABLED=true but Playwright is not installed; "
            "pip install -r requirements-browser.txt "
            "&& playwright install --with-deps chromium "
            "(or set PARSER_BROWSER_ENABLED=false)"
        )

    from bot.services.parser.browser import BrowserFetcher

    log.info(
        "parser.browser.enabled",
        domains=sorted(settings.browser_domains),
        proxied=bool(settings.browser_proxy_url),
    )
    return RoutingFetcher(settings, http=http, browser=BrowserFetcher(settings))
