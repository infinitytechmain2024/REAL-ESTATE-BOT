"""Per-user rate limiting.

One request fans out into several LLM calls and up to a dozen page fetches, so
an unthrottled user can run the worker's budget down on their own. Two limits
apply: a cooldown between requests, and a cap on how many searches run at once
across all users.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from bot.logging_conf import get_logger

log = get_logger(__name__)

_CLEANUP_EVERY = 500
"""Prune the last-seen map every N updates so it cannot grow without bound."""


class ThrottlingMiddleware(BaseMiddleware):
    """Reject messages that arrive inside the cooldown window.

    Callback queries are exempt: button presses are cheap, and silently
    dropping one leaves a spinner running in the user's client.
    """

    def __init__(self, cooldown_seconds: float) -> None:
        self.cooldown = cooldown_seconds
        self._last_seen: dict[int, float] = {}
        self._updates_handled = 0

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if self.cooldown <= 0 or isinstance(event, CallbackQuery):
            return await handler(event, data)

        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)

        now = time.monotonic()
        previous = self._last_seen.get(user.id)
        if previous is not None and now - previous < self.cooldown:
            log.debug("throttled", user_id=user.id, since_last=round(now - previous, 2))
            if isinstance(event, Message):
                await event.answer("⏳ Слишком часто. Подождите пару секунд.")
            return None

        self._last_seen[user.id] = now
        self._prune(now)
        return await handler(event, data)

    def _prune(self, now: float) -> None:
        self._updates_handled += 1
        if self._updates_handled % _CLEANUP_EVERY:
            return
        cutoff = now - max(self.cooldown * 10, 60.0)
        self._last_seen = {uid: seen for uid, seen in self._last_seen.items() if seen > cutoff}


class SearchSlots:
    """Global cap on concurrent pipelines, shared by every handler.

    A plain counter rather than an :class:`asyncio.Semaphore`, because the
    semantics wanted here are "refuse immediately when full", not "queue up".
    Nothing awaits between the check and the increment and the event loop is
    single-threaded, so this cannot race.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    def try_acquire(self) -> bool:
        """Take a slot. ``False`` means the bot is already at capacity."""
        if self._active >= self.limit:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        self._active = max(0, self._active - 1)
