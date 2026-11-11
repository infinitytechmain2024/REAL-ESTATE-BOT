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
    ReactionPage,
    ReactionsConfig,
    map_rows,
    reaction_type,
)
from bot.acquisition.apify_source import MemorySourceRunStore
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

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            return httpx.Response(201, json={"data": {"id": "RUN1", "defaultDatasetId": "DS1", "status": "READY"}})
        if "/actor-runs/RUN1" in path:
            return httpx.Response(200, json={"data": {"id": "RUN1", "status": "SUCCEEDED", "defaultDatasetId": "DS1",
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


def test_a_cap_below_results_times_price_is_a_settings_error() -> None:
    with pytest.raises(ValidationError, match="APIFY_FACEBOOK_REACTIONS_MAX_CHARGE_USD"):
        _settings(APIFY_FACEBOOK_REACTIONS_MAX_RESULTS=500, APIFY_FACEBOOK_REACTIONS_MAX_CHARGE_USD=0.70)
    assert 500 * USD_PER_RESULT > 0.70
    _settings(APIFY_FACEBOOK_REACTIONS_MAX_RESULTS=500, APIFY_FACEBOOK_REACTIONS_MAX_CHARGE_USD=3.10)
    with pytest.raises(ValueError, match="max_charge_usd"):
        ReactionsConfig(actor_id="apify/facebook-likes-scraper", build="0.0.7", max_results=100, max_charge_usd=0.5)


@pytest.mark.parametrize("value", [0, 501])
def test_max_results_range(value: int) -> None:
    with pytest.raises(ValidationError):
        _settings(APIFY_FACEBOOK_REACTIONS_MAX_RESULTS=value)
