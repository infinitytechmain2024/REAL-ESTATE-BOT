"""Shared test setup for the live campaign stack.

The Playwright fakes and pipeline factories of the retired standalone bot live in
``legacy/tests/conftest.py``; that tree is not collected (see ``pyproject.toml``).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _minimal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give code that reads the Telegram token a syntactically valid placeholder.

    Nothing here reaches the network.
    """
    monkeypatch.setenv("TELEGRAM_TOKEN", "123456:test-token-not-real")
