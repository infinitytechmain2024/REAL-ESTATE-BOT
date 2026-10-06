"""Storage limitation: old rows expire, and a user can ask to be forgotten.

The database holds contact details and page text about people who never used
the bot, so "keep everything forever" is not a neutral default. These cover
the two mechanisms that stop it: a window after which rows are purged, and an
erasure request from a user about their own data.

Deleting is irreversible, so the confirmation step is tested as carefully as
the deletion itself.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, Mock

import pytest

from bot.config import Settings, SupabaseSettings
from bot.handlers import privacy
from bot.services.retention import RetentionPurger


def _settings(retention_days: int = 90, configured: bool = True) -> Settings:
    supabase = SupabaseSettings(
        url="https://example.supabase.co" if configured else None,
        key="service-role-key" if configured else None,
        retention_days=retention_days,
    )
    return Settings(supabase=supabase)


# --- the retention window ---------------------------------------------------


async def test_purges_rows_older_than_the_window() -> None:
    repo = AsyncMock()
    await RetentionPurger(_settings(retention_days=30), repo).tick()

    repo.purge_expired.assert_awaited_once()
    (cutoff,) = repo.purge_expired.await_args.args
    age = dt.datetime.now(dt.UTC) - cutoff
    assert 29 <= age.days <= 30, cutoff


async def test_zero_days_means_keep_everything() -> None:
    """An explicit opt-out, not the default -- see COMPLIANCE.md."""
    repo = AsyncMock()
    await RetentionPurger(_settings(retention_days=0), repo).tick()

    repo.purge_expired.assert_not_awaited()


async def test_does_nothing_without_a_database() -> None:
    repo = AsyncMock()
    await RetentionPurger(_settings(configured=False), repo).tick()

    repo.purge_expired.assert_not_awaited()


async def test_a_failed_purge_does_not_kill_the_loop() -> None:
    """Storage trouble is a logged problem, not a crashed background task."""
    repo = AsyncMock()
    repo.purge_expired.side_effect = RuntimeError("postgres is having a day")

    await RetentionPurger(_settings(), repo).tick()  # must not raise


# --- erasure on request -----------------------------------------------------


class FakeMessage:
    def __init__(self, user_id: int | None = 7) -> None:
        self.from_user = Mock(id=user_id) if user_id is not None else None
        self.replies: list[str] = []
        self.keyboards: list[object] = []

    async def answer(self, text: str, **kwargs: object) -> None:
        self.replies.append(text)
        self.keyboards.append(kwargs.get("reply_markup"))


class FakeQuery:
    def __init__(self, user_id: int = 7) -> None:
        self.from_user = Mock(id=user_id)
        self.message = FakeMessage(user_id)
        self.answered: list[str | None] = []

    async def answer(self, text: str | None = None, **_kwargs: object) -> None:
        self.answered.append(text)


async def test_forget_asks_before_deleting_anything() -> None:
    """One tap must not erase a history. It is not recoverable."""
    repo = AsyncMock()
    message = FakeMessage()

    await privacy.cmd_forget(message, _settings(), repo)

    repo.forget_user.assert_not_awaited()
    assert message.replies, "said nothing at all"
    assert message.keyboards[0] is not None, "no confirmation button offered"


async def test_confirming_erases_that_user() -> None:
    repo = AsyncMock()
    query = FakeQuery(user_id=42)

    await privacy.on_forget_confirmed(query, _settings(), repo)

    repo.forget_user.assert_awaited_once_with(42)


async def test_erasure_reports_failure_rather_than_claiming_success() -> None:
    """Telling someone their data is gone when it is not would be the worst
    possible outcome of this feature."""
    repo = AsyncMock()
    repo.forget_user.return_value = False
    query = FakeQuery()

    await privacy.on_forget_confirmed(query, _settings(), repo)

    assert any("не удалось" in reply.lower() for reply in query.message.replies), (
        query.message.replies
    )


async def test_forget_without_a_database_says_so_plainly() -> None:
    repo = AsyncMock()
    message = FakeMessage()

    await privacy.cmd_forget(message, _settings(configured=False), repo)

    repo.forget_user.assert_not_awaited()
    assert message.replies


@pytest.mark.parametrize("user_id", [None])
async def test_an_anonymous_update_is_ignored(user_id) -> None:
    repo = AsyncMock()
    await privacy.cmd_forget(FakeMessage(user_id=user_id), _settings(), repo)

    repo.forget_user.assert_not_awaited()
