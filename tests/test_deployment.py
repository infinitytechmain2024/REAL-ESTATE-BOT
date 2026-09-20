"""Deployment-shape rules that no unit test would otherwise catch.

Two invariants live here. The Facebook password should not be sitting on the
machine when the human-login path is the primary one, and the portal fetcher
must never share a browser with the Facebook session -- routing an Idealista
request through the logged-in profile would put the operator's Facebook
identity behind every listing fetch.
"""

from __future__ import annotations

from bot.config import FacebookSettings, ParserSettings, Settings
from bot.main import deployment_warnings


def _settings(**facebook: object) -> Settings:
    return Settings(facebook=FacebookSettings(enabled=True, **facebook))


# --- 6.4 stored credentials ------------------------------------------------


def test_a_stored_facebook_password_is_called_out() -> None:
    """Primary path is a human logging in; the password need not be here.

    With 2FA switched off on the bot account, a stored password is most of
    what protects it, sitting next to a browser that is already logged in.
    """
    warnings = deployment_warnings(
        _settings(login_email="bot@example.com", login_password="hunter2")
    )

    assert any("FACEBOOK_PASSWORD" in w for w in warnings), warnings


def test_no_warning_when_credentials_are_absent() -> None:
    assert deployment_warnings(_settings()) == []


def test_facebook_disabled_says_nothing() -> None:
    """Whatever is in the file, an unused module is not a live risk."""
    settings = Settings(facebook=FacebookSettings(enabled=False, login_password="hunter2"))
    assert deployment_warnings(settings) == []


# --- 6.5 the two browsers stay separate ------------------------------------


def test_the_portal_fetcher_cannot_be_pointed_at_the_facebook_browser() -> None:
    """There must be no knob that reuses the logged-in profile for portals.

    This asserts the separation is structural rather than a convention: the
    parser's settings expose no profile directory and no CDP endpoint, so
    there is nothing to set that would make an Idealista fetch travel through
    the Facebook session. It is a guard against a future option being added
    without anyone weighing what it would mean.
    """
    knobs = set(ParserSettings.model_fields)

    assert not {field for field in knobs if "profile" in field}
    assert not {field for field in knobs if "cdp" in field}
    assert not {field for field in knobs if "user_data" in field}


def test_facebook_owns_the_profile_and_cdp_settings() -> None:
    """The other half of the same rule: those knobs belong to Facebook alone."""
    facebook = set(FacebookSettings.model_fields)

    assert "profile_dir" in facebook
    assert "cdp_url" in facebook
