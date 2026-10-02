"""Ask for a login when a search needs a platform whose browser profile is not signed in.

The bot's social accounts (LinkedIn, X, TikTok, Instagram) are signed in by an
owner in the live browser window (``/login <platform>``, «🔐 Вход в соцсети»).
When a running search needs one of them and no profile is ready, every owner
gets one message with the button «🔐 Войти в …» (at most once per platform in
``every_hours``), and the person who gave the task -- when not an owner -- is
told once per search that this platform waits for a login while the other
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
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.sender, self.owner_ids = sender, frozenset(owner_ids)
        self.every, self.now = timedelta(hours=every_hours), now
        self._owners_asked: dict[str, datetime] = {}
        self._told: set[tuple[str, str]] = set()  # (campaign id, platform)

    async def need(self, platform: str, campaigns: Sequence[Any]) -> None:
        """``campaigns`` (with ``id``, ``chat_id``, ``requested_by``) wait for ``platform``'s login."""
        if not campaigns:
            return
        last = self._owners_asked.get(platform)
        if self.owner_ids and (last is None or self.now() - last >= self.every):
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
