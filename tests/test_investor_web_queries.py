"""v3 Q51: INVESTOR_WEB_QUERIES -- the web stage of an investors campaign searches investors, not listing portals."""

from __future__ import annotations

import json
from typing import Any

import httpx

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.search_plan import SearchPlan
from bot.campaign.spec import TaskSpec
from bot.web_search.models import GeneratedQuery
from bot.web_search.queries import (
    INVESTOR_SYSTEM,
    SYSTEM,
    FallbackQueryGenerator,
    OpenRouterQueryGenerator,
    QueryTask,
    TemplateQueryGenerator,
    listing_portal_host,
    without_portals,
)
from bot.web_search.settings import WebSearchSettings
from bot.web_search.store import MemoryWebStore
from bot.web_search.worker import WebSearchConfig, WebSearchWorker, query_task
from tests.test_web_search import FakeFetcher, FakeSearcher, run_until_done

GOAL = "соинвесторы в Малаге, реновация квартир под перепродажу, 300 тыс €"
FLAT_GOAL = "квартира 2 комнаты в Малаге до 200000 евро, покупка"
PORTAL_URL = "https://www.idealista.com/venta-viviendas/malaga-malaga/"
PLACE = {"en": "Malaga", "es": "Málaga", "ru": "Малага", "country": "ES"}
INVESTOR_HIT = "https://www.inversores-malaga.example/club"
PORTAL_HIT = "https://www.fotocasa.es/es/comprar/viviendas/malaga-capital/todas-las-zonas/l"
AGENCY_HIT = "https://www.lucasfox.com/venta/malaga.html"


def listing_plan() -> dict[str, Any]:
    """What the planner wrote for the b4-g7-s5 investors campaign: flats to renovate on the portals."""
    return {
        "sites": [{"host": "idealista.com", "priority": 1, "why": "leader"},
                  {"host": "fotocasa.es", "priority": 1, "why": "second"}],
        "queries": [{"text": "piso para reformar en venta Málaga España", "language": "es", "site": None},
                    {"text": "piso para reformar venta Málaga", "language": "es", "site": "fotocasa.es"}],
        "portal_urls": [{"host": "idealista.com", "url": PORTAL_URL, "why": "flats for sale"}],
        "stop": {"min_exact": 15, "max_pages": 60}, "notes": "flats",
    }


def task_spec(mode: str) -> TaskSpec:
    data: dict[str, Any] = {"mode": mode, "place": {"name": "Malaga", "country": "ES", "level": "city"}}
    if mode != "investors":
        data |= {"deal": "sale", "property_type": "apartment", "budget": {"max": 200000, "currency": "EUR"},
                 "rooms": {"min": 2}}
    return TaskSpec.model_validate(data)


class FixedPlanner:
    model = "fixed"

    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, spec, plan, *, source_text=""):
        self.calls += 1
        return SearchPlan.model_validate(listing_plan())


async def make_campaign(campaigns: MemoryCampaignStore, mode: str) -> str:
    goal = GOAL if mode == "investors" else FLAT_GOAL
    spec = task_spec(mode)
    plan = plan_campaign(goal, vertical=mode, place=PLACE, spec=spec)  # type: ignore[arg-type]
    cid = await campaigns.create(plan, chat_id=1, requested_by=1, source_text=goal, actor="test",
                                 spec=spec.model_dump(mode="json"))
    await campaigns.set_state(cid, "running", "test")
    return cid


async def run(mode: str, *, flag: bool) -> tuple[MemoryCampaignStore, MemoryWebStore, FakeSearcher, FixedPlanner, str]:
    campaigns, store = MemoryCampaignStore(), MemoryWebStore(None)
    store.campaigns = campaigns
    cid = await make_campaign(campaigns, mode)
    planner, searcher = FixedPlanner(), FakeSearcher(default=[INVESTOR_HIT, PORTAL_HIT, AGENCY_HIT])
    w = WebSearchWorker(campaigns, store, searcher, FakeFetcher(), FallbackQueryGenerator(None), planner=planner,
                        config=WebSearchConfig(cover_portals=False, queries_per_round=6, max_queries_per_campaign=6,
                                               domain_policy="off", investor_web_queries=flag))
    await run_until_done(w, cid)
    return campaigns, store, searcher, planner, cid


def portal_query(text: str) -> bool:
    """True when the investor round drops ``text`` as aimed at a listing portal."""
    return not without_portals([GeneratedQuery(text, "es")], _investor_task())


def _investor_task(**changes: Any) -> QueryTask:
    return QueryTask(goal="g", task_text=GOAL, location="Málaga", location_aliases={"es": "Málaga"},
                     vertical="investors", country_code="ES", **{"investor_queries": True, **changes})


# --- flag on, investors ----------------------------------------------------------------------------


async def test_flag_on_investors_search_investors_and_never_the_portals() -> None:
    campaigns, store, _searcher, planner, cid = await run("investors", flag=True)
    assert planner.calls == 0  # no listing plan is asked for (no LLM call, no money)
    assert (await campaigns.get(cid)).plan.search_plan is None
    texts = [q.text for q in store.queries[cid]]
    assert texts and all("site:" not in t and not portal_query(t) for t in texts)
    assert not any("reformar" in t or "venta" in t for t in texts)
    joined = " ".join(texts).lower()
    assert "club de inversores" in joined and "family office" in joined
    hosts = {r.host for r in store.urls[cid].values()}
    assert PORTAL_URL not in {r.url for r in store.urls[cid].values()}
    assert hosts == {"inversores-malaga.example"}  # fotocasa and lucasfox hits are dropped


async def test_flag_on_ignores_a_listing_plan_already_stored() -> None:
    campaigns = MemoryCampaignStore()
    cid = await make_campaign(campaigns, "investors")
    await campaigns.set_search_plan(cid, listing_plan())
    campaign = await campaigns.get(cid)
    assert query_task(campaign).search_plan is not None
    task = query_task(campaign, investor_queries=True)
    assert task.search_plan is None and task.investor_web and task.portals() == ()


async def test_flag_on_model_round_uses_the_investor_prompt() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        content = json.dumps({"queries": [{"text": "business angels inmobiliario Málaga", "language": "es"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    generator = OpenRouterQueryGenerator(api_key="k", model="m", timeout_seconds=5,
                                         client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    on, off = _investor_task(), _investor_task(investor_queries=False)
    await generator.generate(on, used=[], count=1)
    await generator.generate(off, used=[], count=1)
    assert seen[0]["messages"][0]["content"] == INVESTOR_SYSTEM
    assert seen[1]["messages"][0]["content"] == SYSTEM


def test_portal_queries_and_hosts_are_recognised() -> None:
    task = _investor_task()
    queries = [GeneratedQuery("site:fotocasa.es inversión Málaga", "es"),
               GeneratedQuery("inversores idealista Málaga", "es"),
               GeneratedQuery("thinkspain.com investors Malaga", "en"),
               GeneratedQuery("club de inversores Málaga", "es")]
    assert [q.text for q in without_portals(queries, task)] == ["club de inversores Málaga"]
    assert listing_portal_host("www.lucasfox.com") and listing_portal_host("lucasfox.es")
    assert listing_portal_host("idealista.com") and listing_portal_host("news.idealista.com")
    assert listing_portal_host("www.fotocasa.es") and listing_portal_host("solvia.es")
    for host in ("urbanitae.com", "altamira-capital.es", "solvia-inversiones.es", "tucasa-invest.es",
                 "colegio-altamira.es", "notidealista.com"):
        assert not listing_portal_host(host), host
    places = [GeneratedQuery("inversores inmobiliarios Altamira Almería", "es"),
              GeneratedQuery("Solvia inversores Málaga", "es"),
              GeneratedQuery("site:solvia.es inversión Málaga", "es"),
              GeneratedQuery("site:altamirainmuebles.com Málaga", "es")]
    assert [q.text for q in without_portals(places, task)] == [q.text for q in places[:2]]
    templates = TemplateQueryGenerator().candidates(task)
    assert templates and all("site:" not in q.text for q in templates)


# --- flag off: as before; real_estate and both: unchanged --------------------------------------------


async def test_flag_off_investors_keep_the_listing_plan_and_the_portals() -> None:
    campaigns, store, _searcher, planner, cid = await run("investors", flag=False)
    assert planner.calls == 1
    assert (await campaigns.get(cid)).plan.search_plan is not None
    assert PORTAL_URL in {r.url for r in store.urls[cid].values()}
    assert store.queries[cid][0].text.startswith("piso para reformar")
    assert "fotocasa.es" in {r.host for r in store.urls[cid].values()}


async def test_real_estate_and_both_do_not_change_with_the_flag() -> None:
    for mode in ("real_estate", "both"):
        _, off_store, off_search, off_planner, off_cid = await run(mode, flag=False)
        _, on_store, on_search, on_planner, on_cid = await run(mode, flag=True)
        assert off_planner.calls == on_planner.calls == 1
        assert [q.text for q in off_store.queries[off_cid]] == [q.text for q in on_store.queries[on_cid]]
        assert off_search.calls == on_search.calls
        assert ({r.url for r in off_store.urls[off_cid].values()} == {r.url for r in on_store.urls[on_cid].values()})
        assert PORTAL_URL in {r.url for r in on_store.urls[on_cid].values()}


def test_setting_reaches_the_config(monkeypatch) -> None:
    assert WebSearchSettings(_env_file=None).config().investor_web_queries is False
    monkeypatch.setenv("INVESTOR_WEB_QUERIES", "true")
    assert WebSearchSettings(_env_file=None).config().investor_web_queries is True


class PortalModel:
    """A model that answers an investor round with portal queries only."""

    model = "portal-model"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, task, *, used, count):
        self.calls += 1
        return [GeneratedQuery("site:idealista.com inversión Málaga", "es"),
                GeneratedQuery("fotocasa pisos inversión Málaga", "es"),
                GeneratedQuery("site:www.thinkspain.com investors Malaga", "en")][:count]


async def test_portal_only_model_round_is_filled_by_templates_before_the_round_ends() -> None:
    campaigns, store = MemoryCampaignStore(), MemoryWebStore(None)
    store.campaigns = campaigns
    cid = await make_campaign(campaigns, "investors")
    model = PortalModel()
    w = WebSearchWorker(campaigns, store, FakeSearcher(), FakeFetcher(), FallbackQueryGenerator(model),
                        planner=FixedPlanner(),
                        config=WebSearchConfig(cover_portals=False, queries_per_round=4, max_queries_per_campaign=8,
                                               domain_policy="off", investor_web_queries=True))
    await w.tick()  # the first round
    run_ = await store.get_run(cid)
    assert run_ is not None and run_.state == "searching"  # not «queries_exhausted» on the first round
    texts = [q.text for q in store.queries[cid]]
    assert model.calls == 1 and len(texts) == 4
    assert all(not portal_query(t) and "site:" not in t for t in texts)
    assert "club de inversores" in " ".join(texts).lower()
    generated = await FallbackQueryGenerator(model).generate(_investor_task(), used=[], count=3)
    assert len(generated) == 3 and not any(portal_query(q.text) for q in generated)
