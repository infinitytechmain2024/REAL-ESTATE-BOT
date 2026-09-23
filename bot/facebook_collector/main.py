"""Intentional worker entry point; orchestration invokes batches by ID later."""

from __future__ import annotations

import asyncio
import os

import asyncpg

from .browser import BrowserSessionClient
from .collector import FacebookBatchCollector
from .reader import FacebookGroupReader
from .settings import FacebookCollectorSettings
from .store import PostgresCollectorStore


async def run_batch(batch_id: str) -> str:
    settings = FacebookCollectorSettings()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        browser = BrowserSessionClient(settings.browser_url, settings.browser_token)
        reader = FacebookGroupReader(browser, max_posts=settings.max_posts_per_group, timeout_seconds=settings.group_timeout_seconds)
        collector = FacebookBatchCollector(
            PostgresCollectorStore(pool), browser, reader, max_posts=settings.max_posts_per_group,
            max_groups=settings.max_groups,
            item_timeout_seconds=settings.group_timeout_seconds, pause_min_seconds=settings.pause_min_seconds,
            pause_max_seconds=settings.pause_max_seconds,
        )
        return await collector.run(batch_id)
    finally:
        await pool.close()


if __name__ == "__main__":
    batch = os.environ.get("FACEBOOK_BATCH_ID")
    if not batch:
        raise SystemExit("FACEBOOK_BATCH_ID is required; this worker runs exactly one queued batch")
    print(asyncio.run(run_batch(batch)))
