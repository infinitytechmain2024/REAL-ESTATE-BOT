"""T5: the FEATURE_APIFY_FACEBOOK flag and the Apify settings of the Facebook services."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bot.facebook_collector.settings import FacebookCollectorSettings

BASE = {"DATABASE_URL": "postgresql://x/y", "BROWSER_SESSION_API_TOKEN": "t" * 24}


def settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> FacebookCollectorSettings:
    for key in ("FEATURE_APIFY_FACEBOOK", "APIFY_TOKEN", "APIFY_FACEBOOK_ACTOR_BUILD", "APIFY_FACEBOOK_MAX_USD_PER_RUN",
                "APIFY_FACEBOOK_MAX_USD_PER_CAMPAIGN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**BASE, **env}.items():
        monkeypatch.setenv(key, value)
    return FacebookCollectorSettings(_env_file=None)


def test_the_flag_is_off_by_default_and_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(monkeypatch)
    assert s.apify_enabled is False and s.apify_config() is None
    assert (s.apify_actor_id, s.apify_max_posts_per_run, s.apify_raw_retention_days) == (
        "apify/facebook-groups-scraper", 500, 7)


@pytest.mark.parametrize(("env", "missing"), [
    ({}, "APIFY_TOKEN, APIFY_FACEBOOK_ACTOR_BUILD"),
    ({"APIFY_TOKEN": "apify_api_x"}, "APIFY_FACEBOOK_ACTOR_BUILD"),
    ({"APIFY_FACEBOOK_ACTOR_BUILD": "1.2.3"}, "APIFY_TOKEN"),
])
def test_on_without_a_token_or_a_build_is_a_startup_error(monkeypatch: pytest.MonkeyPatch, env, missing) -> None:
    with pytest.raises(ValidationError, match=f"FEATURE_APIFY_FACEBOOK=true needs {missing}"):
        settings(monkeypatch, FEATURE_APIFY_FACEBOOK="true", **env)


def test_on_with_an_unpinned_build_or_unsafe_caps_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="pinned build"):
        settings(monkeypatch, FEATURE_APIFY_FACEBOOK="true", APIFY_TOKEN="x", APIFY_FACEBOOK_ACTOR_BUILD="latest")
    with pytest.raises(ValidationError, match="unsafe"):
        settings(monkeypatch, FEATURE_APIFY_FACEBOOK="true", APIFY_TOKEN="x", APIFY_FACEBOOK_ACTOR_BUILD="1.2.3",
                 APIFY_FACEBOOK_MAX_USD_PER_RUN="20", APIFY_FACEBOOK_MAX_USD_PER_CAMPAIGN="10")


def test_on_with_everything_gives_the_provider_config(monkeypatch: pytest.MonkeyPatch) -> None:
    s = settings(monkeypatch, FEATURE_APIFY_FACEBOOK="true", APIFY_TOKEN="apify_api_SECRET", APIFY_FACEBOOK_ACTOR_BUILD="1.2.3")
    config = s.apify_config()
    assert config is not None and (config.build, config.max_usd_per_run, config.timeout_seconds) == ("1.2.3", 2.0, 600)
    assert "apify_api_SECRET" not in repr(s)
