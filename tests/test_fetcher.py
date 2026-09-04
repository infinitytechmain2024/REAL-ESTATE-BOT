"""Redirect handling in the page fetcher.

The point of these is the second hop. Validating only the URL a search engine
gave us is exactly the hole that lets a public page redirect the fetcher into
the private network, so the test that matters is the one where the first URL is
fine and the target is not.
"""

from __future__ import annotations

import httpx
import pytest

from bot.config import ParserSettings
from bot.services.parser.fetcher import PageFetcher

pytestmark = pytest.mark.asyncio

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every hostname to one public address.

    So the tests exercise the redirect logic rather than the resolver, which
    ``tests/test_net.py`` covers on its own. Literal IPs in a URL bypass this
    and are checked for real.
    """
    from bot.utils import net

    async def fake_getaddrinfo(host, port, **kwargs):  # type: ignore[no-untyped-def]
        return [(None, None, None, "", (PUBLIC_IP, port))]

    class _Loop:
        getaddrinfo = staticmethod(fake_getaddrinfo)

    monkeypatch.setattr(net.asyncio, "get_running_loop", lambda: _Loop())


def _fetcher(handler, **overrides) -> PageFetcher:  # type: ignore[no-untyped-def]
    """A PageFetcher whose HTTP client is backed by *handler*."""
    settings = ParserSettings(**overrides)
    fetcher = PageFetcher(settings)
    fetcher._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
    )
    return fetcher


async def test_a_plain_page_is_fetched(public_dns: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html="<html><body>hello</body></html>")

    fetcher = _fetcher(handler)
    page = await fetcher.fetch("https://example.com/listing")
    assert page.ok
    await fetcher.aclose()


async def test_a_redirect_to_a_public_page_is_followed(public_dns: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://example.com/final"})
        return httpx.Response(200, html="<html><body>arrived</body></html>")

    fetcher = _fetcher(handler)
    page = await fetcher.fetch("https://example.com/start")
    assert page.ok
    await fetcher.aclose()


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1:8888/search",              # our own SearXNG
        "http://10.0.0.5/internal",                  # RFC1918
        "file:///etc/passwd",                        # scheme change
    ],
)
async def test_a_redirect_into_the_private_network_is_blocked(
    public_dns: None, target: str
) -> None:
    """The first URL is public and only the redirect target is not."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, html="<html>secret</html>")

    fetcher = _fetcher(handler)
    page = await fetcher.fetch("https://example.com/start")

    assert not page.ok
    assert "blocked" in (page.error or "")
    # The dangerous URL must never have been requested at all.
    assert all(target not in url for url in seen)
    await fetcher.aclose()


async def test_a_redirect_loop_gives_up(public_dns: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/loop"})

    fetcher = _fetcher(handler, max_redirects=3)
    page = await fetcher.fetch("https://example.com/loop")
    assert not page.ok
    assert "redirect" in (page.error or "")
    await fetcher.aclose()


async def test_a_relative_redirect_is_resolved_and_followed(public_dns: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(301, headers={"location": "/b"})
        return httpx.Response(200, html="<html><body>b</body></html>")

    fetcher = _fetcher(handler)
    page = await fetcher.fetch("https://example.com/a")
    assert page.ok
    await fetcher.aclose()


async def test_a_non_html_response_is_refused(public_dns: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})

    fetcher = _fetcher(handler)
    page = await fetcher.fetch("https://example.com/doc.pdf")
    assert not page.ok
    assert "content type" in (page.error or "")
    await fetcher.aclose()


async def test_an_oversized_page_is_abandoned(public_dns: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000, headers={"content-type": "text/html"})

    fetcher = _fetcher(handler, max_bytes=1000)
    page = await fetcher.fetch("https://example.com/huge")
    assert not page.ok
    assert "limit" in (page.error or "")
    await fetcher.aclose()


async def test_the_first_url_is_screened_too(public_dns: None) -> None:
    """A literal private address bypasses the stubbed resolver, as it should."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, html="<html>secret</html>")

    fetcher = _fetcher(handler)
    page = await fetcher.fetch("http://169.254.169.254/latest/meta-data/")
    assert not page.ok
    assert requested == []
    await fetcher.aclose()


async def test_one_bad_page_does_not_break_the_batch(public_dns: None) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "bad" in request.url.path:
            return httpx.Response(500)
        return httpx.Response(200, html="<html><body>fine</body></html>")

    fetcher = _fetcher(handler)
    pages = await fetcher.fetch_many(
        ["https://example.com/good", "https://example.com/bad", "http://127.0.0.1/x"]
    )
    assert len(pages) == 3
    assert pages["https://example.com/good"].ok
    assert not pages["https://example.com/bad"].ok
    assert not pages["http://127.0.0.1/x"].ok
    await fetcher.aclose()
