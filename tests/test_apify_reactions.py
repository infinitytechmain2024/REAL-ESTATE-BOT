"""P1.6: people who reacted under Facebook posts (``apify/facebook-likes-scraper``) against a mocked Apify API.

No live calls: every answer comes from ``httpx.MockTransport``. The rows are placeholders shaped like the actor's
documented output (``name``, ``profileUrl``, ``facebookId``, ``facebookUrl``, ``reaction``, ``url``, ``inputUrl``,
``error``, ``errorDescription``; ``docs/v3/ACTORS.md``), without real people: there is no live sample yet (P1.7).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from bot.acquisition.apify_client import ApifyHttp
from bot.acquisition.apify_facebook_reactions import (
    INPUT_KEYS,
    OTHER,
    REACTION_TYPES,
    USD_PER_RESULT,
    ApifyFacebookReactions,
    PostUrlError,
    ReactionPage,
    ReactionsConfig,
    map_rows,
    normalize_post_url,
    reaction_type,
    split_post_urls,
)
from bot.acquisition.apify_source import MemorySourceRunStore, input_hash
from bot.campaign.settings import CampaignRunnerSettings
from bot.utils import costs

TOKEN = "apify_api_TESTtoken000"
CAMPAIGN = "00000000-0000-0000-0000-000000000001"
POST = "https://www.facebook.com/someagency/posts/pfbid0TEST"
CONFIG = ReactionsConfig(actor_id="apify/facebook-likes-scraper", build="0.0.7", max_results=100, max_charge_usd=0.7)
_COOKIE_LIKE = ("cookie", "session", "token", "password", "secret", "auth", "login", "proxy", "account")


def reaction(n: int, kind: Any = "like", **kw: Any) -> dict[str, Any]:
    return {"name": f"Persona {n}", "profileUrl": f"https://www.facebook.com/profile.php?id=10000000000{n:04d}",
            "facebookId": f"10000000000{n:04d}", "facebookUrl": POST, "reaction": kind, "url": POST,
            "inputUrl": POST, **kw}


class Api:
    def __init__(self, dataset: list[Any]) -> None:
        self.dataset, self.calls = dataset, []
        self.refuse: int | None = None
        self.state = "SUCCEEDED"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            if self.refuse is not None:
                return httpx.Response(self.refuse, json={"error": {"type": "invalid-input"}})
            return httpx.Response(201, json={"data": {"id": "RUN1", "defaultDatasetId": "DS1", "status": "READY"}})
        if "/actor-runs/RUN1" in path:
            return httpx.Response(200, json={"data": {"id": "RUN1", "status": self.state, "defaultDatasetId": "DS1",
                                                      "usageTotalUsd": 0.03}})
        if "/datasets/DS1/items" in path:
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            return httpx.Response(200, json=self.dataset[offset:offset + limit])
        return httpx.Response(404)

    def launch(self) -> httpx.Request:
        (only,) = [r for r in self.calls if r.method == "POST" and r.url.path.endswith("/runs")]
        return only


class Sink:
    def __init__(self) -> None:
        self.pages: list[ReactionPage] = []

    async def __call__(self, page: ReactionPage, run_id: str) -> int:
        assert run_id == "RUN1"
        self.pages.append(page)
        return len(page.engagements)


def source(api: Api, config: ReactionsConfig = CONFIG) -> ApifyFacebookReactions:
    async def no_sleep(_: float) -> None:
        return None

    http = ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(api)), sleep=no_sleep,
                     jitter=lambda: 0, retries=1)
    return ApifyFacebookReactions(MemorySourceRunStore(), http, config)


@pytest.fixture
def ledger():
    sink = costs.MemoryLedger()
    costs.install(sink, budget_usd=5)
    try:
        yield sink
    finally:
        costs.install(None)


def _keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [str(k) for k in value] + [k for v in value.values() for k in _keys(v)]
    if isinstance(value, list):
        return [k for v in value for k in _keys(v)]
    return []


# --- the launch ------------------------------------------------------------------------------------------------


async def test_launch_body_is_the_white_list_with_the_cap_and_pinned_build(ledger) -> None:
    api = Api([reaction(1)])
    outcome = await source(api).collect(CAMPAIGN, [POST, f" {POST} "], Sink())
    assert outcome.status == "succeeded" and outcome.items_imported == 1
    launch = api.launch()
    body = json.loads(launch.content)
    assert body == {"startUrls": [{"url": POST}], "resultsLimit": 100}
    assert set(body) <= INPUT_KEYS
    assert not [k for k in _keys(body) if any(word in k.lower() for word in _COOKIE_LIKE)]
    assert TOKEN not in launch.content.decode()
    params = launch.url.params
    assert params["maxTotalChargeUsd"] == "0.7" and params["maxItems"] == "100" and params["build"] == "0.0.7"
    assert "acts/apify~facebook-likes-scraper/runs" in launch.url.path


async def test_cap_from_the_config_reaches_the_launch(ledger) -> None:
    api = Api([])
    config = ReactionsConfig(actor_id="apify/facebook-likes-scraper", build="0.0.7", max_results=10,
                             max_charge_usd=0.07)
    await source(api, config).collect(CAMPAIGN, [POST], Sink())
    launch = api.launch()
    assert launch.url.params["maxTotalChargeUsd"] == "0.07" and launch.url.params["maxItems"] == "10"
    assert json.loads(launch.content)["resultsLimit"] == 10


def test_only_facebook_posts_are_sent() -> None:
    src = source(Api([]))
    for bad in (["https://www.instagram.com/p/abc/"], ["javascript:alert(1)"], [], ["ftp://facebook.com/x"]):
        with pytest.raises(ValueError):
            src.actor_input(bad)


# --- the mapper ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [
    *[(t, t) for t in REACTION_TYPES], ("LIKE", "like"), (" Love ", "love"), ("laugh", "haha"), ("Laugh", "haha"),
    ("pride", OTHER), ("thankful", OTHER), (None, None), ("", None), ("  ", None), (3, None),
])
def test_reaction_types_are_normalized(raw: Any, expected: str | None) -> None:
    assert reaction_type(raw)[0] == expected
    (engagement,) = map_rows([reaction(1, raw)]).engagements
    assert engagement.kind == "reaction" and engagement.platform == "facebook"
    assert engagement.reaction_type == expected


def test_every_type_maps_and_unknown_words_are_counted() -> None:
    rows = [reaction(n, kind) for n, kind in enumerate([*REACTION_TYPES, "pride"])]
    page = map_rows(rows)
    assert [e.reaction_type for e in page.engagements] == [*REACTION_TYPES, OTHER]
    assert page.unknown_types == {"pride": 1} and not page.skipped and not page.errors


def test_a_row_is_mapped_as_given() -> None:
    (e,) = map_rows([reaction(7, "love", name="  Ana   Pérez ")]).engagements
    assert e.author == "Ana Pérez"
    assert (e.profile_key, e.profile_url) == ("fb:100000000000007",
                                              "https://www.facebook.com/profile.php?id=100000000000007")
    assert e.post_url == POST and e.raw_id == "100000000000007" and e.text is None


def test_vanity_profile_and_missing_name() -> None:
    (e,) = map_rows([reaction(1, name=None, profileUrl="https://m.facebook.com/ana.perez.42?ref=x")]).engagements
    assert e.author is None and e.profile_key == "fb:ana.perez.42"
    assert e.profile_url == "https://www.facebook.com/ana.perez.42"


def test_a_row_without_a_profile_is_dropped_with_its_reason() -> None:
    page = map_rows([reaction(1, profileUrl=None), reaction(2, profileUrl=""),
                     {k: v for k, v in reaction(3).items() if k != "profileUrl"}])
    assert page.engagements == [] and page.skipped == {"no_profile": 3}


def test_the_profile_is_never_built_from_the_id() -> None:
    page = map_rows([{k: v for k, v in reaction(1).items() if k != "profileUrl"}])
    assert page.engagements == [] and page.skipped == {"no_profile": 1}


def test_a_foreign_host_or_a_non_profile_is_dropped() -> None:
    page = map_rows([reaction(1, profileUrl="https://evil.example/profile.php?id=100000000000001"),
                     reaction(2, profileUrl="https://facebook.com.evil.example/ana"),
                     reaction(3, profileUrl="https://www.facebook.com/groups/123456/posts/789"),
                     reaction(4, profileUrl="https://www.facebook.com/photo.php?fbid=1")])
    assert page.engagements == []
    assert page.skipped == {"foreign_host": 2, "not_a_profile": 2}


def test_an_error_row_is_a_post_error_not_a_reaction() -> None:
    page = map_rows([{"inputUrl": POST, "url": POST, "error": "not_available",
                      "errorDescription": "Post is private or was removed"}, reaction(1)])
    (error,) = page.errors
    assert (error.post_url, error.error, error.description) == (POST, "not_available",
                                                                 "Post is private or was removed")
    assert len(page.engagements) == 1 and not page.skipped


def test_an_error_row_with_a_profile_is_still_an_error() -> None:
    page = map_rows([reaction(1, error="blocked")])
    assert page.engagements == [] and [e.error for e in page.errors] == ["blocked"]


def test_no_post_url_duplicates_and_garbage_are_dropped() -> None:
    page = map_rows([reaction(1, inputUrl=None, url=None), reaction(2), reaction(2), "garbage", None])
    assert len(page.engagements) == 1
    assert page.skipped == {"no_post": 1, "duplicate": 1, "not_a_row": 2}


def test_input_url_wins_over_url() -> None:
    other = "https://www.facebook.com/someagency/posts/pfbid0OTHER"
    (e,) = map_rows([reaction(1, url=other)]).engagements
    assert e.post_url == POST
    (e,) = map_rows([reaction(1, inputUrl=None, url=other)]).engagements
    assert e.post_url == other


async def test_empty_dataset_is_a_success_with_nothing(ledger) -> None:
    sink = Sink()
    outcome = await source(Api([])).collect(CAMPAIGN, [POST], sink)
    assert outcome.status == "succeeded" and outcome.items_imported == 0 and sink.pages == []
    assert map_rows([]) == ReactionPage()


async def test_pages_reach_the_sink_mapped_and_cookie_keys_scrubbed(ledger) -> None:
    sink = Sink()
    rows = [reaction(1, "wow", cookies="c_user=1"), reaction(2, profileUrl=None), {"inputUrl": POST, "error": "x"}]
    outcome = await source(Api(rows)).collect(CAMPAIGN, [POST], sink)
    (page,) = sink.pages
    assert [e.reaction_type for e in page.engagements] == ["wow"]
    assert page.skipped == {"no_profile": 1} and len(page.errors) == 1
    assert outcome.items_imported == 1
    assert "cookie" not in repr(page).lower()


# --- settings --------------------------------------------------------------------------------------------------


def _settings(**env: Any) -> CampaignRunnerSettings:
    return CampaignRunnerSettings(_env_file=None, DATABASE_URL="postgresql://x", TELEGRAM_TOKEN="t", **env)


def test_flag_off_builds_nothing(monkeypatch) -> None:
    for name in ("APIFY_FACEBOOK_REACTIONS_ENABLED", "APIFY_FACEBOOK_REACTIONS_ACTOR_BUILD", "APIFY_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    settings = _settings()
    assert settings.apify_reactions_enabled is False and settings.reactions_config() is None
    assert settings.apify_reactions_actor == "apify/facebook-likes-scraper"
    assert (settings.apify_reactions_max_results, settings.apify_reactions_max_charge_usd) == (100, 0.70)


def test_flag_on_builds_the_config() -> None:
    config = _settings(APIFY_FACEBOOK_REACTIONS_ENABLED=True, APIFY_TOKEN="t",
                       APIFY_FACEBOOK_REACTIONS_ACTOR_BUILD=" 0.0.7 ").reactions_config()
    assert isinstance(config, ReactionsConfig)
    social = config.social()
    assert (social.platform, social.purpose, social.build, social.max_items, social.max_charge_usd) == (
        "facebook", "reactions", "0.0.7", 100, 0.70)


@pytest.mark.parametrize("env", [
    {"APIFY_TOKEN": "t"},                                                     # no build
    {"APIFY_TOKEN": "t", "APIFY_FACEBOOK_REACTIONS_ACTOR_BUILD": "  "},
    {"APIFY_TOKEN": "t", "APIFY_FACEBOOK_REACTIONS_ACTOR_BUILD": "latest"},
    {"APIFY_TOKEN": "", "APIFY_FACEBOOK_REACTIONS_ACTOR_BUILD": "0.0.7"},     # no token
])
def test_flag_on_without_a_pinned_build_or_token_does_not_start(env: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        _settings(APIFY_FACEBOOK_REACTIONS_ENABLED=True, **env)


def test_a_cap_below_results_times_price_is_a_settings_error_only_with_the_flag_on() -> None:
    low = {"APIFY_FACEBOOK_REACTIONS_MAX_RESULTS": 200, "APIFY_FACEBOOK_REACTIONS_MAX_CHARGE_USD": 0.70}
    assert 200 * USD_PER_RESULT > 0.70
    off = _settings(APIFY_FACEBOOK_REACTIONS_ENABLED=False, **low)  # ENABLED=false alone always rolls back
    assert off.reactions_config() is None
    on = {"APIFY_FACEBOOK_REACTIONS_ENABLED": True, "APIFY_TOKEN": "t", "APIFY_FACEBOOK_REACTIONS_ACTOR_BUILD": "0.0.7"}
    with pytest.raises(ValidationError, match="APIFY_FACEBOOK_REACTIONS_MAX_CHARGE_USD"):
        _settings(**on, **low)
    _settings(**on, APIFY_FACEBOOK_REACTIONS_MAX_RESULTS=500, APIFY_FACEBOOK_REACTIONS_MAX_CHARGE_USD=3.10)
    with pytest.raises(ValueError, match="max_charge_usd"):
        ReactionsConfig(actor_id="apify/facebook-likes-scraper", build="0.0.7", max_results=100, max_charge_usd=0.5)


@pytest.mark.parametrize("value", [0, 501])
def test_max_results_range(value: int) -> None:
    with pytest.raises(ValidationError):
        _settings(APIFY_FACEBOOK_REACTIONS_MAX_RESULTS=value)


# --- review fixes ----------------------------------------------------------------------------------------------


def test_results_limit_is_shared_between_posts_and_max_items_is_the_run_cap(ledger) -> None:
    src = source(Api([]))
    posts = [f"https://www.facebook.com/someagency/posts/pfbid0P{n}" for n in range(3)]
    assert src.actor_input(posts)["resultsLimit"] == 33
    assert src.actor_input(posts[:1])["resultsLimit"] == 100
    tiny = ReactionsConfig(actor_id="apify/facebook-likes-scraper", build="0.0.7", max_results=2, max_charge_usd=0.02)
    assert source(Api([]), tiny).actor_input(posts)["resultsLimit"] == 1


async def test_a_post_at_the_limit_is_marked_truncated(ledger, caplog) -> None:
    config = ReactionsConfig(actor_id="apify/facebook-likes-scraper", build="0.0.7", max_results=4,
                             max_charge_usd=0.05)
    full, part = POST, "https://www.facebook.com/someagency/posts/pfbid0OTHER"
    rows = [reaction(1, inputUrl=full), reaction(2, inputUrl=full, profileUrl=None),
            reaction(3, inputUrl=part)]
    api, sink = Api(rows), Sink()
    with caplog.at_level("WARNING"):
        outcome = await source(api, config).collect(CAMPAIGN, [full, part], sink)
    assert json.loads(api.launch().content)["resultsLimit"] == 2
    assert api.launch().url.params["maxItems"] == "4"
    (page,) = sink.pages
    assert page.truncated == {full: 2} and outcome.status == "succeeded"
    assert any(r.message == "apify_reactions.truncated" for r in caplog.records)


async def test_truncation_counts_across_pages(ledger) -> None:
    config = ReactionsConfig(actor_id="apify/facebook-likes-scraper", build="0.0.7", max_results=150,
                             max_charge_usd=1.0)
    rows = [reaction(n) for n in range(150)]
    sink = Sink()
    await source(Api(rows), config).collect(CAMPAIGN, [POST], sink)
    assert len(sink.pages) == 2  # page_size 100
    assert [dict(p.truncated) for p in sink.pages] == [{}, {POST: 150}]


@pytest.mark.parametrize("variant", [
    "https://m.facebook.com/someagency/posts/pfbid0TEST?__tn__=x&mibextid=abc",
    "https://www.facebook.com/someagency/posts/pfbid0TEST/",
    "http://web.facebook.com/someagency/posts/pfbid0TEST#comments",
    "https://mbasic.facebook.com/someagency/posts/pfbid0TEST?ref=share",
    " https://facebook.com/someagency/posts/pfbid0TEST ",
])
def test_the_same_post_written_differently_is_one_input(variant: str) -> None:
    src = source(Api([]))
    assert src.actor_input([variant]) == src.actor_input([POST])
    assert input_hash(CONFIG.actor_id, src.actor_input([variant])) == input_hash(CONFIG.actor_id, src.actor_input([POST]))
    assert src.actor_input([variant, POST])["startUrls"] == [{"url": POST}]


def test_post_id_parameters_are_kept() -> None:
    url = "https://m.facebook.com/permalink.php?id=100000000000001&__tn__=x&story_fbid=1234567890"
    assert normalize_post_url(url) == "https://www.facebook.com/permalink.php?id=100000000000001&story_fbid=1234567890"
    assert normalize_post_url("https://www.facebook.com/photo.php?fbid=1234567&set=a.1&type=3") == (
        "https://www.facebook.com/photo.php?fbid=1234567&set=a.1")
    assert normalize_post_url("https://www.facebook.com/groups/1/?multi_permalinks=123456&ref=x") == (
        "https://www.facebook.com/groups/1?multi_permalinks=123456")
    with pytest.raises(ValueError):
        normalize_post_url("https://www.facebook.com/")


async def test_a_refused_launch_books_no_run_cost(ledger) -> None:
    api = Api([])
    api.refuse = 400
    sink = Sink()
    outcome = await source(api).collect(CAMPAIGN, [POST], sink)
    assert outcome.status == "failed" and outcome.error == "apify_http_400" and outcome.cost_usd == 0
    assert not [k for k in ledger.keys if k.startswith("apify:")] and sink.pages == []
    assert len(api.calls) == 1


async def test_a_failed_run_is_billed_once_at_its_usage(ledger) -> None:
    api = Api([reaction(1)])
    api.state = "FAILED"
    sink = Sink()
    outcome = await source(api).collect(CAMPAIGN, [POST], sink)
    assert outcome.status == "failed" and outcome.error == "apify_run_failed" and sink.pages == []
    assert [k for k in ledger.keys if k.startswith("apify:")] == ["apify:RUN1"]
    entry = ledger.entries[ledger.keys["apify:RUN1"]]
    assert entry.cost_usd == pytest.approx(0.03) and entry.code == ""


def test_a_profile_without_a_scheme_has_its_reason() -> None:
    page = map_rows([reaction(1, profileUrl="facebook.com/ana"), reaction(2, profileUrl="www.facebook.com/ana")])
    assert page.engagements == [] and page.skipped == {"no_scheme": 2}


@pytest.mark.parametrize("value", [0, 0.0, False, "", "  ", "false", "0", None])
def test_a_falsy_error_is_no_error(value: Any) -> None:
    page = map_rows([reaction(1, error=value)])
    assert len(page.engagements) == 1 and page.errors == []


@pytest.mark.parametrize(("value", "expected"), [(True, "error"), (404, "404"), ("blocked", "blocked")])
def test_a_real_error_is_a_post_error(value: Any, expected: str) -> None:
    page = map_rows([reaction(1, error=value)])
    assert page.engagements == [] and [e.error for e in page.errors] == [expected]


# --- the post id survives normalization ------------------------------------------------------------------------


def test_watch_videos_with_different_ids_stay_different() -> None:
    a = normalize_post_url("https://m.facebook.com/watch/?v=111&ref=sharing")
    b = normalize_post_url("https://www.facebook.com/watch/?v=222")
    assert (a, b) == ("https://www.facebook.com/watch?v=111", "https://www.facebook.com/watch?v=222")
    src = source(Api([]))
    assert src.actor_input([a, b])["startUrls"] == [{"url": a}, {"url": b}]


def test_video_php_keeps_v_and_media_set_keeps_set() -> None:
    assert normalize_post_url("https://www.facebook.com/video.php?v=5&__tn__=x") == (
        "https://www.facebook.com/video.php?v=5")
    assert normalize_post_url("https://m.facebook.com/media/set/?set=a.1&type=3") == (
        "https://www.facebook.com/media/set?set=a.1")


@pytest.mark.parametrize("url", [
    "https://www.facebook.com/watch", "https://www.facebook.com/watch/?ref=x", "https://www.facebook.com/watch/?v=abc",
    "https://www.facebook.com/someagency", "https://www.facebook.com/", "https://www.facebook.com/groups/123456",
    "https://www.facebook.com/permalink.php?id=100000000000001", "https://www.facebook.com/media/set/?set=",
])
def test_a_link_without_a_post_id_is_rejected(url: str) -> None:
    with pytest.raises(PostUrlError) as exc:
        normalize_post_url(url)
    assert exc.value.reason == "no_post_id"


def test_rejected_links_stay_out_of_the_input() -> None:
    urls = ["https://www.facebook.com/watch", POST, "https://www.instagram.com/p/abc/", "https://www.facebook.com/x"]
    posts, rejected = split_post_urls(urls)
    assert posts == [POST] and rejected == {"no_post_id": 2, "not_facebook": 1}
    assert source(Api([])).actor_input(urls)["startUrls"] == [{"url": POST}]
    with pytest.raises(ValueError):
        source(Api([])).actor_input(["https://www.facebook.com/watch"])


@pytest.mark.parametrize(("url", "expected"), [
    ("https://www.facebook.com/groups/123456/posts/7890123/?__cft__=x", "https://www.facebook.com/groups/123456/posts/7890123"),
    ("https://www.facebook.com/groups/123456/permalink/7890123/", "https://www.facebook.com/groups/123456/permalink/7890123"),
    ("https://www.facebook.com/groups/1/?multi_permalinks=123456&ref=x", "https://www.facebook.com/groups/1?multi_permalinks=123456"),
    ("https://m.facebook.com/permalink.php?story_fbid=1234567890&id=100000000000001",
     "https://www.facebook.com/permalink.php?id=100000000000001&story_fbid=1234567890"),
    ("https://m.facebook.com/story.php?story_fbid=pfbid0AbC&id=100000000000001",
     "https://www.facebook.com/story.php?id=100000000000001&story_fbid=pfbid0AbC"),
    ("https://www.facebook.com/someagency/videos/1234567890/", "https://www.facebook.com/someagency/videos/1234567890"),
    ("https://www.facebook.com/reel/1234567890", "https://www.facebook.com/reel/1234567890"),
    ("https://www.facebook.com/someagency/photos/a.123/4567890/", "https://www.facebook.com/someagency/photos/a.123/4567890"),
])
def test_groups_permalinks_and_paths_work_as_before(url: str, expected: str) -> None:
    assert normalize_post_url(url) == expected
