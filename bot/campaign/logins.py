"""Ask for a login when a search needs a platform whose browser profile is not signed in.

The bot's social accounts (Facebook, LinkedIn, X, TikTok, Instagram) are signed
in by an operator in the live browser window. When a running search needs one of
them and no profile is ready, the need is recorded (``login_requests``, migration
031) and the Telegram control plane's live-view watcher sends every operator the
login link itself -- «Открыть браузер», the same message as for a Facebook
checkpoint (``bot.control_plane.live_view``). Without that store (tests, no
database) every owner gets a «🔐 Войти в …» button instead, at most once per
platform in ``every_hours``. The person who gave the task -- when not an owner --
is told once per search that this platform waits for a login while the other
sources keep searching. Nothing is ever typed into a login form by the bot.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

log = logging.getLogger(__name__)

NAMES = {"facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok", "linkedin": "LinkedIn",
         "x": "X (Twitter)"}


class LoginRequests(Protocol):
    async def need(self, platform: str, searches: int) -> None: ...


class PostgresLoginRequests:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def need(self, platform: str, searches: int) -> None:
        await self.pool.execute(
            """insert into login_requests (platform, searches) values ($1, $2)
               on conflict (platform) do update set searches = excluded.searches, last_needed_at = now()""",
            platform, searches)


class Sender(Protocol):
    async def send(self, chat_id: int, text: str) -> int: ...
    async def send_buttons(self, chat_id: int, text: str, buttons: Sequence[tuple[str, str]]) -> int: ...


def owner_text(platform: str, waiting: int) -> str:
    name = NAMES.get(platform, platform)
    searches = f"{waiting} поиск" + ("а" if 2 <= waiting % 10 <= 4 and not 12 <= waiting % 100 <= 14 else
                                     "" if waiting % 10 == 1 and waiting % 100 != 11 else "ов")
    return (f"Нужен вход в {name}: его ждут {searches}. Нажмите кнопку, войдите в аккаунт в открывшемся окне "
            f"и нажмите «Готово». Бот сам ничего не вводит.")


def user_text(platform: str) -> str:
    name = NAMES.get(platform, platform)
    return (f"{name} пока недоступен: администратор получил запрос на вход в аккаунт. "
            f"Остальные источники продолжают поиск, {name} подключится после входа.")


class LoginPrompts:
    def __init__(self, sender: Sender, owner_ids: Iterable[int], *, every_hours: float = 12,
                 requests: LoginRequests | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.sender, self.owner_ids, self.requests = sender, frozenset(owner_ids), requests
        self.every, self.now = timedelta(hours=every_hours), now
        self._owners_asked: dict[str, datetime] = {}
        self._told: set[tuple[str, str]] = set()  # (campaign id, platform)

    async def need(self, platform: str, campaigns: Sequence[Any]) -> None:
        """``campaigns`` (with ``id``, ``chat_id``, ``requested_by``) wait for ``platform``'s login."""
        if not campaigns:
            return
        if self.requests is not None:
            try:  # the control plane sends the operators the login link (bot.control_plane.live_view)
                await self.requests.need(platform, len(campaigns))
            except Exception:  # noqa: BLE001 - recorded again on the next tick
                log.warning("campaign.login_request_failed", extra={"platform": platform})
        last = self._owners_asked.get(platform)
        if self.requests is None and self.owner_ids and (last is None or self.now() - last >= self.every):
            self._owners_asked[platform] = self.now()
            label = f"🔐 Войти в {NAMES.get(platform, platform)}"
            for owner in sorted(self.owner_ids):
                try:
                    await self.sender.send_buttons(owner, owner_text(platform, len(campaigns)),
                                                   ((label, f"login:go:{platform}"),))
                except Exception:  # noqa: BLE001 - one unreachable owner must not stop the rest
                    log.warning("campaign.login_prompt_failed", extra={"platform": platform, "owner": owner})
            log.info("campaign.login_needed", extra={"platform": platform, "searches": len(campaigns)})
        for campaign in campaigns:
            key = (campaign.id, platform)
            if key in self._told or campaign.requested_by in self.owner_ids:
                continue
            self._told.add(key)
            try:
                await self.sender.send(campaign.chat_id, user_text(platform))
            except Exception:  # noqa: BLE001 - the status line still says the platform waits
                log.warning("campaign.login_notice_failed", extra={"campaign_id": campaign.id})
