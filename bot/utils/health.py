"""Shared probe for container healthchecks: can the service still reach its database?"""

from __future__ import annotations

import asyncio
import os

DB_TIMEOUT_SECONDS = 5.0


async def database_answers(url: str, timeout: float = DB_TIMEOUT_SECONDS) -> bool:
    """Open one connection and run ``select 1``; every step is bounded by ``timeout`` seconds."""
    import asyncpg  # imported late: the module itself must stay importable without the driver

    async def probe() -> bool:
        conn = await asyncpg.connect(url, timeout=timeout)
        try:
            return await conn.fetchval("select 1") == 1
        finally:
            await conn.close()

    try:
        return await asyncio.wait_for(probe(), timeout)
    except Exception:  # noqa: BLE001 - any failure means unhealthy; the reason is not needed here
        return False


def exit_code_for_database() -> int:
    """0 when ``DATABASE_URL`` answers ``select 1`` within 5 seconds, else 1 (the URL is never printed)."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is empty")
        return 1
    if asyncio.run(database_answers(url)):
        return 0
    print("database did not answer select 1 within 5 seconds")
    return 1
