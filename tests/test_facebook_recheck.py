"""Rechecking configured groups, and telling the operator when one goes dark.

A group the bot cannot read is not a user-facing failure -- since Stage 2 a
restricted group is correctly skipped rather than reported as a broken source
-- but it is not nothing either. Only the operator can join a group or drop a
dead URL, so somebody has to tell them, once, when the state changes.

The same browser invariants apply here as everywhere else: never start it,
never touch it without the lock, and stop the moment the session flips.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bot.config import FacebookSettings, Settings
from bot.services.facebook import recheck as recheck_module
from bot.services.facebook.browser import SessionState
from bot.services.facebook.groups import GroupAccess
from bot.services.facebook.recheck import GroupRechecker
from tests.conftest import FakeBot, FakePage, FakeSession

GROUP_A = "https://www.facebook.com/groups/aaa"
GROUP_B = "https://www.facebook.com/groups/bbb"


def _settings(*groups: str) -> Settings:
    return Settings(
        facebook=FacebookSettings(
            enabled=True,
            admin_telegram_ids=["1"],
            group_urls=list(groups) or [GROUP_A],
            min_group_recheck_minutes=1,
        )
    )


def _session(*states: SessionState) -> FakeSession:
    session = FakeSession(list(states) or [SessionState.HEALTHY] * 10)
    session.page = FakePage()
    return session


def _rechecker(session, settings, bot, repo=None) -> GroupRechecker:
    return GroupRechecker(session, settings, bot, repo=repo)


@pytest.fixture
def access(monkeypatch: pytest.MonkeyPatch):
    """Control what each group looks like, without a browser."""
    mock = AsyncMock(return_value=GroupAccess.ACCESSIBLE)
    monkeypatch.setattr(recheck_module, "check_access", mock)
    return mock


# --- the browser invariants -------------------------------------------------


async def test_does_not_start_the_browser(access) -> None:
    """A timer firing must never open a browser window on its own."""
    session = _session()
    session.has_live_context = False
    await _rechecker(session, _settings(), FakeBot()).tick()

    assert session.start_calls == 0
    assert access.await_count == 0


async def test_holds_the_lock_while_using_the_page(access) -> None:
    session = _session()
    await _rechecker(session, _settings(), FakeBot()).tick()

    assert session.observed_unlocked == 0, "the page was read without the lock"


async def test_skips_entirely_when_the_session_is_not_healthy(access) -> None:
    """Jobs are frozen unless HEALTHY; this is a job."""
    session = _session(SessionState.LOGIN_NEEDED)
    bot = FakeBot()
    await _rechecker(session, _settings(), bot).tick()

    assert access.await_count == 0, "a group was opened against an unhealthy session"
    assert bot.messages == [], "the session watchdog owns session alerts, not this"


async def test_stops_when_the_session_flips_mid_run(access) -> None:
    """Second group must not be opened once the session has gone."""
    access.side_effect = [GroupAccess.ACCESSIBLE, GroupAccess.ACCESSIBLE]
    session = _session(SessionState.HEALTHY, SessionState.HUMAN_REQUIRED)
    await _rechecker(session, _settings(GROUP_A, GROUP_B), FakeBot()).tick()

    assert access.await_count == 1, "kept reading after the session flipped"


# --- what the operator hears ------------------------------------------------


async def test_a_group_going_dark_alerts_once(access) -> None:
    access.return_value = GroupAccess.MEMBERSHIP_REQUIRED
    session = _session()
    bot = FakeBot()
    rechecker = _rechecker(session, _settings(), bot)

    await rechecker.tick()
    assert len(bot.messages) == 1, bot.messages
    assert GROUP_A in bot.messages[0]

    rechecker._last_checked.clear()  # let it run again immediately
    await rechecker.tick()
    assert len(bot.messages) == 1, "repeated the same unchanged state at the operator"


async def test_a_group_coming_back_says_so(access) -> None:
    session = _session()
    bot = FakeBot()
    rechecker = _rechecker(session, _settings(), bot)

    access.return_value = GroupAccess.MEMBERSHIP_REQUIRED
    await rechecker.tick()
    rechecker._last_checked.clear()

    access.return_value = GroupAccess.ACCESSIBLE
    await rechecker.tick()

    assert len(bot.messages) == 2, bot.messages
    assert "снова" in bot.messages[1].lower() or "доступ" in bot.messages[1].lower()


async def test_a_readable_group_says_nothing_at_all(access) -> None:
    """Silence is the correct output when everything is fine."""
    bot = FakeBot()
    await _rechecker(_session(), _settings(), bot).tick()

    assert bot.messages == []


async def test_one_message_covers_several_changed_groups(access) -> None:
    """Two groups changing is one message, not a burst."""
    access.side_effect = [GroupAccess.MEMBERSHIP_REQUIRED, GroupAccess.UNAVAILABLE]
    bot = FakeBot()
    await _rechecker(_session(), _settings(GROUP_A, GROUP_B), bot).tick()

    assert len(bot.messages) == 1, bot.messages
    assert GROUP_A in bot.messages[0] and GROUP_B in bot.messages[0]


# --- not re-opening groups more often than asked ---------------------------


async def test_respects_the_recheck_interval(access) -> None:
    session = _session()
    rechecker = _rechecker(session, _settings(), FakeBot())

    await rechecker.tick()
    await rechecker.tick()

    assert access.await_count == 1, "re-opened a group inside the recheck interval"


# --- persistence is optional ------------------------------------------------


async def test_records_state_when_a_repo_is_present(access) -> None:
    access.return_value = GroupAccess.PENDING_APPROVAL
    repo = AsyncMock()
    repo.facebook_group_access = AsyncMock(return_value=None)
    await _rechecker(_session(), _settings(), FakeBot(), repo=repo).tick()

    repo.record_facebook_group_access.assert_awaited_once()
    url, state = repo.record_facebook_group_access.await_args.args
    assert url == GROUP_A
    assert state == GroupAccess.PENDING_APPROVAL.value


async def test_remembered_state_survives_a_restart(access) -> None:
    """A restart must not re-announce a group the operator already knows about."""
    access.return_value = GroupAccess.MEMBERSHIP_REQUIRED
    repo = AsyncMock()
    repo.facebook_group_access = AsyncMock(return_value=GroupAccess.MEMBERSHIP_REQUIRED.value)
    bot = FakeBot()

    await _rechecker(_session(), _settings(), bot, repo=repo).tick()

    assert bot.messages == [], "re-announced a state the database already held"


async def test_works_without_a_database(access) -> None:
    """Supabase is optional in this project; alerting must not depend on it."""
    access.return_value = GroupAccess.UNAVAILABLE
    bot = FakeBot()
    await _rechecker(_session(), _settings(), bot, repo=None).tick()

    assert len(bot.messages) == 1
