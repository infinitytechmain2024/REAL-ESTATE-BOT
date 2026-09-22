"""Fetching pages through a real browser.

Some of the sites worth reading -- Idealista and most large listing portals --
sit behind bot protection that answers :class:`~bot.services.parser.fetcher.
PageFetcher` with a 403 no matter how polite the headers are. A real browser
gets through where an HTTP client does not, at maybe twenty times the cost per
page, so this fetcher is deliberately the exception and not the default: see
:class:`~bot.services.parser.routing.RoutingFetcher` for when it is used.

Playwright ships in ``requirements.txt`` for the Facebook module, but a
browser binary is a separate matter: either ``PARSER_BROWSER_BINARY`` names an
installed one (the Docker image points it at the same Brave the Facebook
session uses, which is why that image needs no ``playwright install``), or
Playwright's own Chromium has been downloaded. Neither is assumed here.
Importing this module without either is fine, and so is constructing the
fetcher;
:func:`~bot.services.parser.routing.build_fetcher` is what refuses to start,
at boot, with a :class:`ConfigurationError` naming the fix. Once running, a
fetch never raises -- a dead browser is one failed page, not a failed request.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from bot.config import ParserSettings
from bot.exceptions import ConfigurationError
from bot.logging_conf import get_logger
from bot.models.result import PageContent
from bot.services.parser.extractor import extract_text

if TYPE_CHECKING:  # pragma: no cover - typing only, playwright may be absent
    from playwright.async_api import Browser, BrowserContext, Playwright

log = get_logger(__name__)


class BrowserFetcher:
    """Loads pages in headless Chromium, one shared browser for the process.

    The browser is started lazily on the first fetch rather than at boot: most
    runs never need it, and paying a second of start-up for a fetcher that may
    go unused is not worth it.
    """

    def __init__(self, settings: ParserSettings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        # Serialises start-up: several pages hitting a cold fetcher at once
        # must not each launch their own Chromium.
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(settings.browser_concurrency)

    # -- lifecycle ---------------------------------------------------------

    async def _ensure_context(self) -> BrowserContext:
        """Start Playwright and the browser once, and return its context."""
        if self._context is not None:
            return self._context

        async with self._lock:
            if self._context is not None:  # another task won the race
                return self._context

            try:
                from playwright.async_api import async_playwright
            except ImportError as exc:  # pragma: no cover - depends on install
                raise ConfigurationError(
                    "PARSER_BROWSER_ENABLED is on but Playwright is not installed; "
                    "pip install -r requirements.txt"
                ) from exc

            self._playwright = await async_playwright().start()

            launch: dict[str, Any] = {"headless": self.settings.browser_headless}
            if self.settings.browser_binary:
                # An installed browser (Brave in the image) instead of
                # Playwright's bundled Chromium, which is never downloaded
                # there. Nothing here depends on it being Brave.
                launch["executable_path"] = self.settings.browser_binary
            if self.settings.browser_proxy_url:
                # Residential/mobile egress. Datacenter IPs are the single
                # biggest tell for the protections this fetcher exists to get
                # past, so in practice this is set whenever the fetcher is.
                launch["proxy"] = {"server": self.settings.browser_proxy_url}

            self._browser = await self._playwright.chromium.launch(**launch)
            self._context = await self._browser.new_context(
                user_agent=self.settings.browser_user_agent,
                locale=self.settings.browser_locale,
                viewport={"width": 1440, "height": 900},
            )
            self._context.set_default_navigation_timeout(
                self.settings.browser_timeout_seconds * 1000
            )
            log.info(
                "browser.started",
                headless=self.settings.browser_headless,
                proxied=bool(self.settings.browser_proxy_url),
                binary=self.settings.browser_binary or "playwright-chromium",
            )
            return self._context

    async def preflight(self) -> None:
        """Launch the browser once so a broken install fails at start-up.

        Raises :class:`ConfigurationError` when Playwright is absent, or when
        the browser it was told to launch is not there -- a bad
        ``PARSER_BROWSER_BINARY`` path, or no bundled Chromium when that
        setting is unset. The context is kept warm rather than closed, so this
        costs start-up time, not an extra launch.
        """
        try:
            await self._ensure_context()
        except ConfigurationError:
            raise
        except Exception as exc:
            fix = (
                f"PARSER_BROWSER_BINARY points at {self.settings.browser_binary!r}; "
                f"check that path exists in this container"
                if self.settings.browser_binary
                else "run `playwright install chromium`, or set PARSER_BROWSER_BINARY "
                "to an installed browser"
            )
            raise ConfigurationError(
                f"PARSER_BROWSER_ENABLED is on but the browser will not launch "
                f"({type(exc).__name__}: {exc}). {fix}, "
                f"or set PARSER_BROWSER_ENABLED=false."
            ) from exc

    async def aclose(self) -> None:
        """Tear the browser down. Safe to call when it never started."""
        for name, closer in (
            ("context", self._context),
            ("browser", self._browser),
            ("playwright", self._playwright),
        ):
            if closer is None:
                continue
            try:
                # Playwright's own handle stops, the others close.
                await (closer.stop() if name == "playwright" else closer.close())
            except Exception:  # noqa: BLE001 - one bad closer must not strand the rest
                log.warning("browser.close_failed", component=name, exc_info=True)
        self._context = self._browser = self._playwright = None

    # -- fetching ----------------------------------------------------------

    async def fetch_many(self, urls: list[str]) -> dict[str, PageContent]:
        """Fetch *urls*, keyed by the URL as given. One entry per input."""
        if not urls:
            return {}

        async def one(url: str) -> PageContent:
            async with self._semaphore:
                return await self.fetch(url)

        pages = await asyncio.gather(*(one(url) for url in urls))
        ok = sum(1 for page in pages if page.ok)
        log.info("browser.batch.done", requested=len(urls), extracted=ok)
        return {page.url: page for page in pages}

    async def fetch(self, url: str) -> PageContent:
        """Load one URL in a fresh tab. Never raises."""
        try:
            context = await self._ensure_context()
        except Exception as exc:  # noqa: BLE001 - documented never to raise: one failed page
            log.warning("browser.start_failed", url=url, error=str(exc), exc_info=True)
            return PageContent(url=url, error=f"browser unavailable: {type(exc).__name__}")

        page = await context.new_page()
        try:
            response = await page.goto(url, wait_until=self.settings.browser_wait_until)
            status = response.status if response is not None else None
            if status is not None and status >= 400:
                return PageContent(url=url, error=f"HTTP {status}", status=status)

            html = await page.content()
            final_url = page.url
        except Exception as exc:  # noqa: BLE001 - navigation failures are data
            log.debug("browser.fetch.failed", url=url, error=str(exc))
            return PageContent(url=url, error=f"browser fetch failed: {type(exc).__name__}")
        finally:
            try:
                await page.close()
            except Exception:  # noqa: BLE001 - the tab may already be gone
                log.debug("browser.page_close_failed", url=url)

        if len(html) > self.settings.max_bytes:
            html = html[: self.settings.max_bytes]

        return extract_text(html, url=url, final_url=final_url, max_chars=self.settings.max_chars)
