"""Expiring stored rows, because keeping them forever is not a neutral default.

The database holds page text and contact details belonging to people who never
used the bot and never agreed to anything -- see COMPLIANCE.md §2.2. Storage
limitation is the cheapest of the duties that follow from that to satisfy in
code, so it is satisfied here: rows older than the configured window are
deleted on a schedule.

The window is a policy number, not an engineering one. It is configurable and
defaults to something short rather than to "forever"; setting it to 0 keeps
everything, which is a deliberate opt-out an operator has to choose.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import TYPE_CHECKING

from bot.config import Settings
from bot.logging_conf import get_logger

if TYPE_CHECKING:
    from bot.services.db import SupabaseRepository

log = get_logger(__name__)

#: Once a day is plenty: the window is measured in days, so checking more
#: often would only add load without expiring anything sooner.
_INTERVAL_SECONDS = 24 * 60 * 60


class RetentionPurger:
    """Deletes searches and results past the retention window."""

    def __init__(self, settings: Settings, repo: SupabaseRepository) -> None:
        self.settings = settings
        self.repo = repo

    @property
    def enabled(self) -> bool:
        return bool(self.settings.supabase.configured and self.settings.supabase.retention_days)

    async def tick(self) -> None:
        """One purge pass. Never raises: storage trouble is not fatal."""
        if not self.enabled:
            return
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(
            days=self.settings.supabase.retention_days
        )
        try:
            await self.repo.purge_expired(cutoff)
        except Exception:
            log.exception("retention.purge_failed", cutoff=cutoff.isoformat())

    async def run(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(_INTERVAL_SECONDS)
