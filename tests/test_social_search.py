"""Social network search: adapters on fixture snapshots, queries, the worker's caps, dedup and stops.

No network and no browser: the browser is a fake that serves fixture
snapshots shaped like the Browser Session Manager's (``items``, ``meta``,
``logged_in``), and the stores are the in-memory twins.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore, SocialActivity
from bot.social_search.adapters import adapter_for, detect_block
from bot.social_search.queries import (
    OpenRouterQueryGenerator,
    QueryContext,
    QueryPlanner,
    SocialQuery,
    fallback_queries,
    normalise_query,
    parse_queries,
    query_key,
)
from bot.social_search.store import MemorySocialStore
from bot.social_search.worker import SocialConfig, SocialSearchWorker, parse_platforms
from tests.test_campaign_runner import Clock, FakeDiscovery, FakeMessenger

LAND = "участок от 1000 м² под застройку в пригороде Мадрида, покупка"
INVESTORS = "ищу инвесторов для строительного проекта в Мадриде"
T0 = 7301234567890123456  # a TikTok id: its top 32 bits are the upload second (2023-11)

# --- fixture snapshots (what the Browser Session Manager returns) ------------------------------

TIKTOK_SEARCH = {
    "url": "https://www.tiktok.com/search?q=terreno%20en%20venta%20Madrid", "title": "terreno en venta Madrid | TikTok",
    "text": "Resultados", "logged_in": True,
    "items": [
        {"kind": "post", "url": f"https://www.tiktok.com/@parcelas.madrid/video/{T0}?is_from_webapp=1",
         "text": "Vendo parcela de 1.200 m² en Boadilla del Monte, edificable, 390.000 € #terrenomadrid", "alt": ""},
        # The same video again (thumbnail link): one item, the richer card kept.
        {"kind": "post", "url": f"https://www.tiktok.com/@parcelas.madrid/video/{T0}", "text": "", "alt": "parcela"},
        {"kind": "post", "url": "https://www.tiktok.com/@casas_es/photo/7301234567890123999",
         "text": "Terreno urbanizable en Valdemoro 2.000 m²", "alt": ""},
        {"kind": "post", "url": "https://www.tiktok.com/@x/live", "text": "live now"},  # not a post
        {"kind": "post", "url": "https://evil.example/@a/video/7301234567890123456", "text": "phish"},
    ],
}
INSTAGRAM_TAG = {
    "url": "https://www.instagram.com/explore/tags/terrenomadrid/", "title": "#terrenomadrid", "text": "", "logged_in": True,
    "items": [
        {"kind": "post", "url": "https://www.instagram.com/p/Cx1AbCdEf/", "text": "", "alt": "May be an image of land"},
        {"kind": "post", "url": "https://www.instagram.com/casas.madrid/reel/Cy9ZzYyXx/", "text": "", "alt": ""},
        {"kind": "post", "url": "https://www.instagram.com/explore/tags/other/", "text": "tag"},
    ],
}
INSTAGRAM_POST = {
    "url": "https://www.instagram.com/p/Cx1AbCdEf/", "title": "Instagram", "text": "", "logged_in": True, "items": [],
    "meta": {"og_description": '34 likes, 2 comments - casas.madrid on March 3, 2025: "Se vende parcela 1.500 m² en '
                               'Arganda del Rey, edificable. 250.000 €"', "time": "2025-03-03T10:00:00.000Z"},
}
LINKEDIN_POSTS = {
    "url": "https://www.linkedin.com/search/results/content/?keywords=inversores", "title": "LinkedIn", "text": "",
    "logged_in": True,
    "items": [
        {"kind": "post", "url": "urn:li:activity:7150000000000000001", "author": "Ana López",
         "text": "Buscamos inversores para promoción residencial en Madrid, ticket desde 100k", "time": "2d"},
        {"kind": "person", "url": "https://www.linkedin.com/in/ana-lopez-123/?miniProfileUrn=x", "text": "Ana López"},
    ],
}
LINKEDIN_PEOPLE = {
    "url": "https://www.linkedin.com/search/results/people/?keywords=business%20angel", "title": "LinkedIn", "text": "",
    "logged_in": True,
    "items": [
        {"kind": "person", "url": "https://es.linkedin.com/in/Juan-Perez-Inversor", "text": "Juan Pérez · Business Angel · Madrid"},
        {"kind": "company", "url": "https://www.linkedin.com/company/fondo-madrid/", "text": "Fondo Madrid"},
        {"kind": "person", "url": "https://www.linkedin.com/in/", "text": "empty"},
    ],
}


# --- adapters ---------------------------------------------------------------------------------


def test_tiktok_cards_become_canonical_posts_with_their_upload_date() -> None:
    adapter = adapter_for("tiktok")
    assert adapter.search_url("hashtag", "#TerrenoMadrid") == "https://www.tiktok.com/tag/terrenomadrid"
    assert adapter.search_url("keyword", "parcela en venta") == "https://www.tiktok.com/search?q=parcela+en+venta"
    items = adapter.parse(TIKTOK_SEARCH, "keyword")
    assert [i.key for i in items] == [f"video:{T0}", "video:7301234567890123999"]
    first = items[0]
    assert first.url == f"https://www.tiktok.com/@parcelas.madrid/video/{T0}" and first.author == "@parcelas.madrid"
    assert "1.200 m²" in first.text and first.published_at and first.published_at.year == 2023
    assert items[1].url.endswith("/photo/7301234567890123999")
    assert adapter.parse(TIKTOK_SEARCH, "keyword", limit=1) == items[:1]


def test_instagram_grid_tiles_are_opened_for_their_caption() -> None:
    adapter = adapter_for("instagram")
    assert adapter.search_url("hashtag", "terreno madrid") == "https://www.instagram.com/explore/tags/terrenomadrid/"
    assert adapter.search_url("keyword", "parcela Madrid").startswith("https://www.instagram.com/explore/search/keyword/?q=")
    tile, reel = adapter.parse(INSTAGRAM_TAG, "hashtag")
    assert (tile.key, tile.url) == ("p:Cx1AbCdEf", "https://www.instagram.com/p/Cx1AbCdEf/")
    assert (reel.key, reel.url) == ("p:Cy9ZzYyXx", "https://www.instagram.com/reel/Cy9ZzYyXx/")
    assert adapter.needs_detail(tile)
    post = adapter.with_detail(tile, INSTAGRAM_POST)
    assert post.author == "@casas.madrid" and post.text.startswith("Se vende parcela 1.500 m²")
    assert post.published_at == datetime(2025, 3, 3, 10, tzinfo=UTC)
    assert not adapter.needs_detail(post)


def test_linkedin_posts_people_and_companies_by_search_kind() -> None:
    adapter = adapter_for("linkedin")
    assert adapter.search_url("posts", "inversores Madrid") == \
        "https://www.linkedin.com/search/results/content/?keywords=inversores+Madrid"
    assert "/search/results/people/" in adapter.search_url("people", "x")
    assert "/search/results/companies/" in adapter.search_url("companies", "x")
    [post] = adapter.parse(LINKEDIN_POSTS, "posts")  # the author link is not a post
    assert post.url == "https://www.linkedin.com/feed/update/urn:li:activity:7150000000000000001/"
    assert post.author == "Ana López" and post.published_at and post.published_at.year == 2024
    people = adapter.parse(LINKEDIN_PEOPLE, "people")
    assert [(p.key, p.url) for p in people] == [("in:juan-perez-inversor", "https://www.linkedin.com/in/juan-perez-inversor/")]
    assert [c.key for c in adapter.parse(LINKEDIN_PEOPLE, "companies")] == ["company:fondo-madrid"]
    assert not adapter.needs_detail(people[0])
    with pytest.raises(ValueError):
        adapter.search_url("hashtag", "x")


@pytest.mark.parametrize(("platform", "snapshot", "kind"), [
    ("instagram", {"url": "https://www.instagram.com/accounts/login/?next=/explore/"}, "login"),
    ("instagram", {"url": "https://www.instagram.com/challenge/action/"}, "checkpoint"),
    ("instagram", {"url": "https://www.instagram.com/explore/tags/x/", "text": "Try Again Later. We restrict certain activity"}, "rate_limit"),
    ("tiktok", {"url": "https://www.tiktok.com/search?q=x", "text": "Drag the slider to fit the puzzle"}, "captcha"),
    ("tiktok", {"url": "https://www.tiktok.com/login?redirect_url=x"}, "login"),
    ("linkedin", {"url": "https://www.linkedin.com/authwall?trk=x"}, "login"),
    ("linkedin", {"url": "https://www.linkedin.com/checkpoint/challenge/x"}, "checkpoint"),
    ("linkedin", {"url": "https://www.linkedin.com/search/results/content/", "title": "Let's do a quick security check"}, "checkpoint"),
    ("tiktok", {"url": "https://www.tiktok.com/search?q=x", "text": "results", "logged_in": False}, "login"),
])
def test_login_walls_captchas_checkpoints_and_rate_limits_stop(platform: str, snapshot: dict[str, Any], kind: str) -> None:
    block = detect_block(platform, snapshot)
    assert block is not None and block.kind == kind


def test_a_result_page_is_not_a_block() -> None:
    for platform, page in (("tiktok", TIKTOK_SEARCH), ("instagram", INSTAGRAM_TAG), ("linkedin", LINKEDIN_POSTS)):
        assert detect_block(platform, page) is None


# --- queries ----------------------------------------------------------------------------------


def context(text: str = LAND, vertical: str = "real_estate") -> QueryContext:
    plan = plan_campaign(text, vertical=vertical)  # type: ignore[arg-type]
    return QueryContext.from_campaign(SimpleNamespace(plan=plan, source_text=text))


def test_query_normalisation_and_identity() -> None:
    assert normalise_query("tiktok", "hashtag", "#Terreno Madrid!").text == "terrenomadrid"  # type: ignore[union-attr]
    assert query_key("keyword", "Parcela en  VENTA, Móstoles") == query_key("keyword", "parcela en venta mostoles")
    assert query_key("hashtag", "#TerrenoMadrid") == query_key("hashtag", "terrenomadrid")
    for platform, kind, text in (("tiktok", "people", "x"), ("tiktok", "keyword", "https://x.com"),
                                 ("instagram", "keyword", "@someone"), ("instagram", "hashtag", "#ab"),
                                 ("linkedin", "hashtag", "x"), ("tiktok", "keyword", " ")):
        assert normalise_query(platform, kind, text) is None


def test_fallback_builds_hashtags_and_keywords_from_the_task_without_repeats() -> None:
    ctx = context()
    first = fallback_queries(ctx, "tiktok", set(), 8)
    texts = [q.text for q in first]
    assert "terrenomadrid" in texts and "terreno en venta Madrid" in texts and "parcelaenventa" in texts
    assert {q.kind for q in first} == {"hashtag", "keyword"} and len({(q.kind, q.key) for q in first}) == 8
    second = fallback_queries(ctx, "tiktok", {(q.kind, q.key) for q in first}, 8)
    assert not {(q.kind, q.key) for q in first} & {(q.kind, q.key) for q in second}
    linkedin = fallback_queries(context(INVESTORS, "investors"), "linkedin", set(), 30)
    kinds = {q.kind for q in linkedin}
    assert kinds == {"posts", "people", "companies"}
    assert any(q.text == "business angel Madrid" and q.kind == "people" for q in linkedin)


def test_model_output_drift_is_normalised() -> None:
    content = "```json\n" + json.dumps({"queries": [
        {"kind": "hashtag", "text": "#ParcelaEnVenta", "language": "es"},
        {"kind": "search", "text": "terreno urbanizable Madrid", "language": "es"},
        {"kind": "keyword", "text": "https://spam.example", "language": "en"},
        "#участокмадрид",
    ]}) + "\n```"
    queries = parse_queries("instagram", content)
    assert [(q.kind, q.text) for q in queries] == [
        ("hashtag", "parcelaenventa"), ("keyword", "terreno urbanizable Madrid"), ("hashtag", "участокмадрид")]


async def test_openrouter_generator_uses_the_strict_schema_then_json_mode() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": "no structured outputs"})
        content = json.dumps({"queries": [{"kind": "people", "text": "business angel Madrid", "language": "es"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    generator = OpenRouterQueryGenerator(api_key="k", model="m", timeout_seconds=5,
                                         client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    queries = await generator.generate(context(INVESTORS, "investors"), "linkedin", ["inversores Madrid"], 4)
    assert [(q.kind, q.text) for q in queries] == [("people", "business angel Madrid")]
    strict = bodies[0]["response_format"]["json_schema"]
    assert strict["strict"] is True and strict["schema"]["properties"]["queries"]["items"]["properties"]["kind"]["enum"] == [
        "posts", "people", "companies"]
    data = json.loads(bodies[0]["messages"][1]["content"].split("\n", 1)[1])
    assert data["used"] == ["inversores Madrid"] and data["mode"] == "investors" and "инвесторов" in data["task"]
    assert bodies[1]["response_format"] == {"type": "json_object"}
    await generator.aclose()


class FakeGenerator:
    def __init__(self, *rounds: list[SocialQuery], fail: bool = False) -> None:
        self.rounds, self.fail, self.calls = list(rounds), fail, []

    async def generate(self, context: QueryContext, platform: str, used: list[str], count: int) -> list[SocialQuery]:
        self.calls.append(list(used))
        if self.fail:
            raise RuntimeError("model down")
        return self.rounds.pop(0) if self.rounds else []


def q(platform: str, kind: str, text: str) -> SocialQuery:
    query = normalise_query(platform, kind, text, "es")
    assert query is not None
    return query


async def test_planner_drops_used_queries_and_tops_up_from_the_fallback() -> None:
    model = FakeGenerator([q("tiktok", "hashtag", "terrenomadrid"), q("tiktok", "hashtag", "TerrenoMadrid"),
                           q("tiktok", "keyword", "vendo parcela Madrid")])
    used = {("keyword", query_key("keyword", "vendo parcela Madrid"))}
    queries = await QueryPlanner(model).round(context(), "tiktok", used, ["vendo parcela Madrid"], 3)
    assert next(q.text for q in queries) == "terrenomadrid" and len(queries) == 3
    assert len({(q.kind, q.key) for q in queries}) == 3 and not {(q.kind, q.key) for q in queries} & used
    failing = await QueryPlanner(FakeGenerator(fail=True)).round(context(), "tiktok", set(), [], 2)
    assert len(failing) == 2  # the model is down: the deterministic fallback still gives a round


# --- the worker -------------------------------------------------------------------------------


class FakeBrowser:
    """Serves fixture snapshots by URL; records every lease and navigation."""

    def __init__(self, pages: dict[str, dict[str, Any]], default: dict[str, Any] | None = None) -> None:
        self.pages, self.default = pages, default
        self.visits: list[tuple[str, int]] = []
        self.leases: list[tuple[str, str]] = []  # (profile, platform)
        self.released: list[str] = []
        self.busy = False
        self.open = 0

    async def acquire(self, profile_id: str, profile_name: str, persisted_state: str, *, platform: str = "facebook") -> str:
        if self.busy:
            raise _Busy()  # like aiohttp's ClientResponseError(status=409) from the Browser Session Manager
        assert self.open == 0, "two leases at once"
        self.open += 1
        self.leases.append((profile_id, platform))
        return f"lease:{profile_id}"

    async def snapshot(self, lease: str, url: str, timeout_ms: int, *, scrolls: int = 0) -> dict[str, Any]:
        self.visits.append((url, scrolls))
        for prefix, page in self.pages.items():
            if url.startswith(prefix):
                return page
        if self.default is None:
            raise AssertionError(f"unexpected navigation {url}")
        return {**self.default, "url": url}

    async def release(self, lease: str, next_state: str = "READY") -> None:
        self.open -= 1
        self.released.append(next_state)


class _Busy(Exception):
    status = 409


class Sleeper:
    def __init__(self) -> None:
        self.pauses: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.pauses.append(seconds)


def world(pages: dict[str, dict[str, Any]], *, platforms: tuple[str, ...] = ("tiktok",), default: dict[str, Any] | None = None,
          generator: FakeGenerator | None = None, **config: Any):
    clock = Clock()
    campaigns = MemoryCampaignStore()
    store = MemorySocialStore(now=clock)
    browser = FakeBrowser(pages, default)
    sleeper = Sleeper()
    worker = SocialSearchWorker(store, campaigns, browser, QueryPlanner(generator), SocialConfig(platforms=platforms, **config),
                                now=clock, sleep=sleeper)
    return clock, campaigns, store, browser, sleeper, worker


async def campaign(campaigns: MemoryCampaignStore, store: MemorySocialStore, text: str = LAND,
                   vertical: str = "real_estate") -> str:
    plan = plan_campaign(text, vertical=vertical)  # type: ignore[arg-type]
    cid = await campaigns.create(plan, chat_id=-1, requested_by=7, source_text=text, actor="t")
    store.campaigns[cid] = "planned"
    return cid


RESULTS = {"https://www.tiktok.com/": TIKTOK_SEARCH}


async def test_social_search_is_off_by_default() -> None:
    assert SocialConfig().platforms == () and parse_platforms("") == ()
    assert parse_platforms(" TikTok, linkedin tiktok") == ("tiktok", "linkedin")
    with pytest.raises(ValueError):
        parse_platforms("tiktok,myspace")
    _clock, campaigns, store, browser, _, worker = world(RESULTS, platforms=())
    await campaign(campaigns, store)
    store.add_profile("tiktok")
    assert not worker.enabled and await worker.tick() == 0 and browser.visits == [] and store.queries == []


async def test_a_query_files_new_posts_for_the_campaign_and_frees_everything() -> None:
    _clock, campaigns, store, browser, _, worker = world(RESULTS, generator=FakeGenerator(
        [q("tiktok", "keyword", "terreno en venta Madrid"), q("tiktok", "hashtag", "terrenomadrid")]))
    cid = await campaign(campaigns, store)
    profile = store.add_profile("tiktok")
    assert await worker.tick() == 1
    assert browser.visits == [("https://www.tiktok.com/search?q=terreno+en+venta+Madrid", 2)]
    assert browser.leases == [(profile, "tiktok")] and browser.released == ["READY"] and browser.open == 0
    assert store.profiles[profile]["state"] == "ready"
    assert len(store.posts) == 2 and {c for c, _ in store.links} == {cid}
    first = next(iter(store.posts.values()))
    assert first["title"] == "TikTok · @parcelas.madrid" and first["vertical"] == "real_estate"
    assert store.queries[0].state == "done" and (store.queries[0].found, store.queries[0].new) == (2, 2)
    assert (await store.campaign_social(cid, "tiktok")).state == "pending"


async def test_the_same_post_is_collected_once_across_queries_and_campaigns() -> None:
    clock, campaigns, store, _browser, _, worker = world({}, default=TIKTOK_SEARCH, pause_seconds=20, jitter_seconds=0)
    first = await campaign(campaigns, store)
    store.add_profile("tiktok")
    await worker.tick()
    assert len(store.posts) == 2
    second = await campaign(campaigns, store, "терреno участок Мадрид покупка земли")
    for _ in range(4):  # later queries (both campaigns) show the same two videos
        clock.advance(21)
        await worker.tick()
    assert len(store.posts) == 2 and {c for c, _ in store.links} == {first}
    assert second in {q.campaign_id for q in store.queries if q.state == "done"}
    assert all(q.new == 0 for q in store.queries[1:] if q.state == "done")
    # A post Facebook already collected is never filed again either.
    assert (await store.seen("tiktok", adapter_for("tiktok").parse(TIKTOK_SEARCH, "keyword"))) == {
        f"video:{T0}", "video:7301234567890123999"}


async def test_rounds_never_repeat_a_query_and_stop_after_max_rounds() -> None:
    clock, campaigns, store, browser, _, worker = world({}, default={**TIKTOK_SEARCH, "items": []},
                                                        pause_seconds=20, jitter_seconds=0, queries_per_round=2, max_rounds=3)
    cid = await campaign(campaigns, store)
    store.add_profile("tiktok")
    for _ in range(10):
        await worker.tick()
        clock.advance(21)
    ran = [(q.query.kind, q.query.key) for q in store.queries if q.campaign_id == cid]
    assert len(ran) == 6 and len(set(ran)) == 6 and len(browser.visits) == 6
    assert sorted({q.round_no for q in store.queries}) == [1, 2, 3]
    assert (await store.campaign_social(cid, "tiktok")).state == "done"
    assert await worker.tick() == 0


async def test_a_new_campaign_skips_queries_another_campaign_ran_recently() -> None:
    clock, campaigns, store, _browser, _, worker = world({}, default={**TIKTOK_SEARCH, "items": []},
                                                        pause_seconds=20, jitter_seconds=0, queries_per_round=2, max_rounds=1)
    first = await campaign(campaigns, store)
    store.add_profile("tiktok")
    for _ in range(3):
        await worker.tick()
        clock.advance(21)
    second = await campaign(campaigns, store)
    store.campaigns[first] = "completed"
    for _ in range(3):
        await worker.tick()
        clock.advance(21)
    keys = {cid: {(q.query.kind, q.query.key) for q in store.queries if q.campaign_id == cid} for cid in (first, second)}
    assert keys[first] and keys[second] and not keys[first] & keys[second]


async def test_daily_cap_items_cap_and_paced_queries_with_jitter() -> None:
    clock, campaigns, store, browser, _, worker = world(RESULTS, queries_per_day=2, items_per_query=1,
                                                        pause_seconds=120, jitter_seconds=60)
    cid = await campaign(campaigns, store)
    store.add_profile("tiktok")
    assert await worker.tick() == 1
    assert len(store.posts) == 1  # items_per_query
    pause = (store.platforms["tiktok"].next_action_at - clock()).total_seconds()  # type: ignore[operator]
    assert 120 <= pause <= 180
    assert await worker.tick() == 0  # paced: nothing before the pause is over
    clock.advance(181)
    assert await worker.tick() == 1
    clock.advance(181)
    assert await worker.tick() == 0 and len(browser.visits) == 2  # 2 queries a day
    assert "дневной лимит" in store.social[(cid, "tiktok")]["note"]
    clock.advance(24 * 3600)
    assert await worker.tick() == 1


async def test_instagram_opens_a_capped_number_of_posts_with_pauses() -> None:
    posts = {"https://www.instagram.com/p/": INSTAGRAM_POST, "https://www.instagram.com/reel/": INSTAGRAM_POST,
             "https://www.instagram.com/explore/": INSTAGRAM_TAG}
    _clock, campaigns, store, browser, sleeper, worker = world(posts, platforms=("instagram",), detail_per_query=1,
                                                              generator=FakeGenerator([q("instagram", "hashtag", "terrenomadrid")]))
    await campaign(campaigns, store)
    store.add_profile("instagram")
    await worker.tick()
    assert [url for url, _ in browser.visits] == ["https://www.instagram.com/explore/tags/terrenomadrid/",
                                                 "https://www.instagram.com/p/Cx1AbCdEf/"]
    assert browser.visits[1][1] == 0 and len(sleeper.pauses) == 1 and 4 <= sleeper.pauses[0] <= 9
    saved = [p for p in store.posts.values()]
    assert saved[0]["author"] == "@casas.madrid" and saved[0]["body_text"].startswith("Se vende parcela")
    # The reel tile had no words and could not be opened (cap 1): left unseen for a later search.
    assert len(saved) == 1 and ("instagram", "p:Cy9ZzYyXx") not in store.seen_items


async def test_a_checkpoint_stops_marks_the_profile_and_asks_the_owner_through_verification() -> None:
    checkpoint = {"url": "https://www.linkedin.com/checkpoint/challenge/abc", "title": "Security Verification", "text": ""}
    clock, campaigns, store, browser, _, worker = world({"https://www.linkedin.com/": checkpoint}, platforms=("linkedin",))
    cid = await campaign(campaigns, store, INVESTORS, "investors")
    profile = store.add_profile("linkedin")
    await worker.tick()
    assert browser.released == ["VERIFICATION_REQUIRED"] and browser.open == 0
    assert store.profiles[profile]["state"] == "human_verification_required"
    assert store.jobs == [{"platform": "linkedin", "profile_id": profile, "kind": "checkpoint", "state": "requested"}]
    assert store.queries[0].state == "failed" and store.queries[0].error.startswith("blocked:linkedin_url")
    assert not store.posts
    row = store.social[(cid, "linkedin")]
    assert row["state"] == "waiting" and "владельцу отправлен запрос" in row["note"]
    # Until someone logs in again the platform is skipped: no browser, an owner note only.
    clock.advance(3600)
    assert await worker.tick() == 0 and len(browser.visits) == 1
    assert "/login linkedin" in store.social[(cid, "linkedin")]["note"]


async def test_a_login_wall_on_a_post_page_also_stops_the_query() -> None:
    wall = {"url": "https://www.instagram.com/accounts/login/?next=/p/Cx1AbCdEf/", "text": ""}
    pages = {"https://www.instagram.com/p/": wall, "https://www.instagram.com/explore/": INSTAGRAM_TAG}
    _clock, campaigns, store, _browser, _, worker = world(pages, platforms=("instagram",),
                                                        generator=FakeGenerator([q("instagram", "hashtag", "terrenomadrid")]))
    await campaign(campaigns, store)
    profile = store.add_profile("instagram")
    await worker.tick()
    assert store.profiles[profile]["state"] == "human_verification_required" and store.jobs[0]["kind"] == "login"


async def test_a_rate_limit_pauses_the_platform_without_bothering_the_owner() -> None:
    limited = {"url": "https://www.tiktok.com/search?q=x", "title": "", "text": "Too many requests. Try again later."}
    clock, campaigns, store, browser, _, worker = world({"https://www.tiktok.com/": limited},
                                                        rate_limit_cooldown_seconds=3600)
    cid = await campaign(campaigns, store)
    profile = store.add_profile("tiktok")
    await worker.tick()
    assert store.profiles[profile]["state"] == "ready" and store.jobs == [] and browser.released == ["READY"]
    assert store.platforms["tiktok"].paused_until == clock() + timedelta(seconds=3600)
    clock.advance(1800)
    assert await worker.tick() == 0 and "пауза" in store.social[(cid, "tiktok")]["note"]
    clock.advance(1801)
    browser.pages = RESULTS
    assert await worker.tick() == 1


async def test_no_ready_profile_skips_the_platform_with_an_owner_note_only() -> None:
    _clock, campaigns, store, browser, _, worker = world(RESULTS, platforms=("instagram", "tiktok"))
    cid = await campaign(campaigns, store)
    store.add_profile("tiktok")
    store.add_profile("instagram", state="human_verification_required")
    assert await worker.tick() == 1
    assert [p for p, _ in browser.leases] == ["profile-tiktok-1"]
    note = store.social[(cid, "instagram")]
    assert note["state"] == "waiting" and note["note"] == "Instagram: нет готового профиля — войдите через /login instagram"


async def test_a_busy_browser_gives_the_query_back() -> None:
    _clock, campaigns, store, browser, _, worker = world(RESULTS)
    await campaign(campaigns, store)
    profile = store.add_profile("tiktok")
    browser.busy = True
    await worker.tick()
    assert store.queries[0].state == "planned" and store.queries[0].started_at is None
    assert store.profiles[profile]["state"] == "ready" and await store.queries_today("tiktok") == 0


async def test_finished_campaigns_are_not_searched_and_a_crash_is_recovered() -> None:
    _clock, campaigns, store, browser, _, worker = world(RESULTS)
    cid = await campaign(campaigns, store)
    profile = store.add_profile("tiktok")
    store.campaigns[cid] = "cancelled"
    assert await worker.tick() == 0 and browser.visits == []
    store.campaigns[cid] = "running"
    await store.add_queries(cid, "tiktok", 1, [q("tiktok", "hashtag", "terrenomadrid")])
    await store.claim_profile(profile)
    await store.start_query("q1", profile)
    assert await store.recover() == 1
    assert store.profiles[profile]["state"] == "ready" and store.queries[0].error == "worker_restarted"


# --- the campaign runner waits for social search, bounded -------------------------------------


async def test_the_campaign_completes_after_pending_social_queries_or_the_grace() -> None:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    clock = Clock()
    runner = CampaignRunner(campaigns, store, FakeMessenger(), FakeDiscovery(campaigns, store, 5), now=clock,
                            config=RunnerConfig(window_cooldown_seconds=30, analysis_grace_seconds=0, social_grace_seconds=900))
    plan = plan_campaign("Найди квартиры в аренду в Мадриде")
    cid = await campaigns.create(plan, chat_id=-1, requested_by=7, source_text="x", actor="t")
    await runner.tick()
    store.finish_batch(next(b for _, b, s in store.windows[cid] if s == "active"))
    await runner.tick()
    store.social[cid] = SocialActivity(searching=None, pending=True)
    clock.advance(31)
    await runner.tick()  # queue exhausted: drain starts, social search still has queries
    clock.advance(600)
    await runner.tick()
    assert (await campaigns.get(cid)).state == "running"
    store.social[cid] = SocialActivity(pending=False)
    await runner.tick()
    assert (await campaigns.get(cid)).state == "completed"

    # The grace bounds the wait: queries that never finish do not keep a campaign open.
    cid2 = await campaigns.create(plan, chat_id=-1, requested_by=7, source_text="x", actor="t")
    store.social[cid2] = SocialActivity(pending=True)
    await runner.tick()
    store.finish_batch(next(b for _, b, s in store.windows[cid2] if s == "active"))
    await runner.tick()
    clock.advance(31)
    await runner.tick()
    assert (await campaigns.get(cid2)).state == "running"
    clock.advance(901)
    await runner.tick()
    assert (await campaigns.get(cid2)).state == "completed"
