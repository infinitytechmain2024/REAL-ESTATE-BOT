"""Shared test configuration.

The bot package imports cleanly without any environment set, so the tests here
exercise the guards directly rather than booting a Bot. Anything that would
reach the network is stubbed.
"""

from __future__ import annotations

import functools

import pytest


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a developer's real .env out of the tests."""
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)


@functools.lru_cache(maxsize=1)
def root_router():  # type: ignore[no-untyped-def]
    """The assembled router, built once per process.

    `build_router` attaches the module-level routers to a fresh root, and
    aiogram refuses to attach a router to a second parent -- so calling it
    twice in one process fails. Production calls it once; the tests have to be
    just as careful.
    """
    from bot.handlers import build_router

    return build_router()
