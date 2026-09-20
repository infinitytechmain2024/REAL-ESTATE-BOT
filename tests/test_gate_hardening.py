"""Things that protect the live view from outside the request path.

The token file is a bearer credential for a logged-in browser, the PIN is the
only control that survives a leaked link, and the noVNC/CDP ports are the
back doors the gate exists to stand in front of. None of these are exercised
by an HTTP test, so they are checked here.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from bot.config import FacebookSettings
from bot.services.facebook.tokens import TokenStore

REPO = Path(__file__).resolve().parent.parent


# --- 3.4 the token file is a credential ------------------------------------


async def test_token_file_is_not_readable_by_other_users(tmp_path) -> None:
    """Anyone who can read this file can drive the Facebook browser."""
    path = tmp_path / "token.json"
    await TokenStore(str(path)).create(ttl_seconds=60)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"token file is {oct(mode)}, expected 0o600"


async def test_token_file_is_never_briefly_world_readable(tmp_path) -> None:
    """The temp file it is written through must be tight from creation.

    Writing wide and narrowing afterwards leaves a window in which the
    credential is readable, which is the bug this guards against.
    """
    path = tmp_path / "token.json"
    store = TokenStore(str(path))
    await store.create(ttl_seconds=60)
    await store.create(ttl_seconds=60)  # second write reuses the temp path

    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"temp file left behind: {leftovers}"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


# --- 3.5 a public link without a PIN is a misconfiguration ------------------


def test_public_base_without_a_pin_is_refused() -> None:
    """Exposing the live view with nothing but a URL in front of it.

    The token is in a Telegram message; possession of that message becomes
    possession of the browser unless a PIN stands in the way.
    """
    with pytest.raises(ValidationError, match="FACEBOOK_DESKTOP_PIN"):
        FacebookSettings(desktop_public_base="https://fb.example.com")


def test_public_base_with_a_pin_is_accepted() -> None:
    settings = FacebookSettings(desktop_public_base="https://fb.example.com", desktop_pin="1234")
    assert settings.desktop_pin == "1234"


def test_no_public_base_needs_no_pin() -> None:
    """Loopback-only is the default, and needs nothing in front of it."""
    assert FacebookSettings().desktop_pin is None


# --- 3.6 the back doors stay shut ------------------------------------------


def test_compose_publishes_nothing_beyond_loopback() -> None:
    """Every published port must be bound to 127.0.0.1.

    A mapping written as "6080:6080" listens on every interface, which on a
    VPS means the internet. The gate is the only intended way in.
    """
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    published = re.findall(r'^\s*-\s*"([^"]+:\d+)"', compose, flags=re.MULTILINE)

    assert published, "no port mappings found; has the compose file changed shape?"
    for mapping in published:
        assert mapping.startswith("127.0.0.1:"), f"{mapping} is reachable from outside the host"


@pytest.mark.parametrize("port", ["6080", "5900", "9222"])
def test_the_back_doors_are_never_published(port: str) -> None:
    """noVNC, VNC and Chrome's debugging port must never be mapped out.

    CDP in particular has no authentication whatsoever: reaching it is
    equivalent to owning the logged-in session and its cookies.
    """
    compose = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
    published = re.findall(r'^\s*-\s*"([^"]+:\d+)"', compose, flags=re.MULTILINE)

    assert not [m for m in published if m.endswith(f":{port}")]


def test_entrypoint_binds_the_back_doors_to_loopback() -> None:
    entrypoint = (REPO / "docker" / "entrypoint.sh").read_text(encoding="utf-8")

    assert "--remote-debugging-address=127.0.0.1" in entrypoint, "CDP is not pinned to loopback"
    assert "-localhost" in entrypoint, "x11vnc is not pinned to loopback"
    assert "websockify --web=/usr/share/novnc 127.0.0.1:6080" in entrypoint
