"""Run one explicit queued Scrapling acquisition run and exit."""

from __future__ import annotations

import asyncio
import json

from .connector import ScraplingConnector
from .settings import ScraplingSettings
from .store import PostgresScraplingStore


async def run_once() -> dict[str, object]:
    settings = ScraplingSettings()
    if not settings.run_id:
        raise SystemExit("SCRAPLING_RUN_ID is required; this worker runs exactly one queued Scrapling run")
    store = PostgresScraplingStore(settings.database_url)
    await store.connect()
    connector = ScraplingConnector(
        request_timeout_seconds=settings.request_timeout_seconds,
        max_content_bytes=settings.max_content_bytes,
        max_content_chars=settings.max_content_chars,
        user_agent=settings.user_agent,
    )
    try:
        claimed = await store.claim(settings.run_id)
        if claimed is None:
            raise SystemExit("run is not a queued active Scrapling acquisition run")
        result = await connector.fetch(claimed.task)
        await store.complete(claimed, result)
        return result.as_dict()
    finally:
        await connector.aclose()
        await store.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run_once()), ensure_ascii=False))
