"""Scrapling JSON-LD extraction and the browser (Agent Reach) read of JavaScript pages in the web stage."""

from __future__ import annotations

import json

from bot.campaign import MemoryCampaignStore
from bot.web_search.fetcher import FetchedPage
from bot.web_search.render import BrowserRenderer, RenderedPage, RenderError, page_of
from bot.web_search.store import MemoryWebStore
from bot.web_search.structured import facts_block, from_jsonld, structured
from bot.web_search.urls import url_key
from tests.test_web_search import (
    FakeFetcher,
    FakeSearcher,
    ListGenerator,
    campaign,
    run_until_done,
    worker,
)

PAGE = "https://www.example-pisos.es/inmueble/12345/"
LISTING = {
    "@context": "https://schema.org",
    "@type": "RealEstateListing",
    "name": "Parcela urbanizable en Boadilla del Monte",
    "url": PAGE,
    "description": "Parcela de 2.100 m² a 5 minutos en coche del metro ligero, todos los servicios.",
    "offers": {"@type": "Offer", "price": "480000", "priceCurrency": "EUR", "businessFunction": "http://purl.org/goodrelations/v1#Sell",
               "itemOffered": {"@type": "LandParcel", "floorSize": {"@type": "QuantitativeValue", "value": 2100, "unitCode": "MTK"},
                               "address": {"@type": "PostalAddress", "addressLocality": "Boadilla del Monte",
                                           "addressRegion": "Madrid", "addressCountry": "ES"}}},
}


def html_with(*blobs: object, body: str = "") -> str:
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(b) if not isinstance(b, str) else b}</script>'
                      for b in blobs)
    return f"<html><head><title>Parcela Boadilla</title>{scripts}</head><body>{body}</body></html>"


# --- extraction ---------------------------------------------------------------------------


def test_a_listing_becomes_flat_json_with_the_sites_exact_figures() -> None:
    data = structured(html_with(LISTING), PAGE)
    assert data.listings == ({
        "title": "Parcela urbanizable en Boadilla del Monte", "url": PAGE, "price": 480000, "currency": "EUR",
        "area_m2": 2100, "address": "Boadilla del Monte, Madrid, ES", "property_type": "landparcel", "deal": "sale",
        "description": LISTING["description"],
    },)
    assert data.listing_for(PAGE) == data.listings[0]
    assert facts_block(data.listings[0]).startswith('JSON-LD: {"title": "Parcela')


def test_an_item_list_gives_the_listing_links_and_graph_is_walked() -> None:
    graph = {"@context": "https://schema.org", "@graph": [
        {"@type": "WebPage", "name": "Terrenos en venta"},
        {"@type": "ItemList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "url": "/inmueble/1/"},
            {"@type": "ListItem", "position": 2, "item": {"@type": "Apartment", "url": "https://www.example-pisos.es/inmueble/2/",
                                                          "numberOfRooms": "3", "offers": {"price": "1.250.000 €"}}},
            {"@type": "ListItem", "position": 3, "url": "https://www.example-pisos.es/inmueble/3/"},
        ]},
    ]}
    data = structured(html_with(graph), "https://www.example-pisos.es/venta/madrid/")
    assert data.item_urls == ("https://www.example-pisos.es/inmueble/1/", "https://www.example-pisos.es/inmueble/2/",
                              "https://www.example-pisos.es/inmueble/3/")
    assert data.listings == ({"url": "https://www.example-pisos.es/inmueble/2/", "price": 1250000, "rooms": 3,
                              "property_type": "apartment"},)
    assert data.listing_for("https://www.example-pisos.es/venta/madrid/") == data.listings[0]  # the only one


def test_units_prices_and_junk() -> None:
    feet = {"@type": "House", "floorSize": {"value": "1,000", "unitCode": "FTK"}, "offers": {"price": 350000.5}}
    hectares = {"@type": "Residence", "lotSize": {"value": 2, "unitCode": "HAR"}}
    shop = {"@type": "Product", "name": "Mug"}  # no real-estate fact: not a listing
    data = from_jsonld([json.dumps(feet), json.dumps(hectares), json.dumps(shop), "{broken", "[]"], PAGE)
    assert [item.get("area_m2") for item in data.listings] == [92.9, 20000]
    assert data.listings[0]["price"] == 350000.5
    assert structured("<html><body>no data</body></html>", PAGE).listings == ()
    assert structured("\x00<<<", PAGE).listings == ()


def test_opengraph_price_when_there_is_no_json_ld() -> None:
    html = ('<html><head><meta property="og:title" content="Chalet en Pozuelo">'
            '<meta property="product:price:amount" content="950000"><meta property="product:price:currency" content="eur">'
            "</head><body></body></html>")
    assert structured(html, PAGE).listings == ({"price": 950000, "currency": "EUR", "title": "Chalet en Pozuelo", "url": PAGE},)


# --- the web stage ------------------------------------------------------------------------


class FakeRenderer:
    def __init__(self, page: RenderedPage | None = None, *, fail: bool = False) -> None:
        self.page, self.fail, self.calls = page, fail, []

    async def render(self, url: str) -> RenderedPage:
        self.calls.append(url)
        if self.fail:
            raise RenderError("browser_unavailable:ClientError")
        return self.page or RenderedPage(url, "Parcela", "Parcela de 2.100 m² en Boadilla del Monte. " * 5)


async def test_the_post_starts_with_the_sites_json() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    fetcher = FakeFetcher({PAGE: html_with(LISTING, body="<main><p>Parcela en venta. Contacto: agencia.</p></main>")})
    renderer = FakeRenderer()
    w = worker(campaigns, store, FakeSearcher(default=[PAGE]), fetcher, ListGenerator(["parcela Boadilla Madrid"]))
    w.renderer = renderer
    await run_until_done(w, cid)
    [post] = store.posts
    assert post["text"].startswith("JSON-LD: {")
    assert '"price": 480000' in post["text"] and '"area_m2": 2100' in post["text"] and "Contacto: agencia" in post["text"]
    assert renderer.calls == []  # HTTP already had the listing


async def test_a_portal_page_queues_its_json_ld_listings() -> None:
    index = "https://www.example-pisos.es/venta/terrenos/madrid/"
    items = {"@type": "ItemList", "itemListElement": [
        {"@type": "ListItem", "url": f"https://www.example-pisos.es/inmueble/{n}/"} for n in (1, 2, 3)]}
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    fetcher = FakeFetcher({index: html_with(items, body="<div id=app></div>")})
    w = worker(campaigns, store, FakeSearcher(default=[index]), fetcher, ListGenerator(["terrenos Madrid"]))
    await run_until_done(w, cid)
    assert fetcher.fetched[0] == index
    assert set(fetcher.fetched[1:]) == {f"https://www.example-pisos.es/inmueble/{n}/" for n in (1, 2, 3)}
    assert len(store.posts) == 3


async def test_a_javascript_page_is_read_in_the_browser_once() -> None:
    shell = "<html><head><title>Cargando…</title></head><body><div id=root></div><script>app()</script></body></html>"
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    renderer = FakeRenderer(RenderedPage(PAGE, "Parcela Boadilla", "Parcela urbanizable de 2.100 m² en Boadilla. " * 5,
                                         jsonld=(json.dumps(LISTING),)))
    w = worker(campaigns, store, FakeSearcher(default=[PAGE]), FakeFetcher({PAGE: shell}), ListGenerator(["parcela Boadilla"]))
    w.renderer = renderer
    await run_until_done(w, cid)
    assert renderer.calls == [PAGE]
    [post] = store.posts
    assert post["text"].startswith("JSON-LD:") and "Boadilla" in post["text"]


async def test_the_browser_read_of_javascript_pages_is_capped() -> None:
    shell = "<html><body><div id=root></div></body></html>"
    urls = [f"https://site{n}.es/inmueble/{n}/" for n in range(4)]
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    fetcher = FakeFetcher({u: shell for u in urls})
    renderer = FakeRenderer(fail=True)
    w = worker(campaigns, store, FakeSearcher(default=urls), fetcher, ListGenerator(["parcela Madrid"]),
               max_renders_per_campaign=2)
    w.renderer = renderer
    await run_until_done(w, cid)
    assert len(renderer.calls) == 2           # the cap
    assert store.posts == []                  # a failed browser read keeps the HTTP result (no text)


async def test_browser_renderer_leases_the_website_profile_and_releases_it() -> None:
    class Browser:
        def __init__(self) -> None:
            self.log: list[str] = []

        async def acquire(self, profile_id: str, profile_name: str, persisted_state: str, *, platform: str = "facebook") -> str:
            self.log.append(f"acquire {profile_id} {platform}")
            return "lease"

        async def snapshot(self, lease: str, url: str, timeout_ms: int) -> dict[str, object]:
            self.log.append(f"snapshot {url} {timeout_ms}")
            return {"url": url, "title": "T", "text": "body", "links": [{"url": "https://a.es/x", "text": "x"}, {"bad": 1}],
                    "jsonld": ["{}", 5]}

        async def release(self, lease: str, next_state: str = "READY") -> None:
            self.log.append("release")

    browser = Browser()
    page = await BrowserRenderer(browser, timeout_seconds=20).render(PAGE)
    assert browser.log == ["acquire web-search-render website", f"snapshot {PAGE} 20000", "release"]
    assert page == RenderedPage(PAGE, "T", "body", (("https://a.es/x", "x"),), ("{}",))
    assert page_of({}, PAGE).url == PAGE


def test_fetched_page_type_is_unchanged() -> None:
    assert FetchedPage(PAGE, "<html></html>").url == PAGE


# --- listings an index page describes itself (via="index") ----------------------------------

SITE = "https://www.example-pisos.es"
INDEX = f"{SITE}/venta/terrenos/madrid/"


def index_html(extra: list[dict] | None = None) -> str:
    items = [{"@type": "ListItem", "position": n, "item": {
        "@type": "Apartment", "name": f"Piso {n}", "url": f"{SITE}/inmueble/{n}0000/",
        "numberOfRooms": 3, "floorSize": {"value": 80 + n, "unitCode": "MTK"}, "offers": {"price": 100000 * n, "priceCurrency": "EUR"},
        "address": {"addressLocality": "Madrid"}}} for n in (1, 2)]
    items.append({"@type": "ListItem", "position": 3, "url": f"{SITE}/inmueble/30000/"})  # a bare link: no card
    items.extend(extra or [])
    return html_with({"@type": "ItemList", "itemListElement": items}, body="<div id=app></div>")


async def index_run(pages: dict[str, str], errors: dict[str, str] | None = None, **config):
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    fetcher = FakeFetcher(pages, errors=errors)
    w = worker(campaigns, store, FakeSearcher(default=[INDEX]), fetcher, ListGenerator(["terrenos Madrid"]), **config)
    await run_until_done(w, cid)
    return store, fetcher


async def test_listings_of_an_index_page_become_index_posts_when_the_detail_pages_fail() -> None:
    errors = {f"{SITE}/inmueble/{n}0000/": "http_403" for n in (1, 2, 3)}
    store, _ = await index_run({INDEX: index_html()}, errors)
    posts = {p["url"]: p for p in store.posts}
    assert set(posts) == {f"{SITE}/inmueble/10000/", f"{SITE}/inmueble/20000/"}  # the bare link has no data
    first = posts[f"{SITE}/inmueble/10000/"]
    assert first["via"] == "index" and first["text"].startswith("JSON-LD: {")
    assert '"price": 100000' in first["text"] and '"rooms": 3' in first["text"] and "Piso 1" in first["text"]
    assert "Ссылка: " + first["url"] in first["text"] and "Данные со страницы результатов example-pisos.es" in first["text"]
    assert len({p["url"] for p in store.posts}) == len(store.posts)  # one post per url


async def test_a_detail_page_read_later_is_the_only_post_of_its_url() -> None:
    detail = f"{SITE}/inmueble/10000/"
    pages = {INDEX: index_html(), detail: html_with({**LISTING, "url": detail}, body="<main><p>Piso en venta. Contacto: agencia.</p></main>")}
    errors = {f"{SITE}/inmueble/{n}0000/": "http_403" for n in (2, 3)}
    store, _ = await index_run(pages, errors)
    posts = {p["url"]: p for p in store.posts}
    assert posts[detail]["via"] == "page" and "Contacto: agencia" in posts[detail]["text"]
    assert posts[f"{SITE}/inmueble/20000/"]["via"] == "index"
    assert [p["url"] for p in store.posts].count(detail) == 1


async def test_index_posts_are_capped_by_max_links_per_index() -> None:
    errors = {f"{SITE}/inmueble/{n}0000/": "http_403" for n in (1, 2, 3)}
    store, _ = await index_run({INDEX: index_html()}, errors, max_links_per_index=1)
    assert [p["url"] for p in store.posts] == [f"{SITE}/inmueble/10000/"]


async def test_an_unknown_page_is_a_listing_only_with_price_and_area() -> None:
    plain = "https://blog-ejemplo.es/guia/comprar-piso-en-madrid"
    priced = "https://blog-ejemplo.es/guia/piso-en-madrid"
    body = "<main><p>Guía para comprar una vivienda en Madrid: pasos, notaría, impuestos y consejos útiles para el comprador.</p><p>Información general sobre hipotecas, tasaciones, plazos y documentos necesarios. "
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    pages = {plain: f"<html><head><title>Guía</title></head><body>{body}</p></main></body></html>",
             priced: f"<html><head><title>Piso</title></head><body>{body} Piso de 85 m² por 250.000 €.</p></main></body></html>"}
    w = worker(campaigns, store, FakeSearcher(default=[plain, priced]), FakeFetcher(pages), ListGenerator(["piso Madrid"]),
               cover_portals=False)
    await run_until_done(w, cid)
    assert [p["url"] for p in store.posts] == [priced]
    assert store.urls[cid][url_key(plain)].detail == "not_a_listing"
