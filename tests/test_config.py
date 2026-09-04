"""Start-up configuration errors.

A missing variable must reach the operator as its own name, not as a pydantic
traceback.
"""

from __future__ import annotations

import pytest

from bot.config import Settings, get_settings
from bot.exceptions import ConfigurationError


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    get_settings.cache_clear()


def test_a_missing_token_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    with pytest.raises(ConfigurationError) as excinfo:
        get_settings(env_file=None)
    assert "TELEGRAM_TOKEN" in str(excinfo.value)


def test_the_error_is_not_a_pydantic_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bot.main` catches ConfigurationError to exit without a traceback."""
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    with pytest.raises(ConfigurationError):
        get_settings(env_file=None)


def test_an_invalid_value_is_reported_with_its_variable_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("LOG_LEVEL", "definitely-not-a-level")
    with pytest.raises(ConfigurationError) as excinfo:
        get_settings(env_file=None)
    assert "LOG_LEVEL" in str(excinfo.value)


def test_a_complete_configuration_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    settings = get_settings(env_file=None)
    assert settings.telegram.token.get_secret_value() == "123:abc"


def test_admin_ids_accept_a_comma_separated_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", "111, 222")
    settings = get_settings(env_file=None)
    assert settings.telegram.admin_ids == [111, 222]


def test_admin_ids_accept_a_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_ADMIN_IDS", '["111","222"]')
    settings = get_settings(env_file=None)
    assert settings.telegram.admin_ids == [111, 222]


def test_the_example_env_file_is_a_valid_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """.env.example is what an operator copies; it must actually load."""
    from dotenv import dotenv_values

    for key, value in dotenv_values(".env.example").items():
        if value is not None:
            monkeypatch.setenv(key, value)

    settings = get_settings(env_file=None)
    assert isinstance(settings, Settings)
    assert settings.limits.daily_searches > 0
    assert settings.pipeline.timeout_seconds > 0
    assert settings.parser.max_redirects >= 0


@pytest.mark.parametrize(
    ("given", "value", "missing"),
    [
        ("SUPABASE_URL", "https://project.supabase.co", "SUPABASE_KEY"),
        ("SUPABASE_KEY", "service-role-key", "SUPABASE_URL"),
    ],
)
def test_half_configured_supabase_is_refused(
    monkeypatch: pytest.MonkeyPatch, given: str, value: str, missing: str
) -> None:
    """Half a credential is a typo, and silently disabling persistence hides it.

    It would also downgrade the daily quotas to in-process counters that reset
    on every restart -- the failure mode the limits exist to prevent.
    """
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:token")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    monkeypatch.setenv(given, value)

    with pytest.raises(ConfigurationError) as excinfo:
        get_settings(env_file=None)

    message = str(excinfo.value)
    assert missing in message
    assert given in message


def test_supabase_may_be_left_out_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither half set stays a supported deployment, not an error."""
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:token")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)

    settings = get_settings(env_file=None)
    assert settings.supabase.configured is False
