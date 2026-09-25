"""Long-running Scrapling worker: runs /run-queued website reads one at a time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .connector import ScraplingConnector
from .store import PostgresScraplingStore

log = logging.getLogger(__name__)


async def step(store: PostgresScraplingStore, connector: ScraplingConnector) -> bool:
    """Run at most one queued run; False when none was waiting."""
    run_id = await store.next_queued()
    if run_id is None:
        return False
    claimed = await store.claim(run_id)
    if claimed is None:
        return True  # another worker took it
    result = await connector.fetch(claimed.task)
    await store.complete(claimed, result)
    log.info("scrapling.finished %s %s", run_id, result.outcome.value)
    return True


async def serve(store: PostgresScraplingStore, connector: ScraplingConnector, *, poll_seconds: float,
                sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    while True:
        try:
            if await step(store, connector):
                continue
        except Exception:
            log.exception("scrapling.step_failed")
        await sleep(poll_seconds)


async def main() -> None:
    from .settings import ScraplingSettings

    settings = ScraplingSettings()
    store = PostgresScraplingStore(settings.database_url)
    await store.connect()
    connector = ScraplingConnector(
        request_timeout_seconds=settings.request_timeout_seconds, max_content_bytes=settings.max_content_bytes,
        max_content_chars=settings.max_content_chars, user_agent=settings.user_agent,
    )
    try:
        log.info("scrapling.ready")
        await serve(store, connector, poll_seconds=settings.poll_seconds)
    finally:
        await connector.aclose()
        await store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
