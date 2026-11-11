"""P1.2 (Instagram) and P1.3 (TikTok) through Apify against a mocked Apify API (``httpx.MockTransport``, no live call).

SYNTHETIC fixtures: every dataset row below is made up from the field names of the actors' output schemas
(``apify_facts`` of 2026-11-11, ``docs/v3/ACTORS.md``), not copied from a run. Handles, names, ids and texts are
invented; there is no real person in them. A field the facts do not list is not used (marked where it matters).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from bot.acquisition.apify_client import ApifyHttp
from bot.acquisition.apify_instagram import (
    COMMENTS_INPUT_KEYS as IG_COMMENTS_KEYS,
)
from bot.acquisition.apify_instagram import (
    POSTS_INPUT_KEYS as IG_POSTS_KEYS,
)
from bot.acquisition.apify_instagram import (
    ApifyInstagramComments,
    ApifyInstagramPosts,
    comments_input,
    instagram_comment,
    instagram_post,
    instagram_profile,
    post_input,
)
from bot.acquisition.apify_social import LOGIN_KEYS, SocialPost, SocialSourceConfig, check_input
from bot.acquisition.apify_source import MemorySourceRunStore
from bot.acquisition.people import Engagement
from bot.campaign.settings import CampaignRunnerSettings
from bot.utils import costs

TOKEN = "apify_api_SECRETtoken123"
CAMPAIGN = "00000000-0000-0000-0000-000000000002"
IG = SocialSourceConfig(platform="instagram", posts_actor="apify/instagram-scraper",
                        comments_actor="apify/instagram-comment-scraper", build="0.3.1", max_results=30,
                        max_charge_usd=0.20, build_setting="APIFY_INSTAGRAM_ACTOR_BUILD")
IG_POST = "https://www.instagram.com/p/C1aBcDeFgHi/"

# --- synthetic rows (field names from the output schemas; values invented) -----------------------------------


def ig_post_row(**kw: Any) -> dict[str, Any]:
    return {"url": IG_POST, "shortCode": "C1aBcDeFgHi",
            "caption": "Se vende chalet en Marbella 🌴 350.000 € · 180 m2 construidos, parcela 600 m2. #marbella",
            "hashtags": ["marbella"], "mentions": [], "timestamp": "2026-10-20T09:30:00.000Z", "type": "Image",
            "ownerUsername": "casas_demo_costa", "ownerFullName": "Casas Demo Costa", "ownerId": "1000000001",
            "commentsCount": 2, "likesCount": 14, "locationName": "Marbella", "locationId": "200000001",
            "images": [], "displayUrl": "https://scontent.cdninstagram.com/x.jpg",
            "latestComments": [{"text": "precio?", "ownerUsername": "demo_lector",
                                "owner": {"username": "demo_lector", "id": "1000000002"}, "likesCount": 0,
                                "timestamp": "2026-10-20T10:00:00.000Z"}],
            "taggedUsers": [], "coauthorProducers": [], **kw}


def ig_comment_row(n: int = 1, **kw: Any) -> dict[str, Any]:
    return {"id": f"1790000000000000{n}", "text": f"Me interesa, ¿sigue disponible? {n}",
            "timestamp": "2026-10-21T08:00:00.000Z", "ownerUsername": f"Demo.Inversor_{n}",
            "owner": {"id": f"300000000{n}", "username": f"Demo.Inversor_{n}", "is_verified": False,
                      "profile_pic_url": "https://scontent.cdninstagram.com/p.jpg"},
            "likesCount": 1, "commentUrl": f"{IG_POST}c/1790000000000000{n}/", "postUrl": IG_POST,
            "inputUrl": IG_POST, **kw}


# --- a scripted Apify API -------------------------------------------------------------------------------------


class Api:
    """Launch answers in order (an int is a refusal status; the last repeats), one SUCCEEDED run, the dataset."""

    def __init__(self, *, launches: list[int] | None = None, dataset: list[Any] | None = None) -> None:
        self.answers, self.dataset = launches or [201], dataset or []
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            status = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
            if status != 201:
                return httpx.Response(status, json={"error": {"type": "rate-limit-exceeded"}})
            return httpx.Response(201, json={"data": {"id": "RUN1", "defaultDatasetId": "DS1", "status": "READY"}})
        if "/actor-runs/RUN1" in path:
            return httpx.Response(200, json={"data": {"id": "RUN1", "status": "SUCCEEDED", "defaultDatasetId": "DS1",
                                                      "usageTotalUsd": 0.04}})
        if "/datasets/DS1/items" in path:
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            return httpx.Response(200, json=self.dataset[offset:offset + limit])
        return httpx.Response(404)

    def launches(self) -> list[httpx.Request]:
        return [r for r in self.calls if r.method == "POST" and r.url.path.endswith("/runs")]

    def body(self) -> dict[str, Any]:
        return json.loads(self.launches()[-1].content)


class Clock:
    """A fake monotonic clock that ``sleep`` advances (no real wait)."""

    def __init__(self) -> None:
        self.now, self.sleeps = 1000.0, []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Saved:
    def __init__(self) -> None:
        self.items: list[Any] = []

    async def __call__(self, items: list[Any], run_id: str) -> int:
        assert run_id == "RUN1"
        self.items += items
        return len(items)


def http(api: Api) -> ApifyHttp:
    async def no_sleep(_: float) -> None:
        return None

    return ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(api)), sleep=no_sleep,
                     jitter=lambda: 0, retries=1)


def build(cls, api: Api, config: SocialSourceConfig, store: MemorySourceRunStore | None = None,
          clock: Clock | None = None):
    clock = clock or Clock()
    return cls(store or MemorySourceRunStore(clock=clock), http(api), config, clock=clock, sleep=clock.sleep)


@pytest.fixture
def ledger():
    sink = costs.MemoryLedger()
    costs.install(sink, budget_usd=5)
    try:
        yield sink
    finally:
        costs.install(None)


def keys_anywhere(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys_anywhere(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys_anywhere(v)}
    return set()


def assert_no_login_keys(request: httpx.Request) -> None:
    found = {k.lower() for k in keys_anywhere(json.loads(request.content))} | {k.lower() for k in request.url.params}
    for word in (*LOGIN_KEYS, "cookies", "headers", "token", "password", "login"):
        assert not any(word.lower() in key for key in found), word
    assert TOKEN not in str(request.url) and TOKEN not in request.content.decode()


def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> CampaignRunnerSettings:
    for key in ("APIFY_TOKEN", "APIFY_INSTAGRAM_ENABLED", "APIFY_INSTAGRAM_ACTOR_BUILD", "APIFY_TIKTOK_ENABLED",
                "APIFY_TIKTOK_ACTOR_BUILD", "APIFY_FACEBOOK_COMMENTS_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    return CampaignRunnerSettings(_env_file=None, DATABASE_URL="postgresql://x/y", TELEGRAM_TOKEN="t", **env)


# --- Instagram: the input -------------------------------------------------------------------------------------


async def test_instagram_posts_launch_body_is_the_white_list_with_cap_and_build(ledger) -> None:
    api = Api(dataset=[])
    await build(ApifyInstagramPosts, api, IG).run(CAMPAIGN, search="#marbella", save=Saved())
    (launch,) = api.launches()
    assert launch.url.path == "/v2/acts/apify~instagram-scraper/runs"
    assert api.body() == {"search": "#marbella", "resultsLimit": 30}  # as planned: how the actor reads «#» is P1.7
    assert set(api.body()) <= IG_POSTS_KEYS
    params = launch.url.params
    assert (params["maxTotalChargeUsd"], params["build"], params["maxItems"]) == ("0.2", "0.3.1", "30")
    assert_no_login_keys(launch)


async def test_instagram_comments_launch_body_is_the_white_list(ledger) -> None:
    api = Api(dataset=[])
    await build(ApifyInstagramComments, api, IG).run(CAMPAIGN, [IG_POST], save=Saved())
    (launch,) = api.launches()
    assert launch.url.path == "/v2/acts/apify~instagram-comment-scraper/runs"
    assert api.body() == {"directUrls": [IG_POST], "resultsLimit": 30, "includeNestedComments": False}
    assert set(api.body()) <= IG_COMMENTS_KEYS
    assert (launch.url.params["maxTotalChargeUsd"], launch.url.params["build"]) == ("0.2", "0.3.1")
    assert_no_login_keys(launch)


def test_instagram_input_refuses_what_is_not_a_public_target() -> None:
    assert post_input(direct_urls=[IG_POST], max_results=5) == {"directUrls": [IG_POST], "resultsLimit": 5}
    with pytest.raises(ValueError, match="not both"):
        post_input(search="marbella", direct_urls=[IG_POST], max_results=5)
    with pytest.raises(ValueError, match="not both"):
        post_input(max_results=5)
    for bad in ("https://evil.example/p/C1aBcDeFgHi/", "http://www.instagram.com/p/C1aBcDeFgHi/",
                "https://user:pw@www.instagram.com/p/C1aBcDeFgHi/"):
        with pytest.raises(ValueError, match="public link"):
            comments_input([bad], max_results=5)
    with pytest.raises(ValueError, match="links per run"):
        comments_input([IG_POST] * 11, max_results=5)


@pytest.mark.parametrize("body", [
    {"search": "x", "initialCookies": []},
    {"search": "x", "resultsLimit": 1, "proxy": {"customHttpHeaders": {"Cookie": "c"}}},
])
def test_an_input_key_off_the_white_list_or_a_login_key_is_refused(body) -> None:
    with pytest.raises(ValueError):
        check_input(body, IG_POSTS_KEYS)
    with pytest.raises(ValueError, match="login"):
        check_input({"directUrls": [{"url": IG_POST, "session": "s"}]}, IG_POSTS_KEYS)


# --- Instagram: mapping ---------------------------------------------------------------------------------------


def test_instagram_post_maps_text_link_author_and_guesses_no_fact() -> None:
    mapped = instagram_post(ig_post_row(), campaign_id=CAMPAIGN, actor_id=IG.posts_actor, run_id="RUN1")
    assert isinstance(mapped, SocialPost)
    post = mapped.post
    assert post.raw_text.startswith("Se vende chalet en Marbella") and "350.000 €" in post.raw_text
    # price, house area and plot area are in the caption only: the analysis reads them, the mapper does not
    assert post.provider_facts.model_dump(exclude_defaults=True) == {} and post.quotes == {}
    assert (post.source.platform, post.source.provider, post.source.actor_id, post.source.run_id) == (
        "instagram", "apify", "apify/instagram-scraper", "RUN1")
    assert post.source.post_url == IG_POST and post.platform_post_id() == "C1aBcDeFgHi"
    assert post.source.published_at is not None and post.source.published_at.isoformat() == "2026-10-20T09:30:00+00:00"
    assert post.source.group_url == "https://www.instagram.com/casas_demo_costa"
    assert mapped.author is not None and (mapped.author.profile_key, mapped.author.name) == (
        "ig:casas_demo_costa", "Casas Demo Costa")
    row = post.collected_post_row()
    assert row["raw_payload"]["platform"] == "instagram" and row["raw_payload"]["provider_facts"] == {}


def test_instagram_post_without_caption_or_off_instagram_is_skipped() -> None:
    kw = {"campaign_id": CAMPAIGN, "actor_id": IG.posts_actor, "run_id": "RUN1"}
    assert instagram_post(ig_post_row(caption=None), **kw) is None
    assert instagram_post(ig_post_row(caption="   "), **kw) is None
    assert instagram_post(ig_post_row(url="https://evil.example/p/C1aBcDeFgHi/"), **kw) is None
    no_author = instagram_post(ig_post_row(ownerUsername=None, ownerFullName=None), **kw)
    assert no_author is not None and no_author.author is None and no_author.post.source.group_url == IG_POST


def test_instagram_comment_maps_the_commenter_from_owner_username() -> None:
    comment = instagram_comment(ig_comment_row(1))
    assert isinstance(comment, Engagement)
    assert (comment.platform, comment.kind, comment.profile_key, comment.profile_url) == (
        "instagram", "comment", "ig:demo.inversor_1", "https://www.instagram.com/demo.inversor_1")
    assert comment.author is None  # the actor gives no name (full_name is null), a username is not a name
    assert (comment.text, comment.post_url, comment.raw_id) == (
        "Me interesa, ¿sigue disponible? 1", IG_POST, "17900000000000001")
    from_owner = instagram_comment(ig_comment_row(2, ownerUsername=None))
    assert from_owner is not None and from_owner.profile_key == "ig:demo.inversor_2"


@pytest.mark.parametrize("change", [
    {"ownerUsername": "evil.example/x", "owner": {}},           # not a username: no profile on another path
    {"ownerUsername": None, "owner": {}},
    {"text": ""},
    {"postUrl": "https://evil.example/p/C1aBcDeFgHi/", "inputUrl": None},  # the post on another host
])
def test_instagram_comment_without_a_profile_text_or_instagram_post_is_skipped(change) -> None:
    assert instagram_comment(ig_comment_row(1, **change)) is None
    assert instagram_profile("p") is None  # a reserved path is not a profile


# --- Instagram: runs ------------------------------------------------------------------------------------------


async def test_instagram_posts_run_saves_mapped_posts(ledger) -> None:
    api, saved = Api(dataset=[ig_post_row(), ig_post_row(caption=None)]), Saved()
    result = await build(ApifyInstagramPosts, api, IG).run(CAMPAIGN, direct_urls=[IG_POST], save=saved)
    assert (result.outcome.status, result.kept, result.error_rows, result.error) == ("succeeded", 1, 0, None)
    assert [p.post.campaign_id for p in saved.items] == [CAMPAIGN]


async def test_instagram_comments_error_row_is_post_unavailable(ledger) -> None:
    error = {"url": IG_POST, "inputUrl": IG_POST, "error": "not_found",
             "errorDescription": "Post not found or private", "requestErrorMessages": []}
    api, saved = Api(dataset=[error]), Saved()
    result = await build(ApifyInstagramComments, api, IG).run(CAMPAIGN, [IG_POST], save=saved)
    assert (result.outcome.status, result.kept, result.error_rows, result.error) == (
        "succeeded", 0, 1, "post_unavailable")
    assert saved.items == []
    mixed = Api(dataset=[error, ig_comment_row(1)])
    other = await build(ApifyInstagramComments, mixed, IG).run(CAMPAIGN, [IG_POST, IG_POST + "x/"], save=Saved())
    assert (other.kept, other.error_rows, other.error) == (1, 1, None)  # one post unreadable, the other has a comment


async def test_instagram_empty_dataset_is_a_successful_run_with_nothing(ledger) -> None:
    saved = Saved()
    result = await build(ApifyInstagramComments, Api(dataset=[]), IG).run(CAMPAIGN, [IG_POST], save=saved)
    assert (result.outcome.status, result.kept, result.error) == ("succeeded", 0, None)
    assert saved.items == []


# --- 429: not an attempt within the window, bounded in time ---------------------------------------------------


async def test_instagram_429_does_not_spend_an_attempt_within_the_window(ledger) -> None:
    api, clock = Api(launches=[429, 429, 201], dataset=[ig_comment_row(1)]), Clock()
    store = MemorySourceRunStore(clock=clock)
    result = await build(ApifyInstagramComments, api, IG, store, clock).run(CAMPAIGN, [IG_POST], save=Saved())
    assert (result.outcome.status, result.kept) == ("succeeded", 1)
    assert len(api.launches()) == 3 and clock.sleeps == [15.0, 15.0]
    (row,) = store.rows.values()
    assert row["attempts"] == 1  # the two refusals were not attempts


async def test_instagram_429_past_the_window_spends_one_attempt_and_stops_after_max(ledger) -> None:
    api, clock = Api(launches=[429]), Clock()
    store = MemorySourceRunStore(clock=clock)
    source = build(ApifyInstagramComments, api, IG, store, clock)
    first = await source.run(CAMPAIGN, [IG_POST], save=Saved())
    assert (first.outcome.status, first.outcome.error) == ("retry", "apify_http_429")
    assert sum(clock.sleeps) == 120.0  # waited the whole window, no longer
    per_call = len(api.launches())
    assert per_call == 9  # 120 s / 15 s pauses: 8 free refusals and the one that spends the attempt
    (row,) = store.rows.values()
    assert (row["attempts"], row["status"], row["run_id"]) == (1, "claimed", None)
    for _ in range(4):
        await source.run(CAMPAIGN, [IG_POST], save=Saved())
    assert (row["attempts"], row["status"], row["error_code"]) == (3, "failed", "max_attempts")
    assert len(api.launches()) == 3 * per_call  # bounded: max_attempts windows, then never again
    assert sum(clock.sleeps) == 3 * 120.0
    assert [e for e in ledger.entries if e.cost_usd] == []  # refused launches cost nothing


async def test_429_stays_an_attempt_when_the_window_is_off(ledger) -> None:
    from dataclasses import replace

    from bot.acquisition.apify_source import ApifySocialRun

    api, clock = Api(launches=[429]), Clock()
    store = MemorySourceRunStore(clock=clock)
    config = replace(IG.run_config("comments"), rate_limit_seconds=0)
    runner = ApifySocialRun(store, http(api), config, clock=clock, sleep=clock.sleep)
    outcome = await runner.run(CAMPAIGN, comments_input([IG_POST], max_results=30), Saved())
    assert outcome.status == "retry" and len(api.launches()) == 1 and clock.sleeps == []
    (row,) = store.rows.values()
    assert row["attempts"] == 1


# --- Instagram: settings --------------------------------------------------------------------------------------


def test_instagram_flag_off_builds_no_source(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(monkeypatch)
    assert s.apify_instagram_enabled is False and s.instagram_config() is None
    assert s.instagram_sources(MemorySourceRunStore(), http(Api())) is None
    assert (s.apify_instagram_posts_actor, s.apify_instagram_comments_actor, s.apify_instagram_max_results,
            s.apify_instagram_max_charge_usd) == ("apify/instagram-scraper", "apify/instagram-comment-scraper", 30, 0.20)


@pytest.mark.parametrize(("env", "message"), [
    ({}, "APIFY_INSTAGRAM_ENABLED=true needs APIFY_TOKEN, APIFY_INSTAGRAM_ACTOR_BUILD"),
    ({"APIFY_TOKEN": "x"}, "needs APIFY_INSTAGRAM_ACTOR_BUILD"),
    ({"APIFY_TOKEN": "x", "APIFY_INSTAGRAM_ACTOR_BUILD": "latest"}, "APIFY_INSTAGRAM_ACTOR_BUILD must be a pinned"),
    ({"APIFY_TOKEN": "x", "APIFY_INSTAGRAM_ACTOR_BUILD": "1.2.3", "APIFY_INSTAGRAM_MAX_RESULTS": "51"}, "less than"),
    ({"APIFY_TOKEN": "x", "APIFY_INSTAGRAM_ACTOR_BUILD": "1.2.3", "APIFY_INSTAGRAM_MAX_CHARGE_USD": "0"}, "greater"),
])
def test_instagram_flag_on_without_what_it_needs_does_not_start(monkeypatch: pytest.MonkeyPatch, env, message) -> None:
    with pytest.raises(ValidationError, match=message):
        settings(monkeypatch, APIFY_INSTAGRAM_ENABLED="true", **env)


def test_instagram_flag_on_builds_both_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(monkeypatch, APIFY_INSTAGRAM_ENABLED="true", APIFY_TOKEN="apify_api_SECRET",
                 APIFY_INSTAGRAM_ACTOR_BUILD="0.3.1")
    sources = s.instagram_sources(MemorySourceRunStore(), http(Api()))
    assert sources is not None
    assert (sources.posts.runner.config.actor_id, sources.posts.runner.config.purpose) == (
        "apify/instagram-scraper", "ig:posts")
    assert (sources.comments.runner.config.actor_id, sources.comments.runner.config.purpose) == (
        "apify/instagram-comment-scraper", "ig:comments")
    cfg = sources.posts.runner.config
    assert (cfg.build, cfg.max_items, cfg.max_charge_usd, cfg.rate_limit_seconds) == ("0.3.1", 30, 0.20, 120.0)
    assert "apify_api_SECRET" not in repr(s) and "apify_api_SECRET" not in repr(sources)
