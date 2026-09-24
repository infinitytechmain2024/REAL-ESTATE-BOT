"""Healthcheck for the collector's browser dependency."""

from __future__ import annotations

import asyncio

from .browser import BrowserSessionClient
from .settings import FacebookCollectorSettings


async def _check() -> None:
    settings = FacebookCollectorSettings()
    if not await BrowserSessionClient(settings.browser_url, settings.browser_token).health():
        raise SystemExit("browser session manager is unhealthy")


if __name__ == "__main__":
    asyncio.run(_check())

