"""Container health probe; no browser is acquired during a health check."""

from __future__ import annotations

import asyncio

from bot.facebook_collector.browser import BrowserSessionClient

from .settings import AgentReachSettings


async def _check() -> bool:
    settings = AgentReachSettings()
    return await BrowserSessionClient(settings.browser_url, settings.browser_token).health()


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(_check()) else 1)
