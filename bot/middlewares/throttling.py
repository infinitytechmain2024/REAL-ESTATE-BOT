"""Per-user rate limiting and concurrency control.

One request fans out into several LLM calls and up to a dozen page fetches, so
an unthrottled user can run the worker's budget down on their own. Three limits
apply, and they answer different questions:

* :class:`Cooldown` -- "not that fast": a minimum gap between two requests from
  the same user.
* :class:`SearchSlots` -- "not that many at once": a cap on pipelines in flight,
  both globally and per user.
* ``bot.services.limits.QuotaService`` -- "not that much today". That one is a
  daily budget rather than a rate, and lives with the other spending limits.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from bot.logging_conf import get_logger

log = get_logger(__name__)

_CLEANUP_EVERY = 500
"""Prune the last-seen map every N updates so it cannot grow without bound."""


class Cooldown:
    """Remembers when each user last did something, and how long ago that was.

    Reusable on purpose: the message middleware and the 'Подробнее' button both
    need "has this user done this recently?", with different windows.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._last_seen: dict[int, float] = {}
        self._checks = 0

    def remaining(self, user_id: int) -> float:
        """Seconds still to wait, or ``0.0`` when the user may go ahead."""
        if self.seconds <= 0:
            return 0.0
        previous = self._last_seen.get(user_id)
        if previous is None:
            return 0.0
        elapsed = time.monotonic() - previous
        return max(0.0, self.seconds - elapsed)

    def stamp(self, user_id: int) -> None:
        """Record that the user just went ahead."""
        now = time.monotonic()
        self._last_seen[user_id] = now
        self._prune(now)

    def try_pass(self, user_id: int) -> float:
        """Stamp and return ``0.0``, or return the wait left without stamping.

        Not stamping on a refusal is deliberate: otherwise someone tapping a
        button repeatedly would push their own window forward every time and
        never get through.
        """
        left = self.remaining(user_id)
        if left > 0:
            return left
        self.stamp(user_id)
        return 0.0

    def _prune(self, now: float) -> None:
        self._checks += 1
        if self._checks % _CLEANUP_EVERY:
            return
        cutoff = now - max(self.seconds * 10, 60.0)
        self._last_seen = {uid: seen for uid, seen in self._last_seen.items() if seen > cutoff}


class ThrottlingMiddleware(BaseMiddleware):
    """Reject messages that arrive inside the cooldown window.

    Callback queries are exempt: the feedback buttons are cheap, and silently
    dropping one leaves a spinner running in the user's client. The one
    expensive button, 'Подробнее', carries its own cooldown in the handler --
    it can answer the callback properly instead of dropping it.
    """

    def __init__(self, cooldown_seconds: float) -> None:
        self.cooldown = Cooldown(cooldown_seconds)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if self.cooldown.seconds <= 0 or isinstance(event, CallbackQuery):
            return await handler(event, data)

        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        wait = self.cooldown.try_pass(user.id)
        if wait > 0:
            log.debug("throttled", user_id=user.id, wait=round(wait, 2))
            if isinstance(event, Message):
                await event.answer(
                    f"⏳ Слишком часто. Подождите {wait:.0f} сек. и повторите."
                )
            return None

        return await handler(event, data)


class SlotDenial(StrEnum):
    """Why a slot could not be taken -- the two cases need different wording."""

    GLOBAL = "global"
    """Every worker slot is busy; someone else's request is running."""

    USER = "user"
    """This user already has as many requests in flight as they may have."""


class SearchSlots:
    """Cap on pipelines in flight, globally and per user.

    Plain counters rather than an :class:`asyncio.Semaphore`, because the
    semantics wanted here are "refuse immediately when full", not "queue up" --
    a user who is told to wait can decide for themselves whether to retry,
    whereas a silent queue just makes the bot look hung. Nothing awaits between
    the check and the increment and the event loop is single-threaded, so this
    cannot race.
    """

    def __init__(self, limit: int, per_user_limit: int = 1) -> None:
        self.limit = limit
        self.per_user_limit = per_user_limit
        self._active = 0
        self._per_user: dict[int, int] = {}

    @property
    def active(self) -> int:
        return self._active

    def active_for(self, user_id: int) -> int:
        return self._per_user.get(user_id, 0)

    def try_acquire(self, user_id: int) -> SlotDenial | None:
        """Take a slot for *user_id*; ``None`` means it was granted."""
        if self._per_user.get(user_id, 0) >= self.per_user_limit:
            return SlotDenial.USER
        if self._active >= self.limit:
            return SlotDenial.GLOBAL

        self._active += 1
        self._per_user[user_id] = self._per_user.get(user_id, 0) + 1
        return None

    def release(self, user_id: int) -> None:
        """Give the slot back. Safe to call more than once."""
        self._active = max(0, self._active - 1)
        remaining = self._per_user.get(user_id, 0) - 1
        if remaining > 0:
            self._per_user[user_id] = remaining
        else:
            # Drop the key rather than leaving a zero behind, so the map stays
            # the size of the currently active users instead of every user ever.
            self._per_user.pop(user_id, None)


BUSY_MESSAGES: dict[SlotDenial, str] = {
    SlotDenial.GLOBAL: (
        "🚦 Сейчас обрабатывается максимальное число запросов. "
        "Попробуйте через минуту, пожалуйста."
    ),
    SlotDenial.USER: (
        "⏳ Ваш предыдущий запрос ещё выполняется. "
        "Дождитесь результатов, пожалуйста."
    ),
}
"""What to tell the user for each kind of refusal."""
