"""Which fetcher a URL gets, and when the browser is allowed to be missing.

Two rules: a domain on ``PARSER_BROWSER_DOMAINS`` skips the doomed HTTP
attempt entirely, and everything else escalates to the browser only when the
response looks like bot protection rather than a page that simply is not
there. A 404 must never cost a page render.
"""

from __future__ import annotations

from bot.config import ParserSettings
from bot.exceptions import ConfigurationError
from bot.models.result import PageContent
from bot.services.parser.browser import BrowserFetcher
from bot.services.parser.fetcher import PageFetcher
from bot.services.parser.routing import RoutingFetcher, build_fetcher

BLOCKED = "https://www.idealista.es/listing"
BROWSER_FIRST = "https://www.fotocasa.es/listing"
MISSING = "https://example.com/gone"
FINE = "https://example.com/ok"


class FakeFetcher:
    """Records which URLs it was asked for, and answers however the test says."""

    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.seen: list[str] = []

    async def fetch_many(self, urls: list[str]) -> dict[str, PageContent]:
        self.seen.extend(urls)
        return {url: self.behaviour(url) for url in urls}

    async def fetch(self, url: str) -> PageContent:
        return (await self.fetch_many([url]))[url]

    async def preflight(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _http(url: str) -> PageContent:
    if "idealista" in url:
        return PageContent(url=url, error="HTTP 403", status=403)
    if "gone" in url:
        return PageContent(url=url, error="HTTP 404", status=404)
    return PageContent(url=url, text="fine")


def _browser(url: str) -> PageContent:
    return PageContent(url=url, text="rendered")


def _routed() -> tuple[RoutingFetcher, FakeFetcher, FakeFetcher]:
    http, browser = FakeFetcher(_http), FakeFetcher(_browser)
    settings = ParserSettings(browser_enabled=True, browser_domains="fotocasa.es")
    return RoutingFetcher(settings, http=http, browser=browser), http, browser


async def test_listed_domain_skips_the_http_attempt() -> None:
    router, http, _browser_fetcher = _routed()
    pages = await router.fetch_many([BROWSER_FIRST])

    assert BROWSER_FIRST not in http.seen, "a known-blocked domain still tried httpx"
    assert pages[BROWSER_FIRST].text == "rendered"


async def test_bot_protection_escalates_to_the_browser() -> None:
    router, _http_fetcher, browser = _routed()
    pages = await router.fetch_many([BLOCKED])

    assert BLOCKED in browser.seen
    assert pages[BLOCKED].text == "rendered"


async def test_a_missing_page_does_not_escalate() -> None:
    """A 404 is an answer, not a block. Rendering it would be wasted work."""
    router, _http_fetcher, browser = _routed()
    pages = await router.fetch_many([MISSING])

    assert MISSING not in browser.seen
    assert pages[MISSING].status == 404


async def test_every_input_url_gets_exactly_one_entry() -> None:
    router, _http_fetcher, _browser_fetcher = _routed()
    urls = [BROWSER_FIRST, BLOCKED, MISSING, FINE]

    assert set(await router.fetch_many(urls)) == set(urls)


async def test_subdomains_of_a_listed_domain_are_covered() -> None:
    router, http, _browser_fetcher = _routed()
    await router.fetch_many(["https://m.fotocasa.es/x"])

    assert http.seen == [], "a subdomain of a listed domain went to httpx"


async def test_browser_off_gives_the_plain_http_fetcher() -> None:
    """The search-only deployment carries no browser machinery at all."""
    fetcher = build_fetcher(ParserSettings())
    assert isinstance(fetcher, PageFetcher)
    await fetcher.preflight()  # a no-op, but it must satisfy the same protocol
    await fetcher.aclose()


async def test_preflight_fails_at_startup_when_chromium_cannot_launch() -> None:
    """Playwright importing proves nothing: the browser binary is separate.

    Without this, the first person to search is the one who discovers that
    `playwright install chromium` was never run.
    """
    settings = ParserSettings(browser_enabled=True, browser_domains="a.com")
    router = RoutingFetcher(
        settings, http=FakeFetcher(_http), browser=BrowserFetcher(settings)
    )
    try:
        await router.preflight()
    except ConfigurationError as exc:
        assert "playwright install" in str(exc).lower()
    finally:
        await router.aclose()


async def test_a_dead_browser_degrades_instead_of_raising() -> None:
    """One failed page, never a failed request."""
    settings = ParserSettings(browser_enabled=True, browser_domains="a.com")
    router = RoutingFetcher(
        settings, http=FakeFetcher(_http), browser=BrowserFetcher(settings)
    )
    page = await router.fetch("https://a.com/z")

    assert not page.ok
    assert page.error
    await router.aclose()
