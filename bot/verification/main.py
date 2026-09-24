"""Verification service: the /verify page plus the job watcher.

Listens on the private Docker network only; Caddy is its HTTPS front.
Without a public URL it stays idle (health endpoint only) instead of failing.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiohttp import web

from bot.facebook_collector.browser import BrowserSessionClient

from .browser import BrowserLiveClient, RecoveryWatchdog
from .service import FlowConfig, VerificationService
from .settings import VerificationSettings
from .store import PostgresVerificationStore
from .telegram import TelegramNotifier
from .web import create_app

log = logging.getLogger(__name__)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = VerificationSettings.from_env()
    store = PostgresVerificationStore(settings.database_url)
    await store.connect()
    notifier = TelegramNotifier(settings.telegram_token)
    service = VerificationService(
        store,
        BrowserLiveClient(settings.browser_session_url, settings.browser_session_api_token),
        # Navigation plus Facebook's feed wait can take well over a minute.
        RecoveryWatchdog(BrowserSessionClient(settings.browser_session_url, settings.browser_session_api_token, timeout_seconds=120)),
        notifier,
        FlowConfig(
            public_url=settings.public_url, operator_ids=settings.operator_ids, owner_id=settings.owner_id,
            bot_token=settings.telegram_token, token_minutes=settings.token_minutes,
            session_minutes=settings.session_minutes, job_hours=settings.job_hours,
            renotify_minutes=settings.renotify_minutes, live_minutes=settings.live_minutes,
        ),
    )
    runner = web.AppRunner(create_app(service, settings.novnc_url))
    await runner.setup()
    await web.TCPSite(runner, settings.listen_host, settings.listen_port).start()
    if not settings.public_url:
        log.warning("verification.disabled", extra={"hint": "set VERIFICATION_PUBLIC_URL or LIVE_VIEW_PUBLIC_URL to an https:// origin"})
    else:
        log.info("verification.started", extra={"public_url": settings.public_url})
    try:
        while True:
            try:
                if settings.public_url:
                    await service.tick()
            except Exception:
                log.exception("verification.tick_failed")
            await asyncio.sleep(settings.poll_seconds)
    finally:
        with suppress(Exception):
            await runner.cleanup()
        await notifier.aclose()
        await store.close()


if __name__ == "__main__":
    asyncio.run(run())
