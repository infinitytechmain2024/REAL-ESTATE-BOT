"""C2: migration 050 (task draft mode "both") and its rollback on a real database.

Skipped unless SYSTEM_TEST_DATABASE_URL names a disposable ``*_test`` database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"),
                                reason="set SYSTEM_TEST_DATABASE_URL to a *_test database")
ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))
ROLLBACK = ROOT / "scripts/rollback/050_task_drafts_mode_both_down.sql"
INSERT = "insert into public.user_task_drafts (telegram_user_id, telegram_chat_id, mode) values ($1, 1, $2)"


@pytest.fixture
async def conn():
    import asyncpg

    c = await asyncpg.connect(URL)
    await c.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await c.execute(path.read_text(encoding="utf-8"))
    try:
        yield c
    finally:
        await c.close()


async def test_both_is_accepted_and_junk_is_not(conn):
    import asyncpg

    for i, mode in enumerate(("real_estate", "investors", "both", None), start=1):
        await conn.execute(INSERT, i, mode)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(INSERT, 9, "other")


async def test_migration_is_idempotent(conn):
    await conn.execute(MIGRATIONS[-1].read_text(encoding="utf-8"))
    await conn.execute(INSERT, 1, "both")


async def test_rollback_refused_with_both_drafts_then_works(conn):
    import asyncpg

    await conn.execute(INSERT, 1, "both")
    with pytest.raises(asyncpg.PostgresError, match="mode both"):
        await conn.execute(ROLLBACK.read_text(encoding="utf-8"))
    await conn.execute("rollback")  # the refused script left its transaction aborted
    await conn.execute("delete from public.user_task_drafts")
    await conn.execute(ROLLBACK.read_text(encoding="utf-8"))
    await conn.execute(ROLLBACK.read_text(encoding="utf-8"))  # repeated rollback does not fail
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(INSERT, 2, "both")
