"""Choosing between the fetchers, per URL.

Three rungs, cheapest first:

1. :class:`~bot.services.parser.fetcher.PageFetcher` -- plain httpx.
2. :class:`~bot.services.parser.stealth.StealthFetcher` -- one request, but
   with a real browser's TLS and header fingerprint.
3. :class:`~bot.services.parser.browser.BrowserFetcher` -- an actual page
   render, perhaps twenty times the cost of rung 1.

Two rules place a URL on them, in order:

1. A domain on ``PARSER_BROWSER_DOMAINS`` skips the plain HTTP attempt and
   starts at the first rung above it that is enabled. This is for sites known
   to block plain HTTP clients outright -- trying httpx first only wastes a
   round trip.
2. Everything else is fetched over HTTP, and *escalated* one rung at a time,
   but only while the answer looks like bot protection rather than a missing
   page (see :attr:`~bot.models.result.PageContent.blocked`). A page that 404s
   or times out is not retried anywhere: it would cost more and fail again.

Each rung is optional. With both off this collapses to the plain HTTP fetcher,
which is what the search-only deployment runs.
"""

from __future__ import annotations

from typing import Protocol

from bot.config import ParserSettings
from bot.logging_conf import get_logger
from bot.models.result import PageContent
from bot.utils.urls import domain_of

log = get_logger(__name__)


class Fetcher(Protocol):
    """What the pipeline needs from anything that turns URLs into text."""

    async def fetch_many(self, urls: list[str]) -> dict[str, PageContent]: ...

    async def fetch(self, url: str) -> PageContent: ...

    async def preflight(self) -> None: ...

    async def aclose(self) -> None: ...


class RoutingFetcher:
    """Fronts the HTTP fetcher with progressively more expensive fallbacks.

    *escalations* is ordered cheapest first; an empty list makes this a
    pass-through to *http*.
    """

    def __init__(
        self,
        settings: ParserSettings,
        *,
        http: Fetcher,
        escalations: list[tuple[str, Fetcher]] | None = None,
    ) -> None:
        self.settings = settings
        self._http = http
        self._escalations = escalations or []
        self._direct_domains = {d.lower().lstrip(".") for d in settings.browser_domains}

    async def preflight(self) -> None:
        """Prove at start-up that every enabled rung can actually run.

        Importing Playwright is no longer evidence of anything -- it ships in
        requirements.txt for the Facebook module, so it is always present even
        when ``playwright install chromium`` was never run. The browser binary
        is what is actually missing in that case, and without this the first
        person to search would be the one to find out. The same reasoning
        applies to Scrapling, which is not in requirements.txt at all.
        """
        for name, fetcher in self._escalations:
            await fetcher.preflight()
            log.info("parser.escalation.preflight_ok", rung=name)

    async def aclose(self) -> None:
        await self._http.aclose()
        for _, fetcher in self._escalations:
            await fetcher.aclose()

    def _skips_http(self, url: str) -> bool:
        """Whether *url*'s domain is one we never bother trying over HTTP."""
        if not self._escalations:
            # Nowhere to send it instead, and `fetch_many` owes one entry per
            # input URL -- so the doomed HTTP attempt is still better than none.
            return False
        domain = (domain_of(url) or "").lower()
        if not domain:
            return False
        # A rule for "idealista.es" should also cover "www.idealista.es".
        return any(domain == rule or domain.endswith(f".{rule}") for rule in self._direct_domains)

    async def fetch_many(self, urls: list[str]) -> dict[str, PageContent]:
        """Fetch *urls*, routing and escalating as described in the module docstring."""
        if not urls:
            return {}

        direct = sorted({url for url in urls if self._skips_http(url)})
        over_http = [url for url in urls if url not in set(direct)]

        pages: dict[str, PageContent] = {}
        if over_http:
            pages.update(await self._http.fetch_many(over_http))

        # Carried into the first rung on top of whatever HTTP got blocked on,
        # since these were never attempted at all.
        pending = direct
        if pending:
            log.info("parser.route.http_skipped", count=len(pending))

        for name, fetcher in self._escalations:
            targets = pending + [url for url, page in pages.items() if page.blocked]
            pending = []
            if not targets:
                break
            log.info("parser.route.escalating", rung=name, count=len(targets))
            pages.update(await fetcher.fetch_many(targets))

        still_blocked = [url for url, page in pages.items() if page.blocked]
        for url in still_blocked:
            page = pages[url]
            if not page.text:
                continue
            # A challenge page that survived every rung still carries its
            # "enable JavaScript" text, and `ok` would call that a readable
            # page. Handing it to the ranker is worse than handing it nothing:
            # the LLM would score the interstitial as if it were the listing.
            pages[url] = page.model_copy(
                update={"text": "", "error": page.error or "bot protection challenge"}
            )

        if still_blocked:
            log.info(
                "parser.route.blocked",
                count=len(still_blocked),
                rungs_tried=[name for name, _ in self._escalations] or None,
                detail=(
                    "set PARSER_STEALTH_ENABLED=true and/or PARSER_BROWSER_ENABLED=true "
                    "to retry these"
                    if not self._escalations
                    else "every enabled fetcher was refused by these pages"
                ),
            )

        return pages

    async def fetch(self, url: str) -> PageContent:
        """Single-URL form of :meth:`fetch_many`."""
        pages = await self.fetch_many([url])
        return pages.get(url) or PageContent(url=url, error="fetcher returned nothing")


def build_fetcher(settings: ParserSettings) -> Fetcher:
    """The fetcher the pipeline should use for *settings*.

    Plain :class:`~bot.services.parser.fetcher.PageFetcher` when every fallback
    is off, so the search-only deployment carries neither Scrapling nor
    Playwright machinery.
    """
    from bot.services.parser.fetcher import PageFetcher

    http = PageFetcher(settings)

    escalations: list[tuple[str, Fetcher]] = []

    if settings.stealth_enabled:
        from bot.services.parser.stealth import StealthFetcher

        log.info(
            "parser.stealth.enabled",
            impersonate=settings.stealth_impersonate,
            proxied=bool(settings.stealth_proxy_url),
        )
        escalations.append(("stealth", StealthFetcher(settings)))

    if settings.browser_enabled:
        from bot.services.parser.browser import BrowserFetcher

        log.info(
            "parser.browser.enabled",
            domains=sorted(settings.browser_domains),
            proxied=bool(settings.browser_proxy_url),
        )
        escalations.append(("browser", BrowserFetcher(settings)))

    if not escalations:
        return http

    return RoutingFetcher(settings, http=http, escalations=escalations)
