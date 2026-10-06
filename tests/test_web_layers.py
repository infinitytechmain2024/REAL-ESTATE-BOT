"""Fetch layers of the web stage: HTTP, then the browser, then an optional scrape API; host blocks per layer."""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from bot.campaign import MemoryCampaignStore
from bot.web_search.fetcher import FetchedPage, FetchError
from bot.web_search.render import RenderedPage
from bot.web_search.scrape_api import ScrapeApiClient
from bot.web_search.store import MemoryWebStore
from bot.web_search.urls import url_key
from bot.web_search.worker import WebSearchConfig, WebSearchWorker, looks_blocked
from tests.test_web_search import (
    FakeFetcher,
    FakeSearcher,
    ListGenerator,
    campaign,
    listing_page,
    run_until_done,
)
from tests.test_web_structured import FakeRenderer

HIT = ("Terreno en venta en Boadilla del Monte - idealista",
       "Terreno urbanizable de 1.200 m² en Boadilla del Monte, Madrid. 480.000 €. Todos los servicios.")
URLS = [f"https://www.idealista.com/inmueble/{61000000 + n}/" for n in range(6)]
TEXT = "Terreno urbanizable de 1.200 m² en Boadilla del Monte, Madrid. 480.000 €. Todos los servicios. " * 3
CAPTCHA = "<html><head><title>Un momento</title></head><body><p>Please solve the CAPTCHA to continue</p></body></html>"


class PerUrlRenderer(FakeRenderer):
    """Every URL renders to its own listing text (identical posts of one site are merged)."""

    async def render(self, url: str) -> RenderedPage:
        self.calls.append(url)
        return RenderedPage(url, "Terreno", f"{url} {TEXT}")


class FakeScraper:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail, self.calls = fail, []

    async def fetch(self, url: str) -> FetchedPage:
        self.calls.append(url)
        if self.fail:
            raise FetchError("scrape_http_500")
        return FetchedPage(url, listing_page("Terreno por scrape"))


async def setup(*urls: str, fetcher: FakeFetcher, renderer=None, scraper=None, hit=True, **config):
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    searcher = FakeSearcher(default=list(urls), texts={u: HIT for u in urls} if hit else {})
    w = WebSearchWorker(campaigns, store, searcher, fetcher, ListGenerator(["terreno Boadilla Madrid"]),
                        renderer=renderer, scraper=scraper,
                        config=WebSearchConfig(pages_per_tick=1, cover_portals=False, **config))
    await run_until_done(w, cid)
    return store, cid


def refused(*urls: str, code: str = "http_403") -> FakeFetcher:
    return FakeFetcher(errors={u: code for u in urls})


async def test_a_403_goes_to_the_browser_and_the_page_is_stored() -> None:
    renderer = FakeRenderer(RenderedPage(URLS[0], "Terreno", TEXT))
    fetcher = refused(URLS[0])
    store, cid = await setup(URLS[0], fetcher=fetcher, renderer=renderer)
    assert fetcher.fetched == [URLS[0]] and renderer.calls == [URLS[0]]
    [post] = store.posts
    assert post["via"] == "page" and "Boadilla" in post["text"]
    assert store.urls[cid][url_key(URLS[0])].rendered and store.urls[cid][url_key(URLS[0])].detail is None
    assert store.hosts["idealista.com"]["http_refusals"] == 1 and store.hosts["idealista.com"]["render_refusals"] == 0


@pytest.mark.parametrize("code", ["http_429", "http_503"])
async def test_429_and_503_also_go_to_the_browser(code: str) -> None:
    renderer = FakeRenderer(RenderedPage(URLS[0], "Terreno", TEXT))
    store, _ = await setup(URLS[0], fetcher=refused(URLS[0], code=code), renderer=renderer)
    assert renderer.calls == [URLS[0]] and len(store.posts) == 1


async def test_a_refusal_in_the_browser_too_gives_the_search_result_card() -> None:
    renderer = FakeRenderer(fail=True)
    store, cid = await setup(URLS[0], fetcher=refused(URLS[0]), renderer=renderer)
    assert renderer.calls == [URLS[0]]
    [post] = store.posts
    assert post["via"] == "search" and "480.000 €" in post["text"]
    assert store.urls[cid][url_key(URLS[0])].detail == "search_snippet"
    assert store.hosts["idealista.com"]["http_refusals"] == 1


async def test_a_captcha_page_in_the_browser_is_a_render_refusal() -> None:
    renderer = FakeRenderer(RenderedPage(URLS[0], "DataDome", "Access denied. Are you a robot?"))
    store, _ = await setup(URLS[0], fetcher=refused(URLS[0]), renderer=renderer)
    [post] = store.posts
    assert post["via"] == "search"
    assert store.hosts["idealista.com"]["render_refusals"] == 1


async def test_robots_disallow_never_renders() -> None:
    renderer = FakeRenderer()
    fetcher = FakeFetcher(disallow={URLS[0]})
    store, _ = await setup(URLS[0], fetcher=fetcher, renderer=renderer)
    assert fetcher.fetched == [] and renderer.calls == []
    assert [p["via"] for p in store.posts] == ["search"]


async def test_when_the_renders_are_used_up_the_refused_page_gets_its_card() -> None:
    renderer = PerUrlRenderer()
    store, _ = await setup(*URLS[:3], fetcher=refused(*URLS[:3]), renderer=renderer, max_renders_per_campaign=2)
    assert len(renderer.calls) == 2
    assert sorted(p["via"] for p in store.posts) == ["page", "page", "search"]


async def test_render_on_refusal_can_be_switched_off() -> None:
    renderer = FakeRenderer(RenderedPage(URLS[0], "Terreno", TEXT))
    store, _ = await setup(URLS[0], fetcher=refused(URLS[0]), renderer=renderer, render_on_refusal=False)
    assert renderer.calls == [] and [p["via"] for p in store.posts] == ["search"]


async def test_a_captcha_looking_200_goes_to_the_browser() -> None:
    renderer = FakeRenderer(RenderedPage(URLS[0], "Terreno", TEXT))
    fetcher = FakeFetcher({URLS[0]: CAPTCHA})
    store, _ = await setup(URLS[0], fetcher=fetcher, renderer=renderer)
    assert renderer.calls == [URLS[0]]
    assert [p["via"] for p in store.posts] == ["page"]
    assert store.hosts["idealista.com"]["http_refusals"] == 1


def test_only_a_short_page_with_a_marker_looks_blocked() -> None:
    assert looks_blocked("", "Access Denied")
    assert looks_blocked("DataDome", "")
    assert not looks_blocked("Piso", "captcha " + "x" * 500)
    assert not looks_blocked("Piso", "Terreno de 1.200 m2")


async def test_three_http_refusals_block_only_the_http_layer_and_the_browser_takes_over() -> None:
    renderer = PerUrlRenderer()
    fetcher = refused(*URLS)
    store, _ = await setup(*URLS, fetcher=fetcher, renderer=renderer)
    assert fetcher.fetched == URLS[:3]               # then HTTP is blocked: no more requests
    assert len(renderer.calls) == 6 and len(store.posts) == 6
    host = store.hosts["idealista.com"]
    assert host["http_blocked_until"] is not None and host["render_blocked_until"] is None
    assert host["blocked_until"] is None             # not every layer is blocked
    assert await store.layer_state("idealista.com") == {"http": False, "render": True}


async def test_when_every_layer_is_blocked_nothing_is_fetched_and_the_card_is_kept() -> None:
    renderer = FakeRenderer(fail=True)
    fetcher = FakeFetcher()
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    until = store.now() + timedelta(hours=5)
    host = store._host("idealista.com")
    host.update(http_blocked_until=until, render_blocked_until=until, blocked_until=until)
    w = WebSearchWorker(campaigns, store, FakeSearcher(default=URLS[:2], texts={u: HIT for u in URLS}), fetcher,
                        ListGenerator(["terreno Boadilla Madrid"]), renderer=renderer,
                        config=WebSearchConfig(pages_per_tick=1, cover_portals=False))
    await run_until_done(w, cid)
    assert fetcher.fetched == [] and renderer.calls == []
    assert [p["via"] for p in store.posts] == ["search", "search"]
    assert store.urls[cid][url_key(URLS[0])].detail == "search_snippet"


async def test_a_blocked_http_layer_goes_straight_to_the_browser() -> None:
    renderer = FakeRenderer(RenderedPage(URLS[0], "Terreno", TEXT))
    fetcher = FakeFetcher()
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    store._host("idealista.com")["http_blocked_until"] = store.now() + timedelta(hours=5)
    w = WebSearchWorker(campaigns, store, FakeSearcher(default=[URLS[0]], texts={URLS[0]: HIT}), fetcher,
                        ListGenerator(["terreno Boadilla Madrid"]), renderer=renderer,
                        config=WebSearchConfig(pages_per_tick=1, cover_portals=False))
    await run_until_done(w, cid)
    assert fetcher.fetched == [] and renderer.calls == [URLS[0]]
    assert [p["via"] for p in store.posts] == ["page"]


async def test_without_a_browser_three_refusals_still_block_the_host() -> None:
    fetcher = refused(*URLS)
    store, _ = await setup(*URLS, fetcher=fetcher)
    assert fetcher.fetched == URLS[:3] and store.hosts["idealista.com"]["blocked_until"] is not None


async def test_the_scrape_api_is_the_last_layer() -> None:
    scraper = FakeScraper()
    renderer = FakeRenderer(fail=True)
    fetcher = refused(URLS[0])
    store, cid = await setup(URLS[0], fetcher=fetcher, renderer=renderer, scraper=scraper)
    assert renderer.calls == [URLS[0]] and scraper.calls == [URLS[0]]
    [post] = store.posts
    assert post["via"] == "page" and "Terreno por scrape" in post["title"]
    assert store.urls[cid][url_key(URLS[0])].scraped
    host = store.hosts["idealista.com"]
    assert host["http_refusals"] == 1 and host["render_refusals"] == 0  # a failed render is not a refusal


async def test_the_scrape_api_is_capped_and_a_failure_keeps_the_card() -> None:
    scraper = FakeScraper(fail=True)
    store, _ = await setup(*URLS[:3], fetcher=refused(*URLS[:3]), scraper=scraper, max_scrape_api_per_campaign=2)
    assert len(scraper.calls) == 2
    assert [p["via"] for p in store.posts] == ["search"] * 3


async def test_scrape_api_client_sends_the_key_and_the_encoded_url_and_hides_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=b"<html>ok</html>")

    client = ScrapeApiClient("https://unlock.example/v1", "s3cret",
                             client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    page = await client.fetch("https://www.idealista.com/inmueble/1/?a=b&c=d")
    assert page.html == "<html>ok</html>" and page.url == "https://www.idealista.com/inmueble/1/?a=b&c=d"
    assert seen[0].headers["authorization"] == "Bearer s3cret"
    assert seen[0].url.params["url"] == "https://www.idealista.com/inmueble/1/?a=b&c=d"
    assert "s3cret" not in repr(client)


@pytest.mark.parametrize(("status", "ctype", "body", "code"), [
    (403, "text/html", b"", "scrape_http_403"),
    (200, "application/json", b"{}", "not_html"),
    (200, "text/html", b"x" * 100, "too_large"),
])
async def test_scrape_api_client_refuses_bad_answers(status: int, ctype: str, body: bytes, code: str) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, headers={"content-type": ctype}, content=body))
    client = ScrapeApiClient("https://unlock.example/v1", "k", max_bytes=50, client=httpx.AsyncClient(transport=transport))
    with pytest.raises(FetchError, match=code):
        await client.fetch("https://a.es/x")


INDEX = "https://www.idealista.com/venta-terrenos/madrid-provincia/"


async def test_a_host_blocked_for_http_and_browser_still_gets_the_scrape_api() -> None:
    scraper = FakeScraper()
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    until = store.now() + timedelta(hours=5)
    store._host("idealista.com").update(http_blocked_until=until, render_blocked_until=until, blocked_until=until)
    fetcher = FakeFetcher()
    w = WebSearchWorker(campaigns, store, FakeSearcher(default=[URLS[0]], texts={URLS[0]: HIT}), fetcher,
                        ListGenerator(["terreno Boadilla Madrid"]), renderer=FakeRenderer(fail=True), scraper=scraper,
                        config=WebSearchConfig(pages_per_tick=1, cover_portals=False))
    await run_until_done(w, cid)
    assert fetcher.fetched == [] and scraper.calls == [URLS[0]]
    assert [p["via"] for p in store.posts] == ["page"]


async def test_a_url_no_layer_can_read_is_not_claimed_so_it_spends_no_budget() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    store._host("idealista.com")["http_blocked_until"] = store.now() + timedelta(hours=5)
    fetcher = FakeFetcher()
    w = WebSearchWorker(campaigns, store, FakeSearcher(default=URLS[:2], texts={u: HIT for u in URLS}), fetcher,
                        ListGenerator(["terreno Boadilla Madrid"]), scraper=FakeScraper(),
                        config=WebSearchConfig(pages_per_tick=1, cover_portals=False, max_scrape_api_per_campaign=0))
    await run_until_done(w, cid)
    assert fetcher.fetched == []
    assert [p["via"] for p in store.posts] == ["search", "search"]
    assert all(p.get("layer") != "none" for p in store.posts)
    assert store.hosts["idealista.com"].get("pages_fetched", 0) == 0


async def test_a_refused_index_page_gets_the_browser_but_never_the_scrape_api() -> None:
    scraper, renderer = FakeScraper(), FakeRenderer(fail=True)
    await setup(INDEX, fetcher=refused(INDEX), renderer=renderer, scraper=scraper)
    assert renderer.calls == [INDEX] and scraper.calls == []


async def test_a_refused_index_page_skips_the_browser_when_the_flag_is_off() -> None:
    scraper, renderer = FakeScraper(), FakeRenderer(fail=True)
    await setup(INDEX, fetcher=refused(INDEX), renderer=renderer, scraper=scraper, render_index_on_refusal=False)
    assert renderer.calls == [] and scraper.calls == []
