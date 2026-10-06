"""Website search stage with fakes (no network): queries in rounds, URL/site de-duplication,
portal index pages, caps, robots.txt, the fetcher's policy and the campaign status labels."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.runner import ANALYSIS, CampaignRunner
from bot.campaign.runs import MemoryRunStore
from bot.campaign.status_text import FACEBOOK, WEB, is_user_status
from bot.web_search.extract import listing_links, looks_like_index, parse_html, post_text
from bot.web_search.fetcher import FetchedPage, FetchError, PageFetcher
from bot.web_search.models import GeneratedQuery, WebStatus
from bot.web_search.queries import (
    FallbackQueryGenerator,
    OpenRouterQueryGenerator,
    QueryGenerationError,
    QueryTask,
    TemplateQueryGenerator,
    cover_portals,
    dedupe,
    missing_portals,
    parse_queries,
    portal_quota,
    query_key,
)
from bot.web_search.searxng import SearchError, SearchHit, SearxngClient
from bot.web_search.store import MemoryWebStore
from bot.web_search.urls import (
    SPAIN_PORTALS,
    SPAIN_PORTALS_BY_KIND,
    classify_url,
    fetchable,
    host_of,
    url_key,
)
from bot.web_search.worker import WebSearchConfig, WebSearchWorker, query_task
from tests.test_campaign_runner import FakeMessenger

GOAL = "участок от 1000 м² под застройку в пригороде Мадрида, покупка"
OWNER, USER = 1, 2

INDEX_URL = "https://www.fotocasa.es/es/comprar/terrenos/madrid-provincia/todas-las-zonas/l"
INDEX_HTML = """<!doctype html><html><head><title>Terrenos en venta en Madrid provincia - fotocasa</title>
<meta name="description" content="1.234 terrenos en venta"></head>
<body><nav><a href="/es/comprar/viviendas/madrid-capital/todas-las-zonas/l">Viviendas</a>
<a href="/es/comprar/terreno/madrid-capital/9/99999999/d">nav listing (ignored: inside nav)</a></nav>
<main><h1>Terrenos en venta</h1>
<article><a href="/es/comprar/terreno/boadilla-del-monte/sin-urbanizar/183456789/d?from=list">Terreno 1.200 m2 Boadilla</a></article>
<article><a href="https://www.fotocasa.es/es/comprar/terreno/pozuelo-de-alarcon/urbanizable/183456790/d">Parcela 1.500 m2 Pozuelo</a></article>
<article><a href="/es/comprar/terreno/boadilla-del-monte/sin-urbanizar/183456789/d?from=list#fotos">same listing again</a></article>
<article><a href="/es/comprar/terreno/las-rozas/urbano/183456791/d">Solar urbano Las Rozas</a></article>
<a href="https://www.idealista.com/inmueble/12345678/">another site</a>
<a href="/es/comprar/terrenos/madrid-provincia/todas-las-zonas/l/2">Página 2</a>
<form action="/buscar"><button>Buscar</button></form>
<script>var ads = "/es/comprar/terreno/x/1/123456799/d";</script>
</main><footer><a href="/es/aviso-legal">Aviso legal</a></footer></body></html>"""
LISTING_HTML = """<html><head><title>Terreno urbanizable en venta en Boadilla del Monte - 1.200 m²</title></head>
<body><main><h1>Terreno urbanizable en Boadilla del Monte, Madrid</h1>
<p>Parcela de 1.200 m² para construir vivienda unifamiliar, a 5 minutos en coche del metro ligero.</p>
<p>Precio: 480.000 €. Todos los servicios a pie de parcela. Contacto: agencia.</p></main></body></html>"""


def listing_page(title: str) -> str:
    return LISTING_HTML.replace("Boadilla del Monte - 1.200 m²", title)


class FakeSearcher:
    def __init__(self, results: dict[str, list[str]] | None = None, default: list[str] | None = None,
                 texts: dict[str, tuple[str, str]] | None = None) -> None:
        self.results, self.default, self.texts = results or {}, default or [], texts or {}
        self.calls: list[tuple[str, str | None]] = []
        self.fail: set[str] = set()

    async def search(self, query: str, *, language: str | None = None) -> list[SearchHit]:
        self.calls.append((query, language))
        if query in self.fail:
            raise SearchError("http_502")
        return [SearchHit(u, *self.texts.get(u, ("", ""))) for u in self.results.get(query, self.default)]


class FakeFetcher:
    def __init__(self, pages: dict[str, str] | None = None, *, disallow: set[str] = frozenset(),
                 errors: dict[str, str] | None = None) -> None:
        self.pages = pages or {}
        self.disallow, self.errors = set(disallow), errors or {}
        self.fetched: list[str] = []

    async def allowed(self, url: str) -> bool:
        return url not in self.disallow

    async def fetch(self, url: str) -> FetchedPage:
        self.fetched.append(url)
        if url in self.errors:
            raise FetchError(self.errors[url])
        return FetchedPage(url, self.pages.get(url, listing_page(url)))


class ListGenerator:
    """Hands out queries from a list, recording what it was told was already used."""

    def __init__(self, queries: list[str]) -> None:
        self.queries = list(queries)
        self.calls: list[tuple[list[str], int]] = []

    async def generate(self, task: QueryTask, *, used: list[str], count: int) -> list[GeneratedQuery]:
        self.calls.append((list(used), count))
        return dedupe([GeneratedQuery(q, "es") for q in self.queries], used, limit=count)


async def campaign(store: MemoryCampaignStore, *, state: str = "running", owner: int = USER) -> str:
    plan = plan_campaign(GOAL, vertical="real_estate", location="Madrid")
    cid = await store.create(plan, chat_id=owner, requested_by=owner, source_text=GOAL, actor="test")
    if state != "planned":
        await store.set_state(cid, state, "test")
    return cid


def worker(campaigns, store, searcher, fetcher, generator, **config) -> WebSearchWorker:
    return WebSearchWorker(campaigns, store, searcher, fetcher, generator, config=WebSearchConfig(**config))


async def run_until_done(w: WebSearchWorker, cid: str, limit: int = 60) -> None:
    for _ in range(limit):
        await w.tick()
        run = await w.store.get_run(cid)
        if run is not None and run.state != "searching":
            return
    raise AssertionError("web stage did not finish")


# --- queries ------------------------------------------------------------------------------


def test_query_key_folds_case_accents_word_order_and_stop_words() -> None:
    assert query_key("Terreno urbanizable en las afueras de Madrid") == query_key("madrid AFUERAS terreno urbanizable")
    assert query_key("parcela en venta Móstoles") == query_key("parcelas venta mostoles")
    assert query_key("terreno 1000m2 Madrid") == query_key("terreno 1000 m2 Madrid")
    assert query_key("site:www.idealista.com terreno Madrid") == query_key("terreno Madrid site:idealista.com")
    assert query_key("terreno Madrid") != query_key("terreno Madrid site:idealista.com")


def test_dedupe_drops_near_identical_queries_and_keeps_different_ones() -> None:
    used = ["terreno urbanizable afueras Madrid 1000 m2", "участок купить Мадрид"]
    fresh = [
        GeneratedQuery("Terrenos urbanizables afueras de Madrid 1000m2", "es"),  # same meaning
        GeneratedQuery("земельный участок купить Мадрид", "ru"),                  # one adjective more
        GeneratedQuery("parcela en venta cerca metro Madrid", "es"),
        GeneratedQuery("parcela venta cerca del metro de Madrid", "es"),           # duplicate of the one above
        GeneratedQuery("site:fotocasa.es terreno urbanizable Madrid", "es"),
        GeneratedQuery('"x"', "es"),                                               # too short after cleaning
        GeneratedQuery("building plot for sale near Madrid", "xx"),                # unknown language -> None
    ]
    kept = dedupe(fresh, used, limit=10)
    assert [q.text for q in kept] == ["parcela en venta cerca metro Madrid", "site:fotocasa.es terreno urbanizable Madrid",
                                      "building plot for sale near Madrid"]
    assert kept[-1].language is None
    assert len(dedupe(fresh, [], limit=2)) == 2


async def test_template_generator_rounds_never_repeat_and_cover_languages_and_portals() -> None:
    plan = plan_campaign(GOAL, vertical="real_estate", location="Madrid")
    task = QueryTask(plan.goal, GOAL, plan.location, dict(plan.location_aliases), plan.vertical,
                     dict(plan.constraints), tuple(plan.languages))
    generator, used = TemplateQueryGenerator(), []
    for _ in range(3):
        round_ = await generator.generate(task, used=used, count=6)
        assert len(round_) == 6
        used += [q.text for q in round_]
    assert len({query_key(q) for q in used}) == len(used) == 18
    assert {"es", "en", "ru", "uk"} <= {q.language for q in await generator.generate(task, used=[], count=9)}
    assert any(q.startswith("site:idealista.com") for q in used) and any("terreno" in q for q in used)
    assert any("участок" in q for q in used) and any("ділянка" in q for q in used)


async def test_worker_generates_rounds_passing_used_queries_up_to_the_cap() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    queries = [f"terreno {town} Madrid" for town in (
        "Boadilla", "Pozuelo", "Majadahonda", "Rozas", "Torrelodones", "Galapagar", "Villanueva", "Brunete",
        "Navalcarnero", "Arganda", "Rivas", "Alcobendas", "Tres Cantos", "Colmenar", "Algete", "Getafe",
        "Leganes", "Fuenlabrada", "Mostoles", "Alcorcon", "Parla", "Pinto", "Valdemoro", "Aranjuez", "Chinchon",
        "Alcala", "Torrejon", "Coslada", "Mejorada", "Velilla", "Paracuellos", "Cobena", "Ajalvir", "Daganzo",
        "Meco", "Camarma", "Villalbilla", "Loeches", "Campo Real", "Morata", "Tielmes", "Perales", "Carabana")]
    generator = ListGenerator(queries)
    store = MemoryWebStore(campaigns)
    searcher = FakeSearcher()
    w = worker(campaigns, store, searcher, FakeFetcher(), generator, queries_per_round=12, max_queries_per_campaign=40,
               queries_per_tick=10, cover_portals=False)
    await run_until_done(w, cid)
    assert [count for _, count in generator.calls] == [12, 12, 12, 4]
    assert [len(used) for used, _ in generator.calls] == [0, 12, 24, 36]
    assert generator.calls[1][0] == [f"{q} España" for q in queries[:12]]  # the model is shown every query already used
    assert len(store.queries[cid]) == 40 and len({q.key for q in store.queries[cid]}) == 40
    assert len(searcher.calls) == 40 and len(set(searcher.calls)) == 40
    assert store.runs[cid].stop_reason == "queries_done"


async def test_a_round_without_new_queries_ends_the_stage() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    generator = ListGenerator(["terreno Boadilla Madrid", "terrenos boadilla madrid"])
    w = worker(campaigns, store, FakeSearcher(), FakeFetcher(), generator, cover_portals=False)
    await run_until_done(w, cid)
    assert [q.text for q in store.queries[cid]] == ["terreno Boadilla Madrid España"]
    assert store.runs[cid].stop_reason == "queries_exhausted"


async def test_a_query_another_campaign_searched_recently_is_not_searched_again() -> None:
    campaigns = MemoryCampaignStore()
    first = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    searcher = FakeSearcher()
    w = worker(campaigns, store, searcher, FakeFetcher(), ListGenerator(["terreno Boadilla Madrid"]),
               cover_portals=False)
    await run_until_done(w, first)
    second = await campaign(campaigns)
    await run_until_done(w, second)
    assert searcher.calls == [("terreno Boadilla Madrid España", "es-ES")]  # a Spanish campaign searches Spain
    assert [q.state for q in store.queries[second]] == ["skipped"]


def madrid_task(**extra) -> QueryTask:
    return QueryTask(goal="участок под застройку", task_text="участок от 1000 м² под застройку, покупка",
                     location="Madrid", location_aliases={"es": "Madrid"}, vertical="real_estate",
                     constraints={"deal": "sale"}, **extra)


def test_idealista_and_fotocasa_come_first_among_the_spanish_portals() -> None:
    assert SPAIN_PORTALS[:2] == ("idealista.com", "fotocasa.es")
    assert {"yaencontre.com", "pisos.com", "habitaclia.com", "milanuncios.com", "terrenos.es", "sareb.es",
            "green-acres.es"} <= set(SPAIN_PORTALS)
    assert missing_portals(madrid_task(), [])[:2] == ("idealista.com", "fotocasa.es")


def test_for_land_terrenos_and_sareb_come_right_after_fotocasa() -> None:
    portals = madrid_task().portals()  # «участок»: land
    assert portals[:4] == ("idealista.com", "fotocasa.es", "terrenos.es", "sareb.es")
    assert portals == SPAIN_PORTALS_BY_KIND["land"] and set(portals) <= set(SPAIN_PORTALS)
    flat = QueryTask(goal="квартира", task_text="квартира 2 комнаты, аренда", location="Madrid",
                     location_aliases={"es": "Madrid"}, vertical="real_estate")
    assert flat.portals() == SPAIN_PORTALS_BY_KIND["apartment"]


def test_a_portal_the_model_already_searched_counts_as_searched() -> None:
    used = ["site:www.idealista.com parcela Madrid", "terreno fotocasa Madrid"]  # a name in the words is not a site:
    assert missing_portals(madrid_task(), used)[0] == "fotocasa.es"


def test_cover_portals_keeps_the_models_portal_query_and_builds_the_skipped_one() -> None:
    task = madrid_task(required_portals=("idealista.com", "fotocasa.es"))
    model = [GeneratedQuery("parcela en venta cerca metro Madrid", "es"),
             GeneratedQuery("site:idealista.com solar urbanizable Comunidad de Madrid", "es"),
             GeneratedQuery("building plot for sale near Madrid", "en")]
    out = cover_portals(model, task, [], 4)
    assert out[0] == GeneratedQuery("site:idealista.com solar urbanizable Comunidad de Madrid", "es")
    assert out[1].text == "site:fotocasa.es terreno en venta 1000 m2 Madrid España"  # what the task asks, on that site
    assert [q.text for q in out[2:]] == ["parcela en venta cerca metro Madrid", "building plot for sale near Madrid"]


def test_portal_quota_is_a_third_of_a_round_but_never_less_than_two() -> None:
    assert [portal_quota(n) for n in (1, 2, 3, 4, 12)] == [1, 2, 2, 2, 4]


async def test_every_spanish_portal_is_searched_even_when_the_model_names_none() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    queries = [f"terreno {town} Madrid" for town in (
        "Boadilla", "Pozuelo", "Majadahonda", "Rozas", "Torrelodones", "Galapagar", "Villanueva", "Brunete",
        "Navalcarnero", "Arganda", "Rivas", "Alcobendas", "Tres Cantos", "Colmenar", "Algete", "Getafe",
        "Leganes", "Fuenlabrada", "Mostoles", "Alcorcon", "Parla", "Pinto", "Valdemoro", "Aranjuez")]
    searcher = FakeSearcher()
    w = worker(campaigns, store, searcher, FakeFetcher(), ListGenerator(queries), queries_per_round=12,
               max_queries_per_campaign=40, queries_per_tick=10)
    await run_until_done(w, cid)
    texts = [q.text for q in store.queries[cid]]
    assert [t.split()[0] for t in texts[:4]] == ["site:idealista.com", "site:fotocasa.es", "site:terrenos.es",
                                                 "site:sareb.es"]
    assert all(any(t.startswith(f"site:{p} ") for t in texts) for p in madrid_task().portals())
    assert sum(t.startswith("terreno ") for t in texts[:12]) == 8  # the model keeps two thirds of every round


def test_parse_queries_accepts_drift() -> None:
    content = "```json\n" + json.dumps({"queries": [
        {"text": "terreno urbanizable afueras Madrid", "language": "ES"},
        {"query": "parcela Madrid", "lang": "es", "site": "idealista.com"},
        "building plot Madrid",
        {"text": ""},
    ]}) + "\n```"
    assert parse_queries(content) == [GeneratedQuery("terreno urbanizable afueras Madrid", "es"),
                                      GeneratedQuery("site:idealista.com parcela Madrid", "es"),
                                      GeneratedQuery("building plot Madrid", None)]
    with pytest.raises(ValueError):
        parse_queries('{"queries": []}')


async def test_openrouter_generator_sends_strict_schema_and_falls_back_to_json_object_on_400() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["Authorization"] == "Bearer sk-test"
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": "structured outputs unsupported"})
        content = json.dumps({"queries": [{"text": "parcela en venta cerca metro Madrid", "language": "es"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    generator = OpenRouterQueryGenerator(api_key="sk-test", model="m", timeout_seconds=5, client=client)
    plan = plan_campaign(GOAL, vertical="real_estate", location="Madrid")
    task = QueryTask(plan.goal, GOAL, plan.location, dict(plan.location_aliases), plan.vertical)
    queries = await generator.generate(task, used=["terreno Madrid"], count=12)
    assert queries == [GeneratedQuery("parcela en venta cerca metro Madrid", "es")]
    assert [r["response_format"]["type"] for r in requests] == ["json_schema", "json_object"]
    assert requests[0]["response_format"]["json_schema"]["strict"] is True
    data = json.loads(requests[0]["messages"][1]["content"].split("\n", 1)[1])
    assert data["used"] == ["terreno Madrid"] and data["count"] == 12 and "idealista.com" in data["portals"]
    assert data["task"] == GOAL

    failing = OpenRouterQueryGenerator(api_key="sk-test", model="m", timeout_seconds=5, client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    with pytest.raises(QueryGenerationError):
        await failing.generate(task, used=[], count=3)
    # the fallback fills the round from templates when the model fails
    filled = await FallbackQueryGenerator(failing).generate(task, used=[], count=5)
    assert len(filled) == 5


# --- URLs, portals, index pages ---------------------------------------------------------------


def test_url_identity_site_and_portal_classification() -> None:
    assert url_key("http://www.Fotocasa.es/es/x/183456789/d?utm_source=g#fotos") == url_key("https://fotocasa.es/es/x/183456789/d")
    assert host_of("https://www.idealista.com/inmueble/1/") == "idealista.com"
    assert host_of("https://m.milanuncios.com/x") == "milanuncios.com"
    assert classify_url("https://www.idealista.com/inmueble/12345678/") == "listing"
    assert classify_url("https://www.idealista.com/venta-terrenos/madrid-provincia/") == "index"
    assert classify_url(INDEX_URL) == "index"
    assert classify_url("https://www.pisos.com/comprar/terreno-boadilla_del_monte-45123456789_100500/") == "listing"
    assert classify_url("https://www.milanuncios.com/terrenos-en-madrid/parcela-1000m2-512345678.htm") == "listing"
    assert classify_url("https://agencia-ejemplo.es/inmueble/venta-parcela-8812345") == "listing"
    assert classify_url("https://agencia-ejemplo.es/contacto") == "unknown"
    assert not fetchable("https://www.facebook.com/groups/x/")
    assert not fetchable("https://example.es/folleto.pdf")
    assert not fetchable("https://example.es/login?next=/")
    assert not fetchable("https://blocked.example/x", frozenset({"blocked.example"}))
    assert fetchable("https://www.fotocasa.es/es/comprar/terreno/x/183456789/d")


def test_index_page_yields_concrete_listing_links_on_the_same_site() -> None:
    page = parse_html(INDEX_HTML, INDEX_URL)
    links = listing_links(page, INDEX_URL, limit=10)
    assert links == [
        "https://www.fotocasa.es/es/comprar/terreno/boadilla-del-monte/sin-urbanizar/183456789/d?from=list",
        "https://www.fotocasa.es/es/comprar/terreno/pozuelo-de-alarcon/urbanizable/183456790/d",
        "https://www.fotocasa.es/es/comprar/terreno/las-rozas/urbano/183456791/d",
    ]
    assert listing_links(page, INDEX_URL, limit=2) == links[:2]
    assert looks_like_index(page, INDEX_URL)
    assert page.title.startswith("Terrenos en venta") and "Buscar" not in page.text and "var ads" not in page.text
    listing = parse_html(LISTING_HTML, "https://www.fotocasa.es/es/comprar/terreno/b/sin-urbanizar/183456789/d")
    assert not looks_like_index(listing, "https://www.fotocasa.es/")
    text = post_text(listing, limit=8000)
    assert text.startswith("Terreno urbanizable en venta en Boadilla del Monte") and "480.000 €" in text
    assert len(post_text(listing, limit=50)) == 50


async def test_worker_expands_an_index_page_into_listings_and_stores_only_listings() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    fetcher = FakeFetcher({INDEX_URL: INDEX_HTML})
    w = worker(campaigns, store, FakeSearcher(default=[INDEX_URL]), fetcher, ListGenerator(["terreno Boadilla Madrid"]),
               max_links_per_index=2)
    await run_until_done(w, cid)
    assert fetcher.fetched[0] == INDEX_URL
    assert len(fetcher.fetched) == 3  # the index, then two of its three listings (cap)
    assert [p["url"] for p in store.posts] == fetcher.fetched[1:]
    rows = store.urls[cid]
    assert rows[url_key(INDEX_URL)].kind == "index" and rows[url_key(INDEX_URL)].state == "fetched"
    assert sorted(r.depth for r in rows.values()) == [0, 1, 1]


# --- de-duplication ---------------------------------------------------------------------------


async def test_the_same_url_from_two_queries_is_fetched_once() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    listing = "https://www.idealista.com/inmueble/98765432/"
    searcher = FakeSearcher({"terreno Boadilla Madrid España": [listing],
                             "parcela Pozuelo venta Madrid España": [listing + "?utm_source=bing", "http://idealista.com/inmueble/98765432"]})
    fetcher = FakeFetcher()
    w = worker(campaigns, store, searcher, fetcher, ListGenerator(["terreno Boadilla Madrid", "parcela Pozuelo venta"]))
    await run_until_done(w, cid)
    assert fetcher.fetched == [listing]
    assert len(store.posts) == 1 and len(store.urls[cid]) == 1


async def test_a_page_read_for_one_campaign_is_never_fetched_for_another() -> None:
    campaigns = MemoryCampaignStore()
    first, second = await campaign(campaigns), await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    listing = "https://www.pisos.com/comprar/terreno-boadilla_del_monte-45123456789_100500/"
    fetcher = FakeFetcher()
    await run_until_done(worker(campaigns, store, FakeSearcher(default=[listing]), fetcher,
                                ListGenerator(["terreno Boadilla Madrid"])), first)
    await run_until_done(worker(campaigns, store, FakeSearcher(default=[listing]), fetcher,
                                ListGenerator(["parcela Boadilla venta"])), second)
    assert fetcher.fetched == [listing]
    assert store.urls[second][url_key(listing)].state == "duplicate"
    assert len(store.posts) == 1 and store.posts[0]["campaign_id"] == first


async def test_two_campaigns_racing_for_one_url_fetch_it_once() -> None:
    campaigns = MemoryCampaignStore()
    first, second = await campaign(campaigns), await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    listing = "https://www.idealista.com/inmueble/11112222/"
    fetcher = FakeFetcher()
    w = worker(campaigns, store, FakeSearcher(default=[listing]), fetcher,
               ListGenerator(["terreno Boadilla Madrid", "parcela Pozuelo venta"]), query_reuse_hours=1)
    for _ in range(20):  # both campaigns queue it before either reads it
        await w.tick()
    assert fetcher.fetched == [listing]
    states = {store.urls[c][url_key(listing)].state for c in (first, second)}
    assert states == {"fetched", "duplicate"}


async def test_a_claim_left_by_a_crashed_worker_is_taken_over_after_the_lease() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    now = [datetime(2026, 9, 26, tzinfo=UTC)]
    store = MemoryWebStore(campaigns, now=lambda: now[0])
    busy = "https://www.idealista.com/inmueble/33334444/"
    crashed = "https://www.idealista.com/inmueble/55556666/"
    for url, claimed in ((busy, now[0]), (crashed, now[0] - timedelta(seconds=600))):
        store.seen[url_key(url)] = {"url": url, "host": "idealista.com", "state": "fetching", "campaign_id": "x",
                                    "claimed_at": claimed, "finished_at": None, "post_id": None}
    fetcher = FakeFetcher()
    w = WebSearchWorker(campaigns, store, FakeSearcher(default=[busy, crashed]), fetcher,
                        ListGenerator(["terreno Boadilla Madrid"]), config=WebSearchConfig(lease_seconds=300),
                        now=lambda: now[0])
    await run_until_done(w, cid)
    # another worker is reading `busy` right now: not ours; `crashed` was claimed 10 min ago: taken over
    assert fetcher.fetched == [crashed]
    assert store.urls[cid][url_key(busy)].state == "duplicate"
    assert store.seen[url_key(crashed)]["state"] == "fetched"


# --- caps, robots, blocks ---------------------------------------------------------------------


async def test_caps_per_campaign_per_site_and_per_day() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    urls = [f"https://www.idealista.com/inmueble/{50000000 + n}/" for n in range(5)] + \
           [f"https://www.fotocasa.es/es/comprar/terreno/x/{190000000 + n}/d" for n in range(5)]
    fetcher = FakeFetcher()
    w = worker(campaigns, store, FakeSearcher(default=urls), fetcher, ListGenerator(["terreno Boadilla Madrid"]),
               max_pages_per_host=2, max_pages_per_campaign=10)
    await run_until_done(w, cid)
    assert sorted(host_of(u) for u in fetcher.fetched) == ["fotocasa.es"] * 2 + ["idealista.com"] * 2
    assert fetcher.fetched[:2] == [urls[0], urls[5]]  # site after site
    assert sum(r.state == "capped" for r in store.urls[cid].values()) == 6

    other = await campaign(campaigns)
    more = [f"https://agencia{n}.es/inmueble/venta-{7000000 + n}" for n in range(6)]
    fetcher2 = FakeFetcher()
    await run_until_done(worker(campaigns, store, FakeSearcher(default=more), fetcher2,
                                ListGenerator(["parcela Pozuelo venta"]), max_pages_per_campaign=3), other)
    assert len(fetcher2.fetched) == 3 and store.runs[other].stop_reason == "page_cap"

    third = await campaign(campaigns)
    fetcher3 = FakeFetcher()
    await run_until_done(worker(campaigns, store, FakeSearcher(default=[f"https://otra{n}.es/anuncio/{8000000 + n}" for n in range(4)]),
                                fetcher3, ListGenerator(["solar Rivas venta"]), max_pages_per_day=7), third)
    assert fetcher3.fetched == [] and store.runs[third].stop_reason == "daily_page_cap"


async def test_robots_disallowed_pages_and_refusing_sites_are_left_alone() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    private = "https://www.habitaclia.com/comprar-terreno-boadilla-i500000000001.htm"
    refusing = [f"https://www.idealista.com/inmueble/{60000000 + n}/" for n in range(5)]
    fetcher = FakeFetcher(disallow={private}, errors={u: "http_403" for u in refusing})
    w = worker(campaigns, store, FakeSearcher(default=[private, *refusing]), fetcher,
               ListGenerator(["terreno Boadilla Madrid"]), pages_per_tick=1)
    await run_until_done(w, cid)
    assert private not in fetcher.fetched and store.urls[cid][url_key(private)].state == "robots"
    assert fetcher.fetched == refusing[:3]  # three refusals block the site for a while
    assert [store.urls[cid][url_key(u)].detail for u in refusing[3:]] == ["host_blocked", "host_blocked"]
    assert store.posts == []


IDEALISTA_HIT = ("Terreno en venta en Boadilla del Monte - idealista",
                 "Terreno urbanizable de 1.200 m² en Boadilla del Monte, Madrid. 480.000 €. Todos los servicios.")


async def test_a_site_that_refuses_bots_still_gives_a_post_from_its_search_result() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    private = "https://www.habitaclia.com/comprar-terreno-boadilla-i500000000001.htm"
    refusing = [f"https://www.idealista.com/inmueble/{60000000 + n}/" for n in range(5)]
    index = "https://www.idealista.com/venta-terrenos/madrid/"
    texts = {u: IDEALISTA_HIT for u in [private, *refusing, index]}
    fetcher = FakeFetcher(disallow={private}, errors={u: "http_403" for u in [*refusing, index]})
    w = worker(campaigns, store, FakeSearcher(default=[private, *refusing, index], texts=texts), fetcher,
               ListGenerator(["terreno Boadilla Madrid"]), pages_per_tick=1, cover_portals=False)
    await run_until_done(w, cid)
    assert private not in fetcher.fetched  # robots.txt: the site is never asked, its search result is kept
    assert fetcher.fetched == refusing[:3]  # three refusals still block Idealista for a while
    posts = {p["url"]: p for p in store.posts}
    assert set(posts) == {private, *refusing}  # every listing, the blocked ones too; not the index page
    post = posts[refusing[0]]
    assert post["via"] == "search" and "480.000 €" in post["text"] and refusing[0] in post["text"]
    assert all(store.urls[cid][url_key(u)].detail == "search_snippet" for u in [private, *refusing])
    assert store.hosts["idealista.com"]["blocked_until"] is not None


async def test_a_search_result_without_a_snippet_is_not_a_post() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    url = "https://www.idealista.com/inmueble/60000001/"
    fetcher = FakeFetcher(errors={url: "http_403"})
    w = worker(campaigns, store, FakeSearcher(default=[url], texts={url: ("idealista", "")}), fetcher,
               ListGenerator(["terreno Boadilla Madrid"]), cover_portals=False)
    await run_until_done(w, cid)
    assert store.posts == [] and store.urls[cid][url_key(url)].detail == "http_403"


async def test_a_paused_site_source_is_not_read() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns, paused_hosts={"idealista.com"})
    fetcher = FakeFetcher()
    await run_until_done(worker(campaigns, store, FakeSearcher(default=["https://www.idealista.com/inmueble/77778888/"]),
                                fetcher, ListGenerator(["terreno Boadilla Madrid"])), cid)
    assert fetcher.fetched == []


async def test_a_failed_search_is_recorded_and_the_stage_goes_on() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    searcher = FakeSearcher(default=["https://www.idealista.com/inmueble/12121212/"])
    searcher.fail.add("terreno Boadilla Madrid España")
    fetcher = FakeFetcher()
    await run_until_done(worker(campaigns, store, searcher, fetcher,
                                ListGenerator(["terreno Boadilla Madrid", "parcela Pozuelo venta"]),
                                cover_portals=False), cid)
    assert [q.state for q in store.queries[cid]] == ["failed", "searched"]
    assert len(fetcher.fetched) == 1


async def test_a_cancelled_campaign_stops_its_web_stage_and_a_long_one_times_out() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    w = worker(campaigns, store, FakeSearcher(), FakeFetcher(), ListGenerator(["terreno Boadilla Madrid"]))
    await w.tick()
    await campaigns.cancel(cid, "test")
    await w.tick()
    assert store.runs[cid].state == "stopped" and store.runs[cid].stop_reason == "campaign_cancelled"
    assert await store.campaign_ids() == []

    now = [datetime(2026, 9, 26, tzinfo=UTC)]
    other = await campaign(campaigns)
    store2 = MemoryWebStore(campaigns, now=lambda: now[0])
    w2 = WebSearchWorker(campaigns, store2, FakeSearcher(), FakeFetcher(), ListGenerator(["a b c", "d e f"]),
                         config=WebSearchConfig(max_minutes_per_campaign=5), now=lambda: now[0])
    await w2.tick()
    now[0] += timedelta(minutes=6)
    await w2.tick()
    assert store2.runs[other].stop_reason == "time_cap"


async def test_query_task_carries_the_campaign_goal_city_and_requirements() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    task = query_task(await campaigns.get(cid))
    assert task.task_text == GOAL and task.location == "Madrid" and task.location_aliases["es"] == "Madrid"
    assert task.vertical == "real_estate" and "idealista.com" in task.portals()


# --- the fetcher ------------------------------------------------------------------------------


async def public(_host: str) -> list[str]:
    return ["93.184.216.34"]


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


async def test_fetcher_obeys_robots_paces_each_site_and_keeps_no_cookies() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert "cookie" not in request.headers
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /privado/\nCrawl-delay: 2\n")
        if request.url.path == "/moved":
            return httpx.Response(301, headers={"location": "/anuncio/12345678"})
        return httpx.Response(200, html="<html><title>ok</title><body>hola</body></html>",
                              headers={"set-cookie": "session=abc; Path=/"})

    sleeps, clock = Sleeps(), [100.0]
    fetcher = PageFetcher(user_agent="TestBot/1", host_interval_seconds=5, resolver=public, sleep=sleeps,
                          clock=lambda: clock[0], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await fetcher.allowed("https://agencia.es/anuncio/1")
    assert not await fetcher.allowed("https://agencia.es/privado/2")
    page = await fetcher.fetch("https://agencia.es/moved")
    assert page.url == "https://agencia.es/anuncio/12345678" and "hola" in page.html
    await fetcher.fetch("https://agencia.es/anuncio/2")
    assert seen.count("https://agencia.es/robots.txt") == 1
    assert sleeps.calls and all(s == pytest.approx(5) for s in sleeps.calls)  # one request per 5 s to one site


async def test_fetcher_refuses_private_targets_non_html_and_unreachable_robots() -> None:
    async def private(_host: str) -> list[str]:
        return ["10.0.0.5"]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "down.es":
            return httpx.Response(503)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = PageFetcher(user_agent="TestBot/1", resolver=private, sleep=Sleeps(), client=client)
    with pytest.raises(FetchError, match="private_target_forbidden"):
        await fetcher.fetch("https://intranet.example/")
    with pytest.raises(FetchError, match="private_target_forbidden"):
        await fetcher.fetch("http://127.0.0.1/")
    ok = PageFetcher(user_agent="TestBot/1", resolver=public, sleep=Sleeps(), client=client)
    assert await ok.allowed("https://agencia.es/x")  # robots.txt 404: everything allowed
    with pytest.raises(FetchError, match="not_html"):
        await ok.fetch("https://agencia.es/folleto")
    assert not await ok.allowed("https://down.es/x")  # robots.txt 5xx: nothing, for now
    with pytest.raises(FetchError, match="target_must_be_http"):
        await ok.fetch("ftp://agencia.es/x")


def test_fetcher_accepts_an_outbound_proxy() -> None:
    PageFetcher(user_agent="TestBot/1", proxy_url="http://proxy.internal:8888")
    PageFetcher(user_agent="TestBot/1", proxy_url="socks5://gluetun:1080")


async def test_searxng_client_reads_json_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["format"] == "json" and request.url.params["language"] == "es"
        assert request.headers["X-Forwarded-For"] == "127.0.0.1"
        return httpx.Response(200, json={"results": [
            {"url": "https://www.idealista.com/inmueble/1/", "title": "t"}, {"url": "ftp://x"},
            {"url": "https://www.idealista.com/inmueble/1/"}, {"url": "https://pisos.com/a"}],
            "unresponsive_engines": [["google", "CAPTCHA"]]})

    client = SearxngClient("http://searxng:8080", max_results=5,
                           client=httpx.AsyncClient(base_url="http://searxng:8080",
                                                    transport=httpx.MockTransport(handler),
                                                    headers={"X-Forwarded-For": "127.0.0.1"}))
    hits = await client.search("terreno Madrid", language="es")
    assert [h.url for h in hits] == ["https://www.idealista.com/inmueble/1/", "https://pisos.com/a"]
    broken = SearxngClient("http://searxng:8080", client=httpx.AsyncClient(
        base_url="http://searxng:8080", transport=httpx.MockTransport(lambda r: httpx.Response(429))))
    with pytest.raises(SearchError, match="http_429"):
        await broken.search("x")


# --- the campaign runner's status -------------------------------------------------------------


class FakeWeb:
    def __init__(self, status: WebStatus | None) -> None:
        self.status = status

    async def web_status(self, campaign_id: str) -> WebStatus | None:
        return self.status


async def test_status_shows_the_site_to_users_and_the_technical_line_to_owners() -> None:
    campaigns = MemoryCampaignStore()
    user_campaign = await campaign(campaigns, owner=USER)
    owner_campaign = await campaign(campaigns, owner=OWNER)
    web = FakeWeb(WebStatus(True, "fotocasa.es", "сайты: страниц 3/60 · сайт fotocasa.es"))
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, MemoryRunStore(campaigns), messenger, owner_ids={OWNER}, web=web)
    await runner.tick()
    by_chat = {chat: text for chat, _, text in messenger.sent}
    assert by_chat[USER] == "Ищу на сайте fotocasa.es…" and is_user_status(by_chat[USER])
    assert by_chat[OWNER].endswith("сайты: страниц 3/60 · сайт fotocasa.es")
    assert "fotocasa" not in by_chat[USER].replace("fotocasa.es", "")
    # the web stage keeps the campaign open: no groups, but no completion while it searches
    assert (await campaigns.get(user_campaign)).state == "running"
    assert (await campaigns.get(owner_campaign)).state == "running"

    web.status = WebStatus(True, None, "сайты: составляю запросы")
    await runner.tick()
    assert any(chat == USER and text == WEB for chat, _, text in messenger.edits)

    web.status = WebStatus(False, None, "сайты: готово")
    await runner.tick()
    await runner.tick()
    assert (await campaigns.get(user_campaign)).state == "completed"


async def test_facebook_reading_wins_the_label_and_no_web_stage_changes_nothing() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns, owner=USER)
    runner = CampaignRunner(campaigns, MemoryRunStore(campaigns), FakeMessenger(), web=FakeWeb(WebStatus(True, "pisos.com")))
    c = await campaigns.get(cid)
    assert await runner._status_text(c, "Сейчас: Facebook · Pisos Madrid · ищу дальше") == (
        "Ищу в группе Facebook «Pisos Madrid»…")
    assert await runner._status_text(c, ANALYSIS) == "Ищу на сайте pisos.com…"
    plain = CampaignRunner(campaigns, MemoryRunStore(campaigns), FakeMessenger())
    assert await plain._status_text(c, ANALYSIS) == FACEBOOK

    class Broken:
        async def web_status(self, campaign_id: str) -> WebStatus:
            raise RuntimeError("db down")

    broken = CampaignRunner(campaigns, MemoryRunStore(campaigns), FakeMessenger(), web=Broken())
    assert await broken._status_text(c, ANALYSIS) == FACEBOOK


async def test_a_hit_with_another_places_markers_is_not_queued_for_a_spanish_campaign() -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    good, bad = "https://www.example.com/inmueble/11112222/", "https://www.example.com/inmueble/33334444/"
    searcher = FakeSearcher(default=[good, bad], texts={bad: ("Casa en Valencia, Carabobo", "Bs. 40.000")})
    fetcher = FakeFetcher()
    w = worker(campaigns, store, searcher, fetcher, ListGenerator(["terreno Boadilla Madrid"]), cover_portals=False)
    await run_until_done(w, cid)
    assert fetcher.fetched == [good]
