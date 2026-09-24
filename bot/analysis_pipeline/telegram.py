"""Minimal Telegram sender for durable analysis digest records."""

from __future__ import annotations

import httpx


async def send_digest(token: str, chat_id: int, body: str) -> int:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": body[:4000], "disable_web_page_preview": True},
        )
        response.raise_for_status()
    data = response.json()
    if not data.get("ok") or not data.get("result", {}).get("message_id"):
        raise ValueError("telegram_digest_delivery_failed")
    return int(data["result"]["message_id"])
