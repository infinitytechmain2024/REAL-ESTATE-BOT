"""v3 Q61: BOTH_MODE_INVESTOR_QUERIES -- a ``both`` campaign's web stage also writes investor rounds (mocks only).

Bench run b5-g9-s6 (persona G9, plot in Madrid plus investors for the project): every query round of the web stage
looked for plots, because the Q51 investor round (INVESTOR_WEB_QUERIES) is only for ``vertical == "investors"``.
"""

from __future__ import annotations

from bot.campaign import MemoryCampaignStore
from bot.web_search.models import GeneratedQuery
from bot.web_search.queries import FallbackQueryGenerator, QueryTask
from bot.web_search.settings import WebSearchSettings
from bot.web_search.store import MemoryWebStore
from bot.web_search.worker import (
    WebSearchConfig,
    WebSearchWorker,
    investor_part,
    investor_round,
    query_task,
)
from tests.test_q54_both_mode_portals import _campaign
from tests.test_web_search import FakeFetcher, FakeSearcher, run_until_done

OBJECT_HIT = "https://www.terrenos-madrid.example/solar-{n}"
INVESTOR_HIT = "https://www.clubinversores-madrid.example/socios-{n}"
PORTAL_HIT = "https://www.fotocasa.es/es/comprar/terreno/madrid/19000000{n}/d"


class RoundGenerator:
    """Writes ``count`` new queries a round: investor words for an investor task, plot words otherwise."""

    def __init__(self) -> None:
        self.tasks: list[QueryTask] = []

    async def generate(self, task: QueryTask, *, used: list[str], count: int) -> list[GeneratedQuery]:
        self.tasks.append(task)
        words = "inversores proyecto" if task.vertical == "investors" else "solar urbano venta"
        start = len(used)
        return [GeneratedQuery(f"{words} Madrid {start + i}", "es") for i in range(count)]


class RoundSearcher(FakeSearcher):
    """An investor query finds an investor page and a portal page; a plot query a plot page."""

    async def search(self, query: str, *, language: str | None = None):
        n = len(self.calls)
        self.default = ([INVESTOR_HIT.format(n=n), PORTAL_HIT.format(n=n)] if "inversores" in query
                        else [OBJECT_HIT.format(n=n)])
        return await super().search(query, language=language)


class VerticalStore(MemoryWebStore):
    """The vertical every fetch was begun with, by URL."""

    def __init__(self, campaigns) -> None:
        super().__init__(campaigns)
        self.verticals: dict[str, str] = {}

    async def begin_fetch(self, campaign_id, url, **kw):
        self.verticals[url.url] = kw["vertical"]
        return await super().begin_fetch(campaign_id, url, **kw)


async def _run(mode: str, *, flag: bool, max_queries: int = 7, investor_web: bool = True):
    campaigns = MemoryCampaignStore()
    camp = await _campaign(mode, campaigns)
    store, generator, searcher = VerticalStore(campaigns), RoundGenerator(), RoundSearcher()
    w = WebSearchWorker(campaigns, store, searcher, FakeFetcher(), generator,
                        config=WebSearchConfig(cover_portals=False, queries_per_round=2,
                                               max_queries_per_campaign=max_queries, domain_policy="off",
                                               investor_web_queries=investor_web,
                                               both_mode_investor_queries=flag))
    await run_until_done(w, camp.id)
    return camp.id, store, generator, searcher


# --- flag on, both -------------------------------------------------------------------------------


async def test_flag_on_both_alternates_object_and_investor_rounds_within_the_query_cap() -> None:
    cid, store, generator, searcher = await _run("both", flag=True)
    assert [t.vertical for t in generator.tasks] == ["both", "investors", "both", "investors"]
    assert all(t.investor_web and t.search_plan is None and t.required_portals == ()
               for t in generator.tasks if t.vertical == "investors")
    rounds = [(q.round_no, q.text.split()[0]) for q in store.queries[cid]]
    assert {r for r, word in rounds if word == "inversores"} == {2, 4}
    assert {r for r, word in rounds if word == "solar"} == {1, 3}
    assert len(store.queries[cid]) == 7 and len(searcher.calls) == 7  # WEB_SEARCH_MAX_QUERIES_PER_CAMPAIGN


async def test_flag_on_both_investor_hits_are_investors_and_portal_hits_dropped() -> None:
    _cid, store, _generator, _searcher = await _run("both", flag=True)
    investors = {u: v for u, v in store.verticals.items() if "clubinversores" in u}
    objects = {u: v for u, v in store.verticals.items() if "terrenos-madrid" in u}
    assert investors and set(investors.values()) == {"investors"}
    assert objects and set(objects.values()) == {"both"}  # an object round's page: as before
    assert not any("fotocasa.es" in u for u in store.verticals)  # an investor round never reads a listing portal


async def test_flag_on_both_does_not_need_investor_web_queries() -> None:
    _cid, store, generator, _searcher = await _run("both", flag=True, investor_web=False)
    assert "investors" in [t.vertical for t in generator.tasks]
    assert "investors" in store.verticals.values()


def test_investor_part_and_rounds() -> None:
    both = QueryTask(goal="both · Madrid · sale", task_text="t", location="Madrid", location_aliases={},
                     vertical="both", search_plan={"queries": []}, required_portals=("idealista.com",))
    part = investor_part(both)
    assert part.vertical == "investors" and part.goal == "investors · Madrid · sale" and part.investor_web
    assert part.search_plan is None and part.required_portals == () and part.portals() == ()
    real_estate = QueryTask(goal="g", task_text="t", location="Madrid", location_aliases={}, vertical="real_estate")
    assert investor_part(real_estate) is real_estate
    assert [investor_round(n) for n in (None, 1, 2, 3, 4)] == [False, False, True, False, True]


# --- flag off: as before; real_estate and investors: unchanged ----------------------------------------


async def test_flag_off_both_searches_objects_only_as_before() -> None:
    cid, store, generator, searcher = await _run("both", flag=False)
    assert {t.vertical for t in generator.tasks} == {"both"}
    assert not any(q.text.startswith("inversores") for q in store.queries[cid])
    assert set(store.verticals.values()) == {"both"}
    assert len(searcher.calls) == 7


async def test_real_estate_and_investors_do_not_change_with_the_flag() -> None:
    for mode in ("real_estate", "investors"):
        off_cid, off_store, off_gen, off_search = await _run(mode, flag=False)
        on_cid, on_store, on_gen, on_search = await _run(mode, flag=True)
        assert [q.text for q in off_store.queries[off_cid]] == [q.text for q in on_store.queries[on_cid]]
        assert [q.round_no for q in off_store.queries[off_cid]] == [q.round_no for q in on_store.queries[on_cid]]
        assert [t.vertical for t in off_gen.tasks] == [t.vertical for t in on_gen.tasks] == [mode] * 4
        assert off_search.calls == on_search.calls
        assert off_store.verticals == on_store.verticals


async def test_flag_on_both_query_task_of_the_campaign_is_unchanged() -> None:
    campaign = await _campaign("both")
    assert query_task(campaign, investor_queries=True).vertical == "both"  # the split is per round, in the worker


async def test_template_fallback_writes_investor_queries_for_an_investor_round() -> None:
    campaign = await _campaign("both")
    task = investor_part(query_task(campaign, investor_queries=True))
    queries = await FallbackQueryGenerator(None).generate(task, used=[], count=4)
    assert queries and all("site:" not in q.text for q in queries)


def test_setting_reaches_the_config(monkeypatch) -> None:
    assert WebSearchSettings(_env_file=None).config().both_mode_investor_queries is False
    monkeypatch.setenv("BOTH_MODE_INVESTOR_QUERIES", "true")
    assert WebSearchSettings(_env_file=None).config().both_mode_investor_queries is True
