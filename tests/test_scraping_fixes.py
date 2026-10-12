"""Phase −1 of the Idealista plan: the fixes for the «участок, Мадрид, покупка, ≥2000 м²» run (docs/idealista-integration/
SCRAPING_ANALYSIS.md): client portals only, the deal in every query, a breaker for refusing sites, a budget, the cheap
filter before the model and the model's errors in the report instead of a silent loss.
"""

from __future__ import annotations

import pytest

from bot.analysis_pipeline.models import AnalysisResult, Evidence
from bot.analysis_pipeline.pipeline import AnalysisPipeline
from bot.analysis_pipeline.prefilter import TaskContext, context_of, prefilter
from bot.campaign import MemoryCampaignStore
from bot.utils import costs
from bot.web_search.models import GeneratedQuery
from bot.web_search.store import MemoryWebStore
from bot.web_search.urls import url_key
from tests.test_web_search import (
    EVIDENCE,
    FakeFetcher,
    FakeSearcher,
    ListGenerator,
    campaign,
    madrid_task,
    run_until_done,
    worker,
)

JUNK = {  # what the Madrid land run met: dictionaries and science sites whose hits pass «property word + deal word»
    "https://dictionary.cambridge.org/dictionary/spanish-english/parcela":
        ("PARCELA | translate Spanish to English - Cambridge Dictionary", "parcela: plot. Se vende parcela de 2000 m2."),
    "https://www.ingles.com/traductor/terreno%20en%20venta": ("Terreno en venta | Traductor - inglés.com", "terreno en venta"),
    "https://www.mdpi.com/2073-445X/10/5/500": ("Urban land in Madrid | MDPI", "parcels larger than 2,000 m2 for sale"),
    "https://www.fao.org/3/x1234e/x1234e.htm": ("Land tenure - FAO", "plot sizes of 2000 m2 per household"),
}
PORTAL = "https://www.idealista.com/inmueble/98765432/"


@pytest.fixture
def ledger():
    sink = costs.MemoryLedger()
    costs.install(sink, budget_usd=1.0)
    try:
        yield sink
    finally:
        costs.install(None)


async def _queued(hits: dict[str, tuple[str, str]], **config) -> set[str]:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    w = worker(campaigns, store, FakeSearcher(default=list(hits), texts=hits), FakeFetcher(),
               ListGenerator(["terreno venta Boadilla Madrid"]), cover_portals=False, **config)
    await w.step(cid)
    await w.step(cid)
    return {u.url for u in store.urls.get(cid, {}).values()}


async def test_dictionaries_and_science_sites_never_reach_the_queue() -> None:
    hits = {**JUNK, PORTAL: EVIDENCE}
    assert await _queued(hits) == {PORTAL}                       # strict: only the client's portals
    assert await _queued(hits, domain_policy="soft") == {PORTAL}  # and the hosts are on the non-listing list anyway
    agency = "https://www.inmobiliaria-sol.es/venta/terreno-boadilla-1234567"
    figures = {agency: ("Terreno en venta en Boadilla", "Parcela 2.300 m² · 390.000 €")}
    words_only = {"https://otro-sitio.es/terrenos-venta-madrid": ("Terreno en venta en Madrid", "Los mejores terrenos")}
    assert await _queued({**figures, **words_only}) == set()
    assert await _queued({**figures, **words_only}, domain_policy="soft") == {agency}  # soft needs a price or an area
    assert await _queued({**figures, **words_only}, domain_policy="off") == {agency, *words_only}


async def test_a_rent_title_or_path_is_dropped_for_a_sale_campaign_on_any_site() -> None:
    rent_title = "https://www.idealista.com/inmueble/11112222/"
    rent_path = "https://www.inmobiliaria-sol.es/arrendamiento/parcela-madrid-1234567"
    hits = {rent_title: ("Terreno en alquiler en Madrid", "Parcela 2.000 m² · 900 €/mes"), PORTAL: EVIDENCE,
            rent_path: ("Parcela en Madrid", "2.000 m² · 900 €")}
    assert await _queued(hits, domain_policy="soft") == {PORTAL}


def test_every_query_of_a_sale_campaign_names_the_deal() -> None:
    from bot.web_search.queries import localise

    task = madrid_task()  # «участок … покупка»
    out = [q.text for q in localise([GeneratedQuery("parcela 2000 m2 Madrid", "es"), GeneratedQuery("comprar terreno Madrid", "es"),
                                     GeneratedQuery("site:idealista.com terreno Madrid", "es"),
                                     GeneratedQuery("building plot Madrid", "en"), GeneratedQuery("участок Madrid", "ru")], task)]
    assert out == ["parcela 2000 m2 Madrid en venta España", "comprar terreno Madrid España",
                   "site:idealista.com terreno en venta Madrid España", "building plot Madrid for sale Spain",
                   "участок Madrid купить Испания"]


async def test_a_refusing_site_trips_the_breaker_and_spends_no_more_paid_reads(ledger) -> None:
    from bot.web_search.fetcher import FetchError

    class Unlocker:  # a paid scrape API that is refused as well (a DataDome site)
        calls = 0

        async def fetch(self, url):
            Unlocker.calls += 1
            raise FetchError("scrape_http_403")

    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    urls = [f"https://www.idealista.com/inmueble/{60000000 + n}/" for n in range(8)]
    fetcher = FakeFetcher(errors={u: "http_403" for u in urls})
    texts = {u: ("Terreno en venta en Boadilla - idealista", "Parcela de 2.400 m² en Boadilla. 480.000 €.") for u in urls}
    w = worker(campaigns, store, FakeSearcher(default=urls, texts=texts), fetcher, ListGenerator(["terreno venta Boadilla"]),
               pages_per_tick=1, cover_portals=False, scrape_cost_usd=0.01)
    w.scraper = Unlocker()
    await run_until_done(w, cid)
    assert Unlocker.calls == 3 and len(fetcher.fetched) == 3  # three refused pages, then nothing more is asked
    assert sum(r.detail == "host_breaker" for r in store.urls[cid].values()) == 0  # every URL kept its card instead
    assert {p["url"] for p in store.posts} == set(urls)  # the search-result cards of all eight stay
    assert (await ledger.summary(cid)).by_stage == {"scrape": pytest.approx(0.03)}


async def test_a_spent_budget_stops_the_web_stage(ledger) -> None:
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    await costs.record("llm", cost_usd=1.5, campaign_id=cid)
    w = worker(campaigns, store, FakeSearcher(default=[PORTAL]), FakeFetcher(), ListGenerator(["terreno venta Boadilla"]))
    await run_until_done(w, cid)
    assert store.runs[cid].stop_reason == "budget_cap" and store.posts == []


# --- the analysis worker -----------------------------------------------------------------------------------------------

LAND = TaskContext("c1", deal="sale", property_type="land", min_area=2000)


def _evidence(title: str, text: str = "", url: str = "https://www.pisos.com/terreno-madrid-123456789/") -> Evidence:
    return Evidence(post_id="p1", source_id="s1", canonical_url=url, title=title, text=text or title + ". " + "x" * 80)


@pytest.mark.parametrize(("title", "text", "url", "reason"), [
    ("Terreno en venta en Madrid, parcela de 800 m²", "", None, "area"),
    ("Terreno en venta en Madrid, parcela de 1.600 m²", "", None, None),          # 80 %: similar, the model decides
    ("Terreno en venta en Madrid, 2,5 ha", "", None, None),
    ("Chalet de 300 m² en Pozuelo", "", None, None),                               # a built area says nothing of the plot
    ("Terreno en alquiler en Getafe, 3.000 m²", "", None, "deal"),
    ("Parcela en Getafe", "", "https://www.pisos.com/alquilar/terreno-getafe-123456789/", "deal"),
    ("Parcela", 'JSON-LD: {"deal": "rent", "area_m2": 5000}\nParcela en Getafe', None, "deal"),
    ("Parcela", 'JSON-LD: {"plot_m2": 900, "area_m2": 150}\nChalet con parcela', None, "area"),
    ("Venta y alquiler de terrenos en Madrid", "", "https://www.agencia.es/terrenos-madrid", None),
])
def test_the_cheap_prefilter(title: str, text: str, url: str | None, reason: str | None) -> None:
    evidence = _evidence(title, text, url) if url else _evidence(title, text)
    assert prefilter(evidence, LAND) == reason
    assert prefilter(evidence, None) is None


def test_the_task_context_from_a_campaign() -> None:
    ctx = context_of("c1", {"deal": "sale", "property_type": "land", "min_area": 2000}, {"deviations": {"area_pct": 20}})
    assert ctx == TaskContext("c1", "sale", "land", 2000, 20)
    assert context_of("c2", None, {"deal": "rent", "area_m2": {"min": "500"}}).min_area == 500
    loose = TaskContext("c1", min_area=2000, area_pct=40)  # an approved -40 %: the floor drops to 45 %
    assert prefilter(_evidence("Terreno en venta, parcela de 1.000 m²"), loose) is None


class _Store:
    def __init__(self, context, n=1):
        self.context, self.n = context, n
        self.finalized, self.released, self.saved = [], [], []

    async def pending(self, n, claim_seconds):
        return [{"id": f"0000000{i}-1111-1111-1111-111111111111", "source_id": "s",
                 "canonical_url": "https://www.pisos.com/comprar/terreno-123456789/",
                 "body_text": "Terreno en venta en Madrid, parcela urbanizable con todos los servicios, buena ubicación.",
                 "title": "Terreno en venta en Madrid", "published_at": None, "comments": "[]", "vertical": "real_estate",
                 "analysis_claim_token": "tok"} for i in range(self.n)]

    async def task_context(self, post_id):
        return self.context

    async def task_hint(self, post_id):
        return None

    async def save(self, e, vertical, result, model, token):
        self.saved.append(e.post_id)
        return "fid" if result.accepted else None

    async def finalize(self, post_id, token, *, accepted):
        self.finalized.append(accepted)
        return True

    async def release(self, post_id, token):
        self.released.append(post_id)


class _Analyzer:
    def __init__(self, fail: Exception | None = None):
        self.fail, self.calls = fail, 0

    async def analyze(self, e, vertical, task_hint=None):
        self.calls += 1
        if self.fail:
            raise self.fail
        return AnalysisResult(relevant=True, confidence=0.9, summary="s", location="Madrid", price_signals=[],
                              related_links=[], category=vertical, reason="r")


async def test_a_prefiltered_post_costs_no_model_call_and_is_counted(ledger) -> None:
    from bot.analysis_pipeline.main import analyse_batch

    store, model = _Store(TaskContext("c1", deal="rent")), _Analyzer()
    await analyse_batch(store, AnalysisPipeline(model), batch_size=5, claim_seconds=60, model="m")
    assert model.calls == 0 and store.finalized == [False]
    assert (await ledger.summary("c1")).skips == {"llm:prefilter_deal": 1}


async def test_a_campaign_over_budget_gets_no_more_analysis(ledger) -> None:
    from bot.analysis_pipeline.main import analyse_batch

    await costs.record("llm", cost_usd=2, campaign_id="c1")
    store, model = _Store(TaskContext("c1")), _Analyzer()
    await analyse_batch(store, AnalysisPipeline(model), batch_size=5, claim_seconds=60, model="m")
    assert model.calls == 0 and store.finalized == [False]
    assert (await ledger.summary("c1")).skips == {"llm:budget_cap": 1}


async def test_an_unusable_model_answer_is_booked_not_lost(ledger) -> None:
    from bot.analysis_pipeline.main import analyse_batch

    store = _Store(TaskContext("c1"))
    await analyse_batch(store, AnalysisPipeline(_Analyzer(ValueError("model_request_refused_400"))), batch_size=5,
                        claim_seconds=60, model="m")
    assert store.finalized == [False]
    assert (await ledger.summary("c1")).errors == {"llm:model_request_refused_400": 1}


async def test_a_dead_key_stops_the_batch_and_hands_every_post_back(ledger) -> None:
    import httpx

    from bot.analysis_pipeline.main import analyse_batch

    refused = httpx.HTTPStatusError("x", request=httpx.Request("POST", "https://x"), response=httpx.Response(401))
    store, model = _Store(TaskContext("c1"), n=3), _Analyzer(refused)
    await analyse_batch(store, AnalysisPipeline(model), batch_size=5, claim_seconds=60, model="m")
    assert model.calls == 1 and len(store.released) == 3 and store.finalized == []  # one try, not three
    assert (await ledger.summary("c1")).errors == {"llm:http_401": 1}


def test_the_url_identity_folds_tracking_and_scheme() -> None:
    assert url_key(PORTAL) == url_key(PORTAL + "?utm_source=bing") == url_key("http://idealista.com/inmueble/98765432")


async def test_a_country_without_a_portal_list_is_searched_softly() -> None:
    from bot.campaign import plan_campaign

    campaigns = MemoryCampaignStore()
    plan = plan_campaign("квартира в Лиссабоне до 300000 евро, покупка", vertical="real_estate", location="Lisboa")
    cid = await campaigns.create(plan, chat_id=1, requested_by=1, source_text="квартира в Лиссабоне", actor="test",
                                 spec={"sources": {"required": ["idealista.pt"]}})  # one named site: not the only one
    await campaigns.set_state(cid, "running", "test")
    store = MemoryWebStore(campaigns)
    agency = "https://www.imobiliaria-lx.pt/venda/apartamento-lisboa-1234567"
    hits = {agency: ("Apartamento à venda em Lisboa", "T2 85 m² 290.000 €")}
    w = worker(campaigns, store, FakeSearcher(default=list(hits), texts=hits), FakeFetcher(),
               ListGenerator(["apartamento venda Lisboa"]), cover_portals=False)
    await w.step(cid)
    await w.step(cid)
    assert {u.url for u in store.urls.get(cid, {}).values()} == {agency}
