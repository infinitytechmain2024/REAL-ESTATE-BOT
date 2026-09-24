"""Deployment-shape rules that no unit test would otherwise catch.

Two invariants live here. The Facebook password should not be sitting on the
machine when the human-login path is the primary one, and the portal fetcher
must never share a browser with the Facebook session -- routing an Idealista
request through the logged-in profile would put the operator's Facebook
identity behind every listing fetch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

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


# --- the vendored SearXNG must survive a clone ------------------------------


def test_the_runtime_ignore_rule_does_not_swallow_searxng() -> None:
    """`data/` unanchored matches a directory of that name at any depth.

    It did exactly that to `searxng/searx/data`, the vendored engine, currency
    and locale tables. Without them `from searx.data import ENGINE_TRAITS`
    fails and SearXNG cannot start at all, so every clone of this repository
    had no working search engine while the bot's own tests stayed green.
    """
    repo = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "check-ignore", "-v", "searxng/searx/data/engines.json"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, (
        f"the vendored SearXNG data package is gitignored by: {result.stdout.strip()}"
    )


def test_the_bots_own_runtime_directory_is_still_ignored() -> None:
    """The rule still has to do its actual job: keep ./data out of the repo.

    That is where the Chrome profile and the live-view token file live, both
    of which are credentials.
    """
    repo = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "check-ignore", "data/facebook_profile/Cookies"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, "./data is no longer ignored — session cookies could be committed"


def test_no_environment_file_is_tracked_except_the_example() -> None:
    """`.env.backup` reached this repository, with values in it.

    The ignore rule was `*.env`, which matches a file *ending* in .env -- not
    the shape a backup takes. The example file is the one env file that belongs
    here, because it carries names and no values.
    """
    repo = Path(__file__).resolve().parent.parent
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.env", ".env", ".env.*"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    offenders = [name for name in tracked if name and name != ".env.example"]

    assert offenders == [], f"environment files are tracked in git: {offenders}"


def test_an_env_backup_cannot_be_added_again() -> None:
    repo = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["git", "check-ignore", ".env.backup"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, ".env.backup is not gitignored"


def test_the_local_model_server_stays_on_loopback() -> None:
    """A model server on a home network answers anyone who asks.

    It has no authentication of any kind, exactly like the CDP port, so the
    rule is the same one: bind to 127.0.0.1 and publish nothing.
    """
    script = Path(__file__).resolve().parent.parent / "scripts" / "run_llm.sh"
    body = script.read_text(encoding="utf-8")

    assert script.stat().st_mode & 0o111, "scripts/run_llm.sh is not executable"
    assert "--host 127.0.0.1" in body, "the local model server must bind to loopback"
    assert "0.0.0.0" not in body


def test_the_local_model_server_sets_the_two_things_ollama_does_not() -> None:
    """The whole reason this script exists rather than `ollama run`.

    A 4096-token default that truncates silently, and a KV cache that cannot be
    quantised, are what made a large context unusable.
    """
    script = Path(__file__).resolve().parent.parent / "scripts" / "run_llm.sh"
    body = script.read_text(encoding="utf-8")

    assert "--ctx-size" in body
    assert "--cache-type-k" in body
