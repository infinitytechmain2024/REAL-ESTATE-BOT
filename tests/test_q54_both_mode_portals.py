"""v3 Q54: BOTH_MODE_PORTALS -- the object part of a ``both`` campaign reaches the Apify portals (mocks only).

Bench run b4-g9-s5 (persona G9, plot in Madrid province plus investors): no portal source ran, because
``_PortalSource.supports`` takes only ``vertical == "real_estate"``; the web stage read 177 portal pages instead.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.spec import TaskSpec
from bot.utils import costs
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources import SourceListing
from bot.web_search.sources.apify import (
    SourceRunContext,
    TemporaryApifyError,
    _execution,
    source_run_scope,
)
from bot.web_search.sources.portals import (
    ApifyFotocasaSource,
    ApifyHabitacliaSource,
    ApifyIdealistaPortalSource,
    ApifyMilanunciosSource,
)
from bot.web_search.store import MemoryWebStore
from bot.web_search.worker import WebSearchConfig, WebSearchWorker, object_part, query_task
from tests.test_web_search import FakeFetcher, FakeSearcher, ListGenerator, run_until_done

TEXT = "покупка Тип: участок. Задача: участок под застройку в Мадриде от 2000 м² и инвесторы под проект"
PORTALS = (ApifyFotocasaSource, ApifyIdealistaPortalSource, ApifyHabitacliaSource, ApifyMilanunciosSource)


@pytest.fixture
def ledger():
    previous, budget = costs.ledger(), costs.budget()
    costs.install(costs.MemoryLedger(), 1)
    try:
        yield
    finally:
        costs.install(previous, budget)


async def _campaign(mode: str, campaigns: MemoryCampaignStore | None = None):
    campaigns = campaigns or MemoryCampaignStore()
    body = {"mode": mode, "place": {"name": "Madrid", "country": "ES", "level": "province"}}
    if mode != "investors":
        body |= {"deal": "sale", "property_type": "land", "area_m2": {"min": 2000}}
    spec = TaskSpec.model_validate(body)
    plan = plan_campaign(TEXT, vertical=mode, spec=spec)  # type: ignore[arg-type]
    cid = await campaigns.create(plan, chat_id=1, requested_by=1, source_text=TEXT, actor="test",
                                 spec=spec.model_dump(mode="json"))
    await campaigns.set_state(cid, "running", "test")
    return await campaigns.get(cid)


def _worker(campaigns=None, store=None, sources=(), searcher=None, **config) -> WebSearchWorker:
    campaigns = campaigns or MemoryCampaignStore()
    config.setdefault("cover_portals", False)
    return WebSearchWorker(campaigns, store or MemoryWebStore(campaigns), searcher or FakeSearcher(), FakeFetcher(),
                           ListGenerator(["inversores inmobiliarios Madrid"]), sources=sources,
                           config=WebSearchConfig(**config))


def _api(requests):
    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run1", "status": "SUCCEEDED",
                                                      "defaultDatasetId": "data1", "usageTotalUsd": .01}})
        if "actor-runs" in request.url.path:
            return httpx.Response(200, json={"data": {"usageTotalUsd": .01}})
        return httpx.Response(200, json=[])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _actor_input(source_class, task) -> dict:
    requests: list[httpx.Request] = []
    adapter = source_class("secret-token", client=_api(requests), sleep=AsyncMock())
    assert adapter.supports(task)
    context = SourceRunContext(campaign_id="c", source_name=adapter.name, run_id=None, dataset_id=None,
                               max_charge_usd=.05, launch_allowed=True, save_run=AsyncMock(),
                               reconcile_usage=AsyncMock())
    with source_run_scope(context):
        await adapter.search(task, limit=5)
    await adapter.aclose()
    return json.loads(next(r for r in requests if r.method == "POST").content)


def test_flag_is_off_by_default_and_reaches_the_worker_config(monkeypatch):
    monkeypatch.delenv("BOTH_MODE_PORTALS", raising=False)
    assert WebSearchSettings(_env_file=None).config().both_mode_portals is False
    monkeypatch.setenv("BOTH_MODE_PORTALS", "true")
    assert WebSearchSettings(_env_file=None).config().both_mode_portals is True


async def test_flag_on_both_task_reaches_the_portals_as_real_estate():
    both, real_estate = await _campaign("both"), await _campaign("real_estate")
    assert both.plan.vertical == "both"
    w = _worker(both_mode_portals=True, place_level_from_spec=True)
    for source_class in PORTALS:
        source = source_class("secret-token")
        task = w._source_task(both, source)
        assert task.vertical == "real_estate" and source.supports(task), source_class.__name__
        same = w._source_task(real_estate, source)
        assert (task.constraints, task.location, task.place_level, task.country) == (
            same.constraints, same.location, same.place_level, same.country)
        assert await _actor_input(source_class, task) == await _actor_input(source_class, same)
        await source.aclose()


async def test_flag_off_both_task_is_as_before():
    both = await _campaign("both")
    w = _worker(place_level_from_spec=True)
    for source_class in PORTALS:
        source = source_class("secret-token")
        task = w._source_task(both, source)
        assert task.vertical == "both" and not source.supports(task)
        await source.aclose()


async def test_investors_never_reach_the_portals():
    investors = await _campaign("investors")
    w = _worker(both_mode_portals=True, place_level_from_spec=True)
    for source_class in PORTALS:
        source = source_class("secret-token")
        task = w._source_task(investors, source)
        assert task.vertical == "investors" and not source.supports(task)
        await source.aclose()
    assert object_part(query_task(investors)).vertical == "investors"


class RealEstateSource:
    """A portal that, like ``_PortalSource``, takes only a real_estate task."""

    name = "apify_fotocasa"
    hosts = frozenset({"fotocasa.es"})
    max_items = 20
    max_charge_usd = .10
    actor_id = "fixture"

    def __init__(self, rows: int = 5, *, fail: bool = False):
        self.rows, self.fail, self.launches = rows, fail, 0

    def supports(self, task):
        return task.vertical == "real_estate"

    async def search(self, task, *, limit):
        context = _execution.get()
        if context.launch_allowed:
            self.launches += 1
            await context.save_run("run1", "data1")
        if self.fail:
            raise TemporaryApifyError("apify_http_503")
        await context.reconcile_usage("run1", .01)
        return [SourceListing(f"https://www.fotocasa.es/es/comprar/terreno/madrid/{183456700 + n}/d", "Terreno",
                              plot_m2=2200, deal="sale", property_type="land") for n in range(min(self.rows, limit))]

    async def aclose(self):
        pass


async def test_apify_mode_both_runs_portals_and_keeps_searching_for_investors(ledger):
    campaigns = MemoryCampaignStore()
    both = await _campaign("both", campaigns)
    store, source = MemoryWebStore(campaigns), RealEstateSource(rows=5)
    w = _worker(campaigns, store, (source,), both_mode_portals=True, acquisition_mode="apify", apify_min_listings=5)
    await run_until_done(w, both.id)
    assert source.launches == 1
    assert store.runs[both.id].stop_reason != "apify_enough"  # the investor part still searches
    assert w.searcher.calls


async def test_apify_mode_both_flag_off_never_launches(ledger):
    campaigns = MemoryCampaignStore()
    both = await _campaign("both", campaigns)
    store, source = MemoryWebStore(campaigns), RealEstateSource(rows=5)
    w = _worker(campaigns, store, (source,), acquisition_mode="apify", apify_min_listings=5)
    await run_until_done(w, both.id)
    assert source.launches == 0 and not await store.source_runs(both.id) and w.searcher.calls


async def test_apify_mode_real_estate_gate_is_unchanged_by_the_flag(ledger):
    campaigns = MemoryCampaignStore()
    real_estate = await _campaign("real_estate", campaigns)
    store, source = MemoryWebStore(campaigns), RealEstateSource(rows=5)
    w = _worker(campaigns, store, (source,), both_mode_portals=True, acquisition_mode="apify", apify_min_listings=5)
    await run_until_done(w, real_estate.id)
    assert source.launches == 1 and store.runs[real_estate.id].stop_reason == "apify_enough"
    assert not w.searcher.calls


PORTAL_INDEX = "https://www.fotocasa.es/es/comprar/terrenos/madrid-provincia/todas-las-zonas/l"
PORTAL_HIT = "https://www.fotocasa.es/es/comprar/terreno/madrid/190000001/d"
INVESTOR_HIT = "https://www.clubinversores-madrid.es/quienes-somos/"


class RecordingStore(MemoryWebStore):
    """Every candidate queued and the vertical of every fetch begun, by layer."""

    def __init__(self, campaigns):
        super().__init__(campaigns)
        self.enqueued, self.verticals = [], []

    async def enqueue(self, campaign_id, candidates, **kw):
        self.enqueued = [*self.enqueued, *candidates]
        return await super().enqueue(campaign_id, candidates, **kw)

    async def begin_fetch(self, campaign_id, url, **kw):
        self.verticals.append((kw.get("layer", "http"), kw["vertical"]))
        return await super().begin_fetch(campaign_id, url, **kw)


async def _both_run(source, *, flag=True):
    campaigns = MemoryCampaignStore()
    both = await _campaign("both", campaigns)
    await campaigns.set_search_plan(both.id, {"portal_urls": [
        {"host": "fotocasa.es", "url": PORTAL_INDEX, "why": "plots"}]})
    store = RecordingStore(campaigns)
    w = _worker(campaigns, store, (source,), searcher=FakeSearcher(default=[PORTAL_HIT, INVESTOR_HIT]),
                cover_portals=True, both_mode_portals=flag, acquisition_mode="apify", apify_min_listings=5)
    await run_until_done(w, both.id)
    queued = {c.url for c in store.enqueued}
    site_queries = [q for q, _ in w.searcher.calls if "site:" in q]
    return both.id, store, queued, site_queries


async def test_portals_done_both_imports_objects_once_and_skips_portal_pages(ledger):
    source = RealEstateSource(rows=5)
    cid, store, queued, site_queries = await _both_run(source)
    assert source.launches == 1 and store.runs[cid].stop_reason != "apify_enough"
    api = [vertical for layer, vertical in store.verticals if layer == "api"]
    assert len(api) == 5 and set(api) == {"real_estate"}  # an object, never split into object and investor
    imported = [p for p in store.posts if "fotocasa.es" in p["url"]]
    assert len(imported) == 5  # each row read once
    assert PORTAL_INDEX not in queued  # no direct portal page (``_enqueue_portal_urls`` skipped)
    assert not site_queries  # no ``site:`` fill (``cover_portals and not portals_done``)
    assert PORTAL_HIT not in queued and INVESTOR_HIT in queued  # the search is for investors
    spent = [e for e in costs.ledger().entries if e.campaign_id == cid and e.provider == "apify" and e.kind == "cost"]
    assert spent and spent[0].cost_usd == .01  # campaign_costs


@pytest.mark.parametrize("source", [RealEstateSource(rows=0), RealEstateSource(rows=5, fail=True)],
                         ids=["too_few", "failed"])
async def test_portals_not_done_both_falls_back_to_portal_pages_and_search(ledger, source):
    cid, store, queued, site_queries = await _both_run(source)
    assert source.launches == 1 and store.runs[cid].stop_reason != "apify_enough"
    if source.fail:
        assert (await store.source_runs(cid))[0].state == "failed"
    assert PORTAL_INDEX in queued and site_queries and PORTAL_HIT in queued and INVESTOR_HIT in queued


async def test_flag_off_both_reads_portal_pages_as_before(ledger):
    source = RealEstateSource(rows=5)
    _, store, queued, site_queries = await _both_run(source, flag=False)
    assert source.launches == 0 and not [v for layer, v in store.verticals if layer == "api"]
    assert PORTAL_INDEX in queued and site_queries and PORTAL_HIT in queued
