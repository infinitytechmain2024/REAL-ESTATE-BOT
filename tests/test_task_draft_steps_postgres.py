"""Migration 031: user_task_drafts.step accepts the 'ask' and 'target' steps the intake saves.

Skipped unless SYSTEM_TEST_DATABASE_URL names a disposable ``*_test`` database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bot.control_plane.intake import Draft, PostgresIntakeStore

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"), reason="set SYSTEM_TEST_DATABASE_URL to a *_test database")
MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))


async def test_ask_and_target_steps_are_saved() -> None:
    import asyncpg

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=2)
    try:
        class Owner:
            def _pool(self):
                return pool

        store = PostgresIntakeStore(Owner())
        for step in ("ask", "target", "summary", "idle"):
            await store.save(Draft(user_id=7, chat_id=-1, mode="investors", step=step))
            saved = await store.get(7)
            assert saved is not None and saved.step == step
        with pytest.raises(asyncpg.CheckViolationError):
            await store.save(Draft(user_id=7, chat_id=-1, mode="investors", step="bogus"))
    finally:
        await pool.close()
