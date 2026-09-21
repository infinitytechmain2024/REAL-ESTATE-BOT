"""Offline coverage for the optional Scrapling parser fallback."""

from __future__ import annotations

import asyncio
import builtins
from typing import ClassVar

from bot.config import ParserSettings
from bot.models.result import PageContent
from bot.services.parser.fetcher import PageFetcher
from bot.services.parser.routing import RoutingFetcher, build_fetcher
from bot.services.parser.scrapling import ScraplingFetcher

URL = "https://listing.example/plot"
HTML = """<html><head><title>Plot</title></head>
<body><h1>Urban plot</h1><p>Madrid 2,000 m2 EUR 250000</p></body></html>"""


class _Response:
    def __init__(self, body: str | bytes, *, status: int = 200, url: str = URL) -> None:
        self.body = body
        self.status = status
        self.url = url
        self.encoding = "utf-8"


class _FakeAsyncFetcher:
    response: _Response | None = _Response(HTML)
    error: BaseException | None = None
    calls: ClassVar[list[tuple[str, dict]]] = []

    @classmethod
    def get(cls, url: str, **kwargs):
        cls.calls.append((url, kwargs))

        async def result() -> _Response:
            if cls.error is not None:
                raise cls.error
            assert cls.response is not None
            return cls.response

        return result()


def _settings(**overrides) -> ParserSettings:
    return ParserSettings(scrapling_enabled=True, **overrides)


async def test_scrapling_fetch_extracts_text_and_forwards_limits() -> None:
    fetcher = ScraplingFetcher(_settings(timeout_seconds=0.25))
    _FakeAsyncFetcher.calls = []
    _FakeAsyncFetcher.response = _Response(HTML)
    _FakeAsyncFetcher.error = None
    fetcher._fetcher = _FakeAsyncFetcher

    page = await fetcher.fetch(URL)

    assert page.ok
    assert "Urban plot" in page.text
    assert page.status == 200
    assert _FakeAsyncFetcher.calls == [
        (
            URL,
            {"headers": {"User-Agent": _settings().user_agent}, "timeout": 0.25},
        )
    ]


async def test_scrapling_rejects_oversized_body_before_extraction() -> None:
    fetcher = ScraplingFetcher(_settings(max_bytes=8))
    _FakeAsyncFetcher.response = _Response(HTML)
    _FakeAsyncFetcher.error = None
    fetcher._fetcher = _FakeAsyncFetcher

    page = await fetcher.fetch(URL)

    assert not page.ok
    assert page.status == 200
    assert page.error == "page exceeded the size limit"


async def test_scrapling_timeout_becomes_page_error() -> None:
    fetcher = ScraplingFetcher(_settings(timeout_seconds=0.01))

    async def slow_get(*args, **kwargs):
        await asyncio.sleep(0.1)

    class SlowFetcher:
        get = slow_get

    fetcher._fetcher = SlowFetcher

    page = await fetcher.fetch(URL)

    assert not page.ok
    assert page.error == "scrapling timed out after 0.01s"


class _RecordingFetcher:
    def __init__(self, result: PageContent) -> None:
        self.result = result
        self.seen: list[str] = []

    async def fetch_many(self, urls: list[str]) -> dict[str, PageContent]:
        self.seen.extend(urls)
        return {url: self.result.model_copy(update={"url": url}) for url in urls}

    async def fetch(self, url: str) -> PageContent:
        return (await self.fetch_many([url]))[url]

    async def preflight(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _MappedFetcher(_RecordingFetcher):
    def __init__(self, results: dict[str, PageContent]) -> None:
        super().__init__(PageContent(url=URL))
        self.results = results

    async def fetch_many(self, urls: list[str]) -> dict[str, PageContent]:
        self.seen.extend(urls)
        return {url: self.results[url] for url in urls}


async def test_routing_retries_empty_success_and_skips_a_real_404() -> None:
    empty_url = "https://listing.example/empty"
    missing_url = "https://listing.example/missing"
    http = _MappedFetcher(
        {
            empty_url: PageContent(url=empty_url, status=200, error="no readable text found"),
            missing_url: PageContent(url=missing_url, status=404, error="HTTP 404"),
        }
    )
    scrapling = _RecordingFetcher(PageContent(url=URL, text="adaptive result"))
    router = RoutingFetcher(_settings(), http=http, scrapling=scrapling)

    pages = await router.fetch_many([empty_url, missing_url])

    assert scrapling.seen == [empty_url]
    assert pages[empty_url].text == "adaptive result"


async def test_routing_preserves_block_status_for_browser_escalation() -> None:
    browser = _RecordingFetcher(PageContent(url=URL, text="browser result"))
    http = _RecordingFetcher(PageContent(url=URL, status=403, error="HTTP 403"))
    scrapling = _RecordingFetcher(PageContent(url=URL, error="scrapling failed: TimeoutError"))
    router = RoutingFetcher(
        _settings(browser_enabled=True), http=http, scrapling=scrapling, browser=browser
    )

    pages = await router.fetch_many([URL])

    assert scrapling.seen == [URL]
    assert browser.seen == [URL]
    assert pages[URL].text == "browser result"


async def test_missing_optional_scrapling_degrades_to_http(monkeypatch) -> None:
    real_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "scrapling.fetchers":
            raise ImportError("optional dependency missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    fetcher = build_fetcher(_settings())

    assert isinstance(fetcher, PageFetcher)
    await fetcher.aclose()
