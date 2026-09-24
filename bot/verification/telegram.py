"""Bot API sender for verification notices. Plain text, one optional Mini App button."""

from __future__ import annotations

from typing import Protocol

import httpx


class Notifier(Protocol):
    async def send(self, chat_id: int, text: str, button: tuple[str, str] | None = None) -> None: ...


class TelegramNotifier:
    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self._client = client or httpx.AsyncClient(timeout=20)

    async def send(self, chat_id: int, text: str, button: tuple[str, str] | None = None) -> None:
        body: dict[str, object] = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if button:
            # A Mini App button: Telegram signs who pressed it into the page.
            body["reply_markup"] = {"inline_keyboard": [[{"text": button[0], "web_app": {"url": button[1]}}]]}
        response = await self._client.post(self._url, json=body)
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()
