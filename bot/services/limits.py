"""Per-user daily request quotas.

A cooldown stops someone hammering the bot for a minute; it does nothing about
someone who sends one request every ten seconds all day. This is the counter
that does, and it is the reason a stuck client cannot quietly spend a month's
LLM budget overnight.

Counters roll over on the UTC calendar date. The authoritative store is
Supabase, through the atomic ``bump_daily_usage`` function -- two requests
arriving together must not both read the old count and both be let through. If
Supabase is disabled or unreachable the service falls back to an in-process
counter, so the limit still holds for as long as the worker lives rather than
disappearing exactly when things are already going wrong.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from bot.config import LimitsSettings
from bot.exceptions import QuotaExceededError
from bot.logging_conf import get_logger

if TYPE_CHECKING:
    from bot.services.db import SupabaseRepository

log = get_logger(__name__)


class QuotaKind(StrEnum):
    """What is being counted. The values are the ``daily_usage`` column names."""

    SEARCHES = "searches"
    DETAILS = "details"


@dataclass(frozen=True, slots=True)
class QuotaVerdict:
    """The outcome of one quota check."""

    allowed: bool
    used: int
    limit: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


def utc_today() -> dt.date:
    """The current UTC calendar date -- the window every counter resets on."""
    return dt.datetime.now(dt.UTC).date()


_MESSAGES: dict[QuotaKind, str] = {
    QuotaKind.SEARCHES: (
        "🚧 Вы израсходовали дневной лимит поисковых запросов ({limit} в сутки).\n"
        "Лимит обновится после 00:00 UTC."
    ),
    QuotaKind.DETAILS: (
        "🚧 Вы израсходовали дневной лимит подробных сводок ({limit} в сутки).\n"
        "Лимит обновится после 00:00 UTC."
    ),
}


class QuotaService:
    """Counts what each user spends per UTC day and refuses them past the limit."""

    def __init__(self, settings: LimitsSettings, repo: SupabaseRepository) -> None:
        self.settings = settings
        self.repo = repo
        self._date = utc_today()
        self._local: dict[tuple[int, QuotaKind], int] = {}

    def limit_for(self, kind: QuotaKind) -> int:
        return (
            self.settings.daily_searches
            if kind is QuotaKind.SEARCHES
            else self.settings.daily_details
        )

    async def consume(self, user_id: int, kind: QuotaKind) -> QuotaVerdict:
        """Book one unit against *user_id* and say whether it was allowed.

        The counter is incremented first and checked afterwards, because that
        is what makes the increment atomic. A user who keeps pressing past
        their limit therefore keeps incrementing a number nobody reads again
        until tomorrow -- harmless, and much safer than a check-then-increment
        that two concurrent requests can both win.
        """
        limit = self.limit_for(kind)
        if limit <= 0:
            # A limit of zero means the feature is switched off; do not spend a
            # database round trip to say so.
            raise QuotaExceededError(
                f"{kind.value} are disabled (limit is 0)",
                user_message=(
                    "Эта функция сейчас отключена администратором. "
                    "Обратитесь к администратору бота."
                ),
            )

        used = await self._increment(user_id, kind)
        verdict = QuotaVerdict(allowed=used <= limit, used=used, limit=limit)

        if not verdict.allowed:
            log.info("quota.exceeded", user_id=user_id, kind=kind.value, used=used, limit=limit)
            raise QuotaExceededError(
                f"user {user_id} is over the daily {kind.value} quota ({used}/{limit})",
                user_message=_MESSAGES[kind].format(limit=limit),
            )

        log.debug("quota.consumed", user_id=user_id, kind=kind.value, used=used, limit=limit)
        return verdict

    async def _increment(self, user_id: int, kind: QuotaKind) -> int:
        """Add one to today's counter and return the new value."""
        self._roll_over()

        counted = await self.repo.bump_daily_usage(user_id, kind.value)
        if counted is not None:
            # Keep the local mirror in step so a later Supabase outage does not
            # hand the user a fresh allowance.
            self._local[(user_id, kind)] = counted
            return counted

        if self.repo.enabled:
            # Configured but not answering: fall back rather than fail open,
            # and say so once per failure so the cause is visible in the log.
            log.warning("quota.supabase_unavailable", user_id=user_id, kind=kind.value)

        current = self._local.get((user_id, kind), 0) + 1
        self._local[(user_id, kind)] = current
        return current

    def _roll_over(self) -> None:
        """Drop the in-process counters when the UTC date changes."""
        today = utc_today()
        if today != self._date:
            log.info("quota.rolled_over", previous=self._date.isoformat(), current=today.isoformat())
            self._date = today
            self._local.clear()
