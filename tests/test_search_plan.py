"""The LLM-written SearchPlan: validation, the planner (fake HTTP), and its consumption by the web stage."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.architect import plan_with_model
from bot.campaign.search_plan import (
    MAX_QUERIES,
    OpenRouterSearchPlanner,
    PlanQuery,
    PortalUrl,
    SearchPlan,
    SiteHint,
    blocked_hosts_of,
    known_hosts,
    parse_plan,
    plan_languages,
    validate_plan,
)
from bot.campaign.spec import TaskSpec
from bot.web_search.queries import FallbackQueryGenerator, QueryTask, planned_round
from bot.web_search.store import MemoryWebStore
from bot.web_search.worker import WebSearchConfig, WebSearchWorker, query_task
from tests.test_web_search import FakeFetcher, FakeSearcher, ListGenerator, run_until_done

GOAL = "квартира 2 комнаты в Валенсии до 200000 евро, покупка"
IDEALISTA_URL = "https://www.idealista.com/venta-viviendas/valencia-valencia/con-precio-hasta_200000,de-dos-dormitorios/"
FOTOCASA_URL = "https://www.fotocasa.es/es/comprar/viviendas/valencia-capital/todas-las-zonas/l?maxPrice=200000&minRooms=2"


def spec(**sources: list[str]) -> TaskSpec:
    return TaskSpec.model_validate({
        "mode": "real_estate", "place": {"name": "Valencia", "country": "ES", "districts": ["Ruzafa"]},
        "deal": "sale", "property_type": "apartment", "budget": {"max": 200000, "currency": "EUR"},
        "rooms": {"min": 2}, "sources": sources})


def campaign_plan(task_spec: TaskSpec):
    return plan_campaign(GOAL, vertical="real_estate", place={"en": "Valencia", "ru": "Валенсия", "country": "ES"},
                         spec=task_spec)


def good_plan() -> dict[str, Any]:
    return {
        "sites": [{"host": "idealista.com", "priority": 1, "why": "leader"},
                  {"host": "fotocasa.es", "priority": 2, "why": "second"}],
        "queries": [
            {"text": "piso en venta Valencia España hasta 200000 2 habitaciones", "language": "es", "site": None},
            {"text": "piso venta Ruzafa Valencia 2 habitaciones", "language": "es", "site": "idealista.com"},
            {"text": "2 bedroom flat for sale Valencia Spain under 200000", "language": "en", "site": None},
        ],
        "portal_urls": [{"host": "idealista.com", "url": IDEALISTA_URL, "why": "filters"},
                        {"host": "fotocasa.es", "url": FOTOCASA_URL, "why": "filters"}],
        "stop": {"min_exact": 15, "max_pages": 80}, "notes": "ok",
    }


def planner(handler) -> OpenRouterSearchPlanner:
    return OpenRouterSearchPlanner(api_key="k", model="m", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def answer(content: Any, status: int = 200) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(status, json={"choices": [{"message": {"content": text}}]})


# --- validation ----------------------------------------------------------------------------------


def test_unknown_hosts_are_dropped_known_and_required_sources_kept() -> None:
    task_spec = spec(required=["https://www.spainhouses.net/x", "idealista"], blocked=["fotocasa"])
    known = known_hosts(task_spec, "ES")
    raw = SearchPlan(
        sites=[SiteHint(host="idealista.com"), SiteHint(host="made-up-portal.com"), SiteHint(host="fotocasa.es"),
               SiteHint(host="www.Idealista.com", priority=3)],
        queries=[PlanQuery(text="piso Valencia", language="es", site="made-up-portal.com"),
                 PlanQuery(text="site:evil.example piso Valencia", language="es"),
                 PlanQuery(text="piso venta Valencia", language="es", site="idealista.com")],
        portal_urls=[PortalUrl(host="idealista.com", url=IDEALISTA_URL),
                     PortalUrl(host="idealista.com", url="http://www.idealista.com/venta-viviendas/x/"),  # not https
                     PortalUrl(host="idealista.com", url="https://other.example/venta-viviendas/"),       # host mismatch
                     PortalUrl(host="fotocasa.es", url=FOTOCASA_URL)])                                     # blocked
    checked = validate_plan(raw, known, blocked=blocked_hosts_of(task_spec, "ES"))
    assert [s.host for s in checked.sites] == ["idealista.com"]
    assert [q.text for q in checked.queries] == ["site:idealista.com piso venta Valencia"]
    assert [u.url for u in checked.portal_urls] == [IDEALISTA_URL]
    assert "spainhouses.net" in known


def test_queries_are_deduplicated_by_meaning_and_capped() -> None:
    raw = SearchPlan(queries=[PlanQuery(text="Piso en venta Valencia", language="es"),
                              PlanQuery(text="venta piso valencia", language="es"),
                              PlanQuery(text="x", language="es")])
    assert [q.text for q in validate_plan(raw, ["idealista.com"]).queries] == ["Piso en venta Valencia"]
    many = SearchPlan(queries=[PlanQuery(text=f"piso Valencia zona{n} calle{n}", language=lang)
                               for lang in ("es", "en", "ru", "uk", "es") for n in range(20)])
    checked = validate_plan(many, ["idealista.com"])
    assert len(checked.queries) <= MAX_QUERIES
    assert max(sum(q.language == lang for q in checked.queries) for lang in ("es", "en", "ru", "uk")) == 15


def test_parse_plan_survives_bad_items_and_languages_follow_the_text() -> None:
    parsed = parse_plan("```json\n" + json.dumps({"queries": [{"text": "a piso", "language": "xx"},
                                                               {"text": "piso Valencia", "language": "es"}, 5],
                                                  "stop": {"min_exact": "many"}}) + "\n```")
    assert [q.text for q in parsed.queries] == ["piso Valencia"] and parsed.stop.min_exact == 10
    assert plan_languages(spec()) == ["es", "en"]
    assert plan_languages(spec(), "квартира в Валенсии") == ["es", "en", "ru"]
    assert plan_languages(spec(), "квартира в Києві") == ["es", "en", "uk"]


# --- the planner -----------------------------------------------------------------------------------


async def test_planner_returns_a_validated_plan_from_one_call() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return answer(good_plan() | {"sites": good_plan()["sites"] + [{"host": "nope.com", "priority": 1}]})

    task_spec = spec()
    result = await planner(handler).plan(task_spec, campaign_plan(task_spec), source_text=GOAL)
    assert result is not None
    assert [s.host for s in result.sites] == ["idealista.com", "fotocasa.es"]
    assert len(result.queries) == 3 and len(result.portal_urls) == 2 and result.stop.min_exact == 15
    assert len(seen) == 1 and seen[0]["model"] == "m" and seen[0]["temperature"] == 0.3
    system, user = (m["content"] for m in seen[0]["messages"])
    assert "NEVER invent a host" in system and "idealista.com/venta-viviendas" in system
    data = json.loads(user.split("\n", 1)[1])
    assert data["languages"] == ["es", "en", "ru"] and data["country"] == "ES"
    assert "idealista.com" in data["known_portals"]["apartment"]


async def test_planner_failures_return_none() -> None:
    task_spec = spec()
    plan = campaign_plan(task_spec)

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    for handler in (lambda r: httpx.Response(500), lambda r: answer("not json"), lambda r: answer({"queries": []}),
                    lambda r: answer({"queries": [{"text": "piso Valencia", "language": "es", "site": "nope.com"}]}),
                    timeout):
        assert await planner(handler).plan(task_spec, plan) is None


async def test_plan_with_model_stores_the_plan_and_keeps_the_plan_when_the_planner_fails() -> None:
    task_spec = spec()
    plan = campaign_plan(task_spec)
    assert plan.search_plan is None

    class Good:
        async def plan(self, spec, plan, *, source_text=""):
            return SearchPlan.model_validate(good_plan())

    class Bad:
        async def plan(self, spec, plan, *, source_text=""):
            raise RuntimeError("boom")

    stored = await plan_with_model(plan, task_spec, Good())
    assert stored.search_plan is not None and stored.search_plan["stop"]["max_pages"] == 80
    assert await plan_with_model(plan, task_spec, Bad()) is plan


# --- consumption -----------------------------------------------------------------------------------


def task_with(search_plan: dict[str, Any] | None, **kw: Any) -> QueryTask:
    return QueryTask(goal=GOAL, task_text=GOAL, location="Valencia", location_aliases={"es": "Valencia", "en": "Valencia",
                     "ru": "Валенсия", "uk": "Валенсія"}, vertical="real_estate", constraints={"deal": "sale"},
                     country_code="ES", search_plan=search_plan, **kw)


async def test_round_uses_plan_queries_first_then_the_generators() -> None:
    plan = good_plan()
    task = task_with(plan)
    first = planned_round(task, [], 2)
    assert [q.language for q in first] == ["es", "es"] and first[0].text.startswith("piso en venta Valencia")
    assert "site:idealista.com" in first[1].text
    model = ListGenerator(["casa Valencia centro"])
    out = await FallbackQueryGenerator(model).generate(task, used=[], count=5)
    assert [q.text for q in out[:3]] == [q.text for q in planned_round(task, [], 3)]
    assert len(out) == 5 and model.calls[0][1] == 2  # the model only fills what the plan did not
    again = await FallbackQueryGenerator(None).generate(task, used=[q.text for q in out[:3]], count=3)
    assert not {q.text for q in again} & {q.text for q in out[:3]}  # never repeats a used plan query
    plain = await FallbackQueryGenerator(ListGenerator(["casa Valencia centro"])).generate(task_with(None), used=[], count=1)
    assert [q.text for q in plain] == ["casa Valencia centro España"]


def test_portals_follow_the_plan_priority_then_the_kind_list_and_blocked_are_removed() -> None:
    plan = {"sites": [{"host": "pisos.com", "priority": 3}, {"host": "kyero.com", "priority": 1}]}
    portals = task_with(plan).portals()
    assert portals[:2] == ("kyero.com", "pisos.com") and portals[2] == "idealista.com"
    assert task_with(None).portals()[0] == "idealista.com"
    blocked = task_with(plan, blocked_hosts=frozenset({"idealista.com", "kyero.com"})).portals()
    assert "idealista.com" not in blocked and "kyero.com" not in blocked and blocked[0] == "pisos.com"


class FixedPlanner:
    model = "fixed"

    def __init__(self, result: SearchPlan | None) -> None:
        self.result, self.calls = result, 0

    async def plan(self, spec, plan, *, source_text=""):
        self.calls += 1
        return self.result


async def make_campaign(campaigns: MemoryCampaignStore, task_spec: TaskSpec | None) -> str:
    plan = campaign_plan(task_spec or spec())
    cid = await campaigns.create(plan, chat_id=1, requested_by=1, source_text=GOAL, actor="test",
                                 spec=task_spec.model_dump(mode="json") if task_spec else None)
    await campaigns.set_state(cid, "running", "test")
    return cid


def web_worker(campaigns, store, searcher, generator, plan_maker=None, **config) -> WebSearchWorker:
    return WebSearchWorker(campaigns, store, searcher, FakeFetcher(), generator, planner=plan_maker,
                           config=WebSearchConfig(**{"cover_portals": False, **config}))


async def test_worker_plans_once_persists_and_enqueues_portal_urls_as_index_pages() -> None:
    campaigns, store = MemoryCampaignStore(), MemoryWebStore(None)
    store.campaigns = campaigns
    cid = await make_campaign(campaigns, spec())
    fixed = FixedPlanner(SearchPlan.model_validate(good_plan()))
    searcher = FakeSearcher()
    w = web_worker(campaigns, store, searcher, FallbackQueryGenerator(None), fixed, queries_per_round=4,
                   max_queries_per_campaign=4)
    await run_until_done(w, cid)
    assert fixed.calls == 1
    stored = (await campaigns.get(cid)).plan.search_plan
    assert stored is not None and len(stored["portal_urls"]) == 2
    rows = {r.url: r for r in store.urls[cid].values()}
    assert rows[IDEALISTA_URL].kind == "index" and rows[IDEALISTA_URL].depth == 0
    assert rows[FOTOCASA_URL].kind == "index"
    assert searcher.calls[0][0].startswith("piso en venta Valencia")  # plan queries before templates


async def test_worker_falls_back_to_todays_behaviour_when_the_planner_fails_or_is_missing() -> None:
    for maker in (FixedPlanner(None), None):
        campaigns, store = MemoryCampaignStore(), MemoryWebStore(None)
        store.campaigns = campaigns
        cid = await make_campaign(campaigns, spec())
        w = web_worker(campaigns, store, FakeSearcher(), ListGenerator(["terreno Boadilla Valencia"]), maker)
        await run_until_done(w, cid)
        assert (await campaigns.get(cid)).plan.search_plan is None
        assert [q.text for q in store.queries[cid]] == ["terreno Boadilla Valencia España"]
        assert not store.urls.get(cid)
    campaigns = MemoryCampaignStore()  # a goal without a spec has nothing to plan from
    cid = await make_campaign(campaigns, spec())
    campaign = replace(await campaigns.get(cid), spec=None)
    fixed = FixedPlanner(SearchPlan.model_validate(good_plan()))
    w = web_worker(campaigns, MemoryWebStore(campaigns), FakeSearcher(), FallbackQueryGenerator(None), fixed)
    assert await w._with_search_plan(campaign) is campaign and fixed.calls == 0


async def test_blocked_sources_are_never_searched_or_queued() -> None:
    campaigns, store = MemoryCampaignStore(), MemoryWebStore(None)
    store.campaigns = campaigns
    task_spec = spec(blocked=["idealista", "https://www.example-spam.com/"])
    cid = await make_campaign(campaigns, task_spec)
    fixed = FixedPlanner(SearchPlan.model_validate(good_plan()))
    bad = "https://www.example-spam.com/piso-valencia-12345678"
    good = "https://www.kyero.com/property/12345678-piso-valencia"
    searcher = FakeSearcher(default=[bad, good, "https://www.idealista.com/inmueble/99999999/"])
    w = web_worker(campaigns, store, searcher, FallbackQueryGenerator(None), fixed, queries_per_round=3,
                   max_queries_per_campaign=3)
    await run_until_done(w, cid)
    task = query_task(await campaigns.get(cid))
    assert task.blocked_hosts == frozenset({"idealista.com", "example-spam.com"})
    queued = {r.host for r in store.urls[cid].values()}
    assert queued == {"kyero.com", "fotocasa.es"}  # the idealista portal URL was not queued either
    assert "idealista.com" not in task.portals()


# --- review fixes: listing URLs, languages, cap, deadline, blocked index links -----------------------


def test_a_listing_url_is_not_a_portal_search_page() -> None:
    listing = "https://www.idealista.com/inmueble/12345678/"
    raw = SearchPlan(portal_urls=[PortalUrl(host="idealista.com", url=listing),
                                  PortalUrl(host="idealista.com", url=IDEALISTA_URL)])
    assert [u.url for u in validate_plan(raw, ["idealista.com"]).portal_urls] == [IDEALISTA_URL]


async def test_worker_queues_portal_urls_with_their_classified_kind() -> None:
    campaigns, store = MemoryCampaignStore(), MemoryWebStore(None)
    store.campaigns = campaigns
    cid = await make_campaign(campaigns, spec())
    plan = good_plan() | {"portal_urls": [{"host": "idealista.com", "url": "https://www.idealista.com/inmueble/12345678/"},
                                          {"host": "idealista.com", "url": IDEALISTA_URL}]}
    campaign = await campaigns.get(cid)
    await campaigns.set_search_plan(cid, plan)  # a stored plan (old data) may still hold a listing URL
    w = web_worker(campaigns, store, FakeSearcher(), FallbackQueryGenerator(None))
    await w._enqueue_portal_urls(await campaigns.get(cid) or campaign)
    rows = {r.url: r.kind for r in store.urls[cid].values()}
    assert rows == {IDEALISTA_URL: "index"}


def test_languages_follow_the_country_and_the_task_script() -> None:
    assert plan_languages(spec(), "квартира в Валенсии", "ES") == ["es", "en", "ru"]
    assert plan_languages(spec(), "квартира в Валенсії", "ES") == ["es", "en", "uk"]  # one of ru / uk, never both
    assert plan_languages(spec(), "flat in Valencia", "ES") == ["es", "en"]
    assert plan_languages(spec(), "квартира в Киеве", "UA") == ["uk", "ru", "en"]
    assert "es" not in plan_languages(spec(), "flat in Kyiv", "UA")


def test_spain_caps_cyrillic_queries_to_a_quarter_ukraine_keeps_them() -> None:
    queries = ([PlanQuery(text=f"piso Valencia zona{n} calle{n}", language="es") for n in range(8)]
               + [PlanQuery(text=f"квартира Valencia район{n} улица{n}", language="ru") for n in range(6)])
    capped = validate_plan(SearchPlan(queries=queries), [], country="ES")
    cyr = [q for q in capped.queries if q.language == "ru"]
    assert len(cyr) == 2 and len(capped.queries) == 10 and len(cyr) / len(capped.queries) <= 0.25
    assert sum(q.language == "ru" for q in validate_plan(SearchPlan(queries=queries), [], country="UA").queries) == 6


def test_planned_round_applies_the_same_cyrillic_cap() -> None:
    plan = {"queries": [{"text": f"квартира Valencia район{n}", "language": "ru"} for n in range(6)]
            + [{"text": f"piso Valencia zona{n}", "language": "es"} for n in range(6)]}
    out = planned_round(task_with(plan), [], 8)
    assert len(out) == 8 and sum(q.language == "ru" for q in out) <= 2


async def test_the_planner_deadline_is_total_across_both_posts() -> None:
    import asyncio

    calls: list[int] = []

    async def slow(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        await asyncio.sleep(0.2)
        return httpx.Response(400)  # triggers the retry without response_format

    p = OpenRouterSearchPlanner(api_key="k", model="m", timeout_seconds=0.3,
                                client=httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    task_spec = spec()
    started = asyncio.get_running_loop().time()
    assert await p.plan(task_spec, campaign_plan(task_spec)) is None
    assert asyncio.get_running_loop().time() - started < 0.5 and len(calls) == 2


def test_plan_timeout_setting_is_bounded_under_the_web_lease() -> None:
    import pytest
    from pydantic import ValidationError

    from bot.campaign.settings import CampaignRunnerSettings

    with pytest.raises(ValidationError):
        CampaignRunnerSettings(OPENROUTER_PLAN_TIMEOUT_SECONDS=120)


def test_blocked_campaign_hosts_are_filtered_from_index_links() -> None:
    from bot.web_search.extract import parse_html
    from bot.web_search.models import QueuedUrl
    from bot.web_search.urls import url_key
    from tests.test_web_search import INDEX_HTML, INDEX_URL

    w = web_worker(MemoryCampaignStore(), MemoryWebStore(None), FakeSearcher(), FallbackQueryGenerator(None))
    queued = QueuedUrl(INDEX_URL, url_key(INDEX_URL), "fotocasa.es", 0, "index")
    parsed = parse_html(INDEX_HTML, INDEX_URL)
    from bot.web_search.structured import structured

    data = structured(INDEX_HTML, INDEX_URL)
    _, open_children = w._page(queued, INDEX_URL, parsed, data)
    _, blocked_children = w._page(queued, INDEX_URL, parsed, data, blocked=frozenset({"fotocasa.es"}))
    assert open_children and not blocked_children
