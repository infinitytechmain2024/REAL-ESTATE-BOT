"""Keeping the configured group list honest, and telling the operator when it isn't.

A group the bot cannot read is not a user-facing failure. Since Stage 2 a
restricted group is skipped rather than reported as a broken source, which is
right: "we are not members of that group" is a stable fact, not an outage, and
warning every searcher about it would train them to ignore the warning.

But it is not nothing either. Only the operator can join a group, answer its
screening questions, or drop a URL that no longer exists -- and without this,
an entirely unreadable group list looks exactly like "nothing matched" to
everyone, forever. So the state of each group is rechecked periodically and
the operator is told once, when it changes.

The browser rules are the same as everywhere else in this package: never start
it, never touch the page without the lock, and stop the moment the session
stops being healthy. Opening a group navigates, so this is a job, and jobs do
not run against a session that needs a human.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from aiogram import Bot

from bot.config import Settings
from bot.logging_conf import get_logger
from bot.services.facebook.browser import FacebookSession, SessionState
from bot.services.facebook.groups import GroupAccess, check_access

if TYPE_CHECKING:
    from bot.services.db import SupabaseRepository

log = get_logger(__name__)

#: How each state reads to a non-technical operator, and what to do about it.
_ACCESS_LABEL = {
    GroupAccess.ACCESSIBLE: "✅ снова доступна",
    GroupAccess.MEMBERSHIP_REQUIRED: "🚫 нужно вступить в группу",
    GroupAccess.PENDING_APPROVAL: "⏳ заявка на вступление ещё не одобрена",
    GroupAccess.UNAVAILABLE: "❌ группа недоступна (возможно, удалена)",
    GroupAccess.LOGIN_REQUIRED: "🔒 требуется вход в Facebook",
    GroupAccess.UNKNOWN_ERROR: "❓ не удалось прочитать (разметка изменилась?)",
}


class GroupRechecker:
    """Re-opens each configured group on a schedule and reports state changes.

    State is remembered in ``facebook_groups`` when Supabase is configured, so
    a restart does not re-announce what the operator already knows. Without a
    database it falls back to in-process memory and may repeat itself once
    after a restart -- which is the right trade: alerting must not depend on
    storage this project treats as optional.
    """

    def __init__(
        self,
        session: FacebookSession,
        settings: Settings,
        bot: Bot,
        repo: SupabaseRepository | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.bot = bot
        self.repo = repo
        #: url -> monotonic timestamp of the last check, to honour the interval.
        self._last_checked: dict[str, float] = {}
        #: url -> last state we reported, when there is no database to hold it.
        self._known: dict[str, str] = {}
        self._tick_lock = asyncio.Lock()

    # -- one pass ----------------------------------------------------------

    async def tick(self) -> None:
        """Recheck whatever is due, and send at most one message about it."""
        async with self._tick_lock:
            due = [url for url in self.settings.facebook.group_urls if self._is_due(url)]
            if not due:
                return

            changes: list[tuple[str, GroupAccess]] = []
            async with self.session.lock:
                if not self.session.has_live_context:
                    return
                if await self.session.observe_state() != SessionState.HEALTHY:
                    # The session watchdog owns session-level alerting; this
                    # job simply does not run while a human is needed.
                    log.info("facebook.recheck.skipped_unhealthy")
                    return

                for index, url in enumerate(due):
                    if index and await self.session.observe_state() != SessionState.HEALTHY:
                        log.info("facebook.recheck.aborted_mid_run", checked=index)
                        break
                    access = await self._check(url)
                    if access is None:
                        continue
                    self._last_checked[url] = time.monotonic()
                    if await self._record(url, access):
                        changes.append((url, access))

            if changes:
                await self._notify(changes)

    def _is_due(self, url: str) -> bool:
        last = self._last_checked.get(url)
        if last is None:
            return True
        interval = self.settings.facebook.min_group_recheck_minutes * 60
        return (time.monotonic() - last) >= interval

    async def _check(self, url: str) -> GroupAccess | None:
        """Classify one group. A failure here is not a state change."""
        try:
            return await check_access(self.session.page, url)
        except Exception:
            log.exception("facebook.recheck.check_failed", group_url=url)
            return None

    async def _record(self, url: str, access: GroupAccess) -> bool:
        """Store the new state; return whether it differs from the last one.

        A storage failure must not turn into a silent alert or a repeated one,
        so the in-process map is updated either way.
        """
        previous: str | None = self._known.get(url)
        if self.repo is not None:
            stored = await self.repo.facebook_group_access(url)
            if stored is not None:
                previous = stored
            await self.repo.record_facebook_group_access(url, access.value)

        self._known[url] = access.value
        if previous == access.value:
            return False
        if previous is None and access is GroupAccess.ACCESSIBLE:
            # First sight of a group that is simply fine. "Available again"
            # would be a lie, and a working group is not news -- but a group
            # that is unreadable on first sight very much is.
            return False
        log.info(
            "facebook.recheck.state_changed",
            group_url=url,
            previous=previous,
            current=access.value,
        )
        return True

    async def _notify(self, changes: list[tuple[str, GroupAccess]]) -> None:
        """One message for the whole pass, not one per group."""
        lines = ["Изменился доступ к группам Facebook:", ""]
        lines += [f"{_ACCESS_LABEL.get(access, access.value)}\n{url}" for url, access in changes]
        if all(access is not GroupAccess.ACCESSIBLE for _url, access in changes):
            lines += ["", "Пока группа недоступна, бот её не читает."]
        text = "\n".join(lines)

        for chat_id in dict.fromkeys(self.settings.facebook.admin_telegram_ids):
            try:
                await self.bot.send_message(int(chat_id), text)
            except Exception:
                log.exception("facebook.recheck.send_failed", chat_id=chat_id)

    # -- the loop ----------------------------------------------------------

    async def run(self) -> None:
        """Recheck forever, on the configured interval."""
        interval = max(self.settings.facebook.min_group_recheck_minutes * 60, 60)
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("facebook.recheck.tick_failed")
            await asyncio.sleep(interval)
