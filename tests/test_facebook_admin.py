"""The recovery watcher: what it must never do, and how often it may speak.

Two invariants here. It must not navigate the browser -- a human is using it.
And it must hold the session lock while it reads, because a group job may be
driving the same single page; the lock is the "one owner of the browser"
rule, and a poller that ignores it is the most disruptive thing in the
codebase, firing every ten seconds for fifteen minutes.
"""

from __future__ import annotations

import asyncio

import pytest

from bot.config import FacebookSettings, Settings
from bot.handlers import facebook_admin
from bot.services.facebook.browser import SessionState


class FakeSession:
    """Records whether the lock was held at the moment state was observed."""

    def __init__(self, states: list[SessionState]) -> None:
        self._states = list(states)
        self.lock = asyncio.Lock()
        self.observed_unlocked = 0
        self.probe_calls = 0
        self.observe_calls = 0

    async def observe_state(self) -> SessionState:
        self.observe_calls += 1
        if not self.lock.locked():
            self.observed_unlocked += 1
        return self._states.pop(0) if self._states else SessionState.HUMAN_REQUIRED

    async def probe_state(self) -> SessionState:
        self.probe_calls += 1
        if not self.lock.locked():
            self.observed_unlocked += 1
        return self._states.pop(0) if self._states else SessionState.HUMAN_REQUIRED

    async def start(self) -> None:
        return None


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, _chat_id: int, text: str, **_kwargs: object) -> None:
        self.messages.append(text)


class FakeTokenStore:
    def __init__(self) -> None:
        self.invalidated = 0

    async def invalidate(self) -> None:
        self.invalidated += 1

    async def get_or_create(self, _ttl: int) -> str:
        return "tok"


@pytest.fixture(autouse=True)
def _fast_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Collapse the poll interval so a 15-minute incident runs in milliseconds.

    The interval must stay non-zero: the watcher counts elapsed time by
    adding it, so 0 never reaches the timeout and the test hangs forever
    rather than failing.
    """
    monkeypatch.setattr(facebook_admin, "_WATCH_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(facebook_admin, "_WATCH_TIMEOUT_SECONDS", 0.05)
    facebook_admin._watcher_task = None


def _settings() -> Settings:
    return Settings(facebook=FacebookSettings(enabled=True, admin_telegram_ids=["1"]))


async def test_watcher_observes_and_never_probes() -> None:
    """Probing navigates. The human is mid-login; navigating wipes their form."""
    session = FakeSession([SessionState.LOGIN_NEEDED, SessionState.HEALTHY])
    bot = FakeBot()

    await facebook_admin._watch_for_recovery(
        session, FakeTokenStore(), _settings(), bot, chat_id=1
    )

    assert session.probe_calls == 0, "the watcher navigated the human's browser"
    assert session.observe_calls >= 1


async def test_watcher_holds_the_lock_while_reading() -> None:
    """A group job may be driving the same page; the lock is the invariant."""
    session = FakeSession([SessionState.HEALTHY])

    await facebook_admin._watch_for_recovery(
        session, FakeTokenStore(), _settings(), FakeBot(), chat_id=1
    )

    assert session.observed_unlocked == 0, "the watcher read the page without the lock"


async def test_recovery_sends_exactly_one_message_and_kills_the_link() -> None:
    session = FakeSession([SessionState.LOGIN_NEEDED, SessionState.HEALTHY])
    bot, tokens = FakeBot(), FakeTokenStore()

    await facebook_admin._watch_for_recovery(session, tokens, _settings(), bot, chat_id=1)

    assert len(bot.messages) == 1, bot.messages
    assert "Готово" in bot.messages[0]
    assert tokens.invalidated == 1, "the live-view link outlived the incident"


async def test_timeout_sends_exactly_one_message_and_not_the_recovery_one() -> None:
    session = FakeSession([SessionState.LOGIN_NEEDED] * 50)
    bot = FakeBot()

    await facebook_admin._watch_for_recovery(session, FakeTokenStore(), _settings(), bot, 1)

    assert len(bot.messages) == 1, bot.messages
    assert "15 минут" in bot.messages[0]


async def test_a_failing_check_does_not_kill_the_watcher() -> None:
    """A transient error is 'not recovered yet', not a crashed background task."""

    class Flaky(FakeSession):
        async def observe_state(self) -> SessionState:
            self.observe_calls += 1
            if self.observe_calls == 1:
                raise RuntimeError("browser hiccup")
            return SessionState.HEALTHY

    session = Flaky([])
    bot = FakeBot()

    await facebook_admin._watch_for_recovery(session, FakeTokenStore(), _settings(), bot, 1)

    assert len(bot.messages) == 1
    assert "Готово" in bot.messages[0]


async def test_second_tap_reuses_the_running_watcher() -> None:
    """No stacked pollers, no duplicate alerts for one incident."""
    session = FakeSession([SessionState.LOGIN_NEEDED] * 50)
    bot = FakeBot()
    settings = _settings()

    facebook_admin._start_watcher(session, FakeTokenStore(), settings, bot, 1)
    first = facebook_admin._watcher_task
    facebook_admin._start_watcher(session, FakeTokenStore(), settings, bot, 1)
    second = facebook_admin._watcher_task

    assert first is second, "a second tap stacked a duplicate watcher"
    await asyncio.wait_for(first, timeout=5)
    assert len(bot.messages) == 1
