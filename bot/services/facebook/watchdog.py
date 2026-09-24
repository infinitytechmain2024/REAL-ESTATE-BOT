"""Passive session monitoring; never launches or navigates the shared browser."""

import asyncio
from uuid import UUID

from aiogram import Bot

from bot.config import Settings
from bot.logging_conf import get_logger
from bot.services.db import SupabaseRepository
from bot.services.facebook.alerts import RECOVERY_TEXT, open_button
from bot.services.facebook.browser import FacebookSession, SessionState
from bot.services.facebook.tokens import TokenStore

log = get_logger(__name__)


class FacebookWatchdog:
    """One incident per unhealthy spell, restored from optional Supabase storage.

    Without Supabase (or during a storage outage), in-process state still
    suppresses repeats. A restart can re-alert when persistence is unavailable.
    Telegram and Postgres cannot commit atomically: delivery failures are logged,
    not blindly retried, since Telegram may have accepted a timed-out request.
    """

    def __init__(
        self,
        session: FacebookSession,
        tokens: TokenStore | None,
        settings: Settings,
        bot: Bot,
        repo: SupabaseRepository | None = None,
    ) -> None:
        self.session = session
        self.tokens = tokens
        self.settings = settings
        self.bot = bot
        self.repo = repo
        self._restored = False
        self._incident_id: UUID | None = None
        self._incident = False
        self._tick_lock = asyncio.Lock()

    async def tick(self) -> None:
        # Serialise timer and manual login ticks, including notification delivery.
        async with self._tick_lock:
            if not self._restored:
                row = await self.repo.current_facebook_incident() if self.repo else None
                if row:
                    self._incident = True
                    self._incident_id = UUID(str(row["id"]))
                self._restored = True
            async with self.session.lock:
                if not self.session.has_live_context:
                    return
                try:
                    state = await self.session.observe_state()
                except Exception:
                    log.exception("facebook.watchdog.observe_failed")
                    state = SessionState.HUMAN_REQUIRED
            if state != SessionState.HEALTHY and not self._incident:
                self._incident = True
                if self.repo is not None:
                    self._incident_id = await self.repo.open_facebook_incident(state.value)
                try:
                    keyboard = await open_button(self.settings, self.tokens)
                except Exception:
                    log.exception("facebook.watchdog.link_failed")
                    keyboard = None
                await self._notify(
                    "Facebook недоступен: нужен вход или проверка. "
                    + (
                        "Нажмите «Открыть Facebook»."
                        if keyboard
                        else "Проверьте окно браузера на машине, где запущен бот."
                    ),
                    reply_markup=keyboard,
                )
            elif state == SessionState.HEALTHY and self._incident:
                self._incident = False
                if self.repo is not None and self._incident_id is not None:
                    await self.repo.resolve_facebook_incident(self._incident_id)
                self._incident_id = None
                if self.tokens is not None:
                    try:
                        await self.tokens.invalidate()
                    except Exception:
                        log.exception("facebook.watchdog.invalidate_failed")
                await self._notify(RECOVERY_TEXT)

    async def _notify(self, text: str, **kwargs: object) -> None:
        for chat_id in dict.fromkeys(self.settings.facebook.admin_telegram_ids):
            try:
                await self.bot.send_message(int(chat_id), text, **kwargs)
            except Exception:
                log.exception("facebook.watchdog.send_failed", chat_id=chat_id)

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("facebook.watchdog.tick_failed")
            await asyncio.sleep(self.settings.facebook.watchdog_interval_seconds)
