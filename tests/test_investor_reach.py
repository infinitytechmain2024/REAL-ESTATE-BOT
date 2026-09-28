"""Investor reach across platforms: which results count, the queries, judging, the worker, the card, PostgreSQL."""

from __future__ import annotations

import json

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.reach import (
    Candidate,
    Contact,
    Judged,
    MemoryReachStore,
    PostgresReachStore,
    ReachCampaign,
    ReachConfig,
    ReachWorker,
    contact_card,
    parse_judged,
    plan_queries,
    platform_of,
    rule_judge,
)
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore, PostgresRunStore
from bot.campaign.store import PostgresCampaignStore
from bot.orchestra.store import SafetyLimits
from bot.web_search.searxng import SearchError, SearchHit
from tests.test_campaign_runner import CHAT, Clock, FakeMessenger
from tests.test_near_match import USER, ButtonMessenger, needs_db
from tests.test_near_match import pool as pool

MADRID = ReachCampaign("c1", "Madrid", {"es": "Madrid", "en": "Madrid", "ru": "Мадрид", "uk": "Мадрид"},
                       ("es", "en", "ru", "uk"), "Инвесторы в недвижимость в Мадриде")


@pytest.mark.parametrize(("url", "expected"), [
    ("https://es.linkedin.com/in/juan-perez-123", ("linkedin", "profile")),
    ("https://www.linkedin.com/company/madrid-capital/", ("linkedin", "company")),
    ("https://www.linkedin.com/posts/juan_inversion-activity-1", ("linkedin", "post")),
    ("https://www.linkedin.com/jobs/view/1", None),
    ("https://www.reddit.com/r/GoingToSpain/comments/abc/investing_in_madrid/", ("reddit", "post")),
    ("https://www.reddit.com/user/madrid_investor", ("reddit", "profile")),
    ("https://www.reddit.com/r/GoingToSpain/", None),
    ("https://x.com/juaninvierte/status/123", ("x", "post")),
    ("https://twitter.com/juaninvierte", ("x", "profile")),
    ("https://x.com/search?q=x", None),
    ("https://www.instagram.com/p/C1abc/", ("instagram", "post")),
    ("https://www.instagram.com/inversor.madrid/", ("instagram", "profile")),
    ("https://www.instagram.com/explore/tags/x/", None),
    ("https://www.tiktok.com/@inversor/video/7", ("tiktok", "post")),
    ("https://www.tiktok.com/@inversor", ("tiktok", "profile")),
    ("https://www.youtube.com/@canal", ("youtube", "profile")),
    ("https://www.youtube.com/watch?v=1", ("youtube", "post")),
    ("https://www.facebook.com/groups/1/", None),
    ("https://es.wikipedia.org/wiki/Madrid", None),
    ("https://www.aban.es/socios/madrid", ("web", "page")),
    ("https://example.com/doc.pdf", None),
    ("http://example.com/", None),
])
def test_results_worth_judging_are_profiles_posts_and_public_pages(url: str, expected) -> None:
    assert platform_of(url) == expected


def test_queries_cover_every_platform_in_the_campaigns_languages_with_its_city() -> None:
    queries = plan_queries(MADRID)
    assert {q.platform for q in queries} == {"linkedin", "reddit", "x", "instagram", "tiktok", "youtube", "web"}
    assert all("Madrid" in q.text or "Мадрид" in q.text for q in queries)
    assert len({q.text for q in queries}) == len(queries)
    assert queries[0].text == 'site:linkedin.com/in "real estate investor" Madrid'
    spanish_only = plan_queries(ReachCampaign("c", "Madrid", {"es": "Madrid"}, ("es",)))
    assert {q.language for q in spanish_only} == {"es", "en"}


def _cand(title: str, snippet: str = "", url: str = "https://es.linkedin.com/in/x") -> Candidate:
    where = platform_of(url) or ("web", "page")
    return Candidate(url, where[0], where[1], title, snippet)


@pytest.mark.parametrize(("title", "snippet", "kind", "relevant"), [
    ("Juan Pérez - Inversor inmobiliario - Madrid | LinkedIn", "", "investor", True),
    ("Ana García - Agente inmobiliario en Madrid", "", "agent", True),
    ("Madrid Business Angels Network", "Red de business angels en Madrid", "network", True),
    ("Capital Family Office", "family office con sede en Madrid", "fund", True),
    ("Busco inversor para proyecto en Madrid", "", "seeking", True),
    ("Juan Pérez - Inversor inmobiliario - Valencia", "", "investor", False),
    ("Madrid weather today", "", "other", False),
])
def test_the_rules_find_the_kind_and_require_the_city(title: str, snippet: str, kind: str, relevant: bool) -> None:
    judged = rule_judge(_cand(title, snippet), MADRID)
    assert (judged.kind, judged.relevant) == (kind, relevant)
    assert judged.keep is relevant


def test_the_rules_take_the_name_from_the_title() -> None:
    assert rule_judge(_cand("Juan Pérez - Inversor inmobiliario - Madrid | LinkedIn"), MADRID).name == "Juan Pérez"


def test_the_models_answer_is_one_verdict_per_result_and_drift_is_not_kept() -> None:
    content = json.dumps({"results": [
        {"index": 0, "kind": "Investor", "relevant": True, "confidence": 0.9, "name": "Juan", "summary_ru": "Инвестор"},
        {"index": 1, "kind": "agent", "relevant": "yes", "confidence": 0.9, "name": None, "summary_ru": ""},
        {"index": 7, "kind": "fund", "relevant": True, "confidence": 1, "name": "X", "summary_ru": "?"},
    ]})
    judged = parse_judged(content, 3)
    assert judged[0] == Judged("investor", True, 0.9, "Juan", "Инвестор") and judged[0].keep
    assert not judged[1].keep, "only a real true is relevant"
    assert judged[2] == Judged("other", False, 0.0)


def test_the_card_says_who_where_the_link_and_how_to_approach() -> None:
    card = contact_card(Contact("k" * 64, "https://es.linkedin.com/in/juan-perez", "linkedin", "investor", "Juan Pérez",
                                "Juan Pérez - Inversor inmobiliario - Madrid | LinkedIn", "Contacto: +34 612 345 678",
                                "Инвестор в жилую недвижимость Мадрида"))
    assert card.splitlines() == [
        "💼 Инвестор · LinkedIn",
        "Имя: Juan Pérez",
        "Ссылка: https://es.linkedin.com/in/juan-perez",
        "Заголовок: Juan Pérez - Inversor inmobiliario - Madrid | LinkedIn",
        "Кратко: Инвестор в жилую недвижимость Мадрида",
        "Контакт: +34 612 345 678",
        "Рекомендация: Написать через LinkedIn и предложить конкретный объект под инвестиции.",
    ]
    web = contact_card(Contact("k" * 64, "https://www.aban.es/socios", "web", "network", title="ABAN"))
    assert web.startswith("🤝 Клуб инвесторов / бизнес-ангелы · aban.es\nСсылка: https://www.aban.es/socios")


# --- the worker --------------------------------------------------------------------------------------


class FakeSearcher:
    def __init__(self, results: dict[str, list[SearchHit]] | None = None, fail: set[str] | None = None) -> None:
        self.results, self.fail = results or {}, fail or set()
        self.queries: list[tuple[str, str | None]] = []

    async def search(self, query: str, *, language: str | None = None) -> list[SearchHit]:
        self.queries.append((query, language))
        if query in self.fail:
            raise SearchError("timeout")
        return self.results.get(query, [])


LINKEDIN_Q = 'site:linkedin.com/in "real estate investor" Madrid'
HITS = [SearchHit("https://es.linkedin.com/in/juan-perez", "Juan Pérez - Real Estate Investor - Madrid | LinkedIn", ""),
        SearchHit("https://es.linkedin.com/in/juan-perez", "duplicate", ""),
        SearchHit("https://www.facebook.com/juan", "Juan on Facebook", ""),
        SearchHit("https://es.linkedin.com/in/ana", "Ana - Barista - Madrid", "")]


async def test_a_tick_runs_a_few_queries_keeps_each_result_once_and_ends_at_the_cap() -> None:
    store = MemoryReachStore([MADRID])
    searcher = FakeSearcher({LINKEDIN_Q: HITS})
    worker = ReachWorker(store, searcher, config=ReachConfig(queries_per_campaign=3, queries_per_tick=2))
    assert await worker.tick() == 2
    assert [q for q, _ in searcher.queries] == [q.text for q in plan_queries(MADRID)[:2]]
    assert [(s.candidate.url, s.judged.keep, s.judged_by) for s in store.contacts.values()] == [
        ("https://es.linkedin.com/in/juan-perez", True, "rules"), ("https://es.linkedin.com/in/ana", False, "rules")]
    assert store.current["c1"] is None
    assert await worker.tick() == 1
    assert await worker.tick() == 0 and store.done == {"c1"}
    # A link already judged (by any campaign) is never judged or stored again.
    other = MemoryReachStore([ReachCampaign("c2", "Madrid", {"en": "Madrid"}, ("en",))])
    other.contacts = dict(store.contacts)
    await ReachWorker(other, FakeSearcher({LINKEDIN_Q: HITS})).tick()
    assert len(other.contacts) == 2


async def test_the_daily_cap_and_a_failed_search_are_respected() -> None:
    store = MemoryReachStore([MADRID], today=80)
    searcher = FakeSearcher()
    assert await ReachWorker(store, searcher).tick() == 0 and searcher.queries == []
    store = MemoryReachStore([MADRID])
    searcher = FakeSearcher(fail={LINKEDIN_Q})
    await ReachWorker(store, searcher, config=ReachConfig(queries_per_tick=1)).tick()
    assert store.errors == {LINKEDIN_Q: "timeout"} and store.used["c1"] == [LINKEDIN_Q], "never searched again"


async def test_the_model_decides_and_the_rules_step_in_when_it_fails() -> None:
    class Judge:
        model = "openai/gpt-4o-mini"

        def __init__(self, fail: bool) -> None:
            self.fail = fail

        async def judge(self, campaign, candidates):
            if self.fail:
                raise RuntimeError("down")
            return [Judged("agent", True, 0.8, "Ana", "Агент") for _ in candidates]

    for fail, expected in ((False, [("agent", "openai/gpt-4o-mini")] * 2), (True, [("investor", "rules"), ("other", "rules")])):
        store = MemoryReachStore([MADRID])
        await ReachWorker(store, FakeSearcher({LINKEDIN_Q: HITS}), Judge(fail),
                          config=ReachConfig(queries_per_tick=1)).tick()
        assert [(s.judged.kind, s.judged_by) for s in store.contacts.values()] == expected


def test_unsafe_reach_settings_are_refused() -> None:
    with pytest.raises(ValueError):
        ReachConfig(queries_per_tick=0)


# --- the runner -------------------------------------------------------------------------------------


def _contact(n: int, kind: str = "investor") -> Contact:
    return Contact(f"{n:064d}", f"https://es.linkedin.com/in/p{n}", "linkedin", kind, f"P{n}")


async def test_an_investor_search_sends_reach_contacts_once_and_waits_for_the_reach() -> None:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, now=Clock(), config=RunnerConfig(max_people=2))
    goal = "Найди инвесторов в Мадриде"
    cid = await campaigns.create(plan_campaign(goal), chat_id=CHAT, requested_by=8, source_text=goal, actor="t")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.contacts["Madrid"] = [_contact(1), _contact(2, "agent"), _contact(3)]
    store.reaching.add(cid)
    await runner.step(cid)
    await runner.step(cid)
    cards = [t for _, _, t in messenger.sent if "· LinkedIn" in t]
    assert [c.splitlines()[0] for c in cards] == ["💼 Инвестор · LinkedIn", "🧑‍💼 Агент недвижимости · LinkedIn"]
    assert (await campaigns.get(cid)).state == "running", "the search waits while the reach still runs"
    store.reaching.clear()
    await runner.step(cid)
    assert (await campaigns.get(cid)).state == "completed"


# --- PostgreSQL ---------------------------------------------------------------------------------------


@needs_db
async def test_postgres_reach_from_the_query_to_the_card(pool) -> None:
    campaigns = PostgresCampaignStore(pool)
    goal = "Найди инвесторов в Мадриде"
    cid = await campaigns.create(plan_campaign(goal), chat_id=CHAT, requested_by=USER, source_text=goal, actor="t")
    await campaigns.set_state(cid, "running", "campaign:test")
    property_search = await campaigns.create(plan_campaign("Найди квартиры в аренду в Мадриде"), chat_id=CHAT,
                                             requested_by=USER, source_text="x", actor="t")
    store = PostgresReachStore(pool)
    opened = await store.open_campaigns()
    assert [c.id for c in opened] == [cid], "only investor searches reach out"
    assert opened[0].location == "Madrid" and opened[0].aliases["ru"] == "Мадрид"

    worker = ReachWorker(store, FakeSearcher({LINKEDIN_Q: HITS}), config=ReachConfig(queries_per_tick=2))
    assert await worker.tick() == 2
    assert await store.queries_today() == 2 and len(await store.used_queries(cid)) == 2
    rows = await pool.fetch("select url, kind, relevant, location from reach_contacts order by url")
    assert [(r["url"], r["kind"], r["relevant"], r["location"]) for r in rows] == [
        ("https://es.linkedin.com/in/ana", "other", False, "Madrid"),
        ("https://es.linkedin.com/in/juan-perez", "investor", True, "Madrid")]
    runs = PostgresRunStore(pool, SafetyLimits())
    assert await runs.reach_pending(cid) and not await runs.reach_pending(property_search)

    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, runs, messenger, None)
    await runner.step(cid)
    await runner.step(cid)
    cards = [t for _, _, t in messenger.sent if t.startswith("💼 Инвестор · LinkedIn")]
    assert len(cards) == 1 and "Ссылка: https://es.linkedin.com/in/juan-perez" in cards[0]
    assert await runs.stored_contacts(cid, "Madrid", 90, 10) == []

    await store.finish(cid)
    assert await store.open_campaigns() == [] and not await runs.reach_pending(cid)
