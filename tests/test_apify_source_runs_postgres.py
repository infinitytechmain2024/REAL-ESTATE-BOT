"""P1.1: migration 051 (``apify_source_runs``), its rollback and the run store on a real database.

Skipped unless SYSTEM_TEST_DATABASE_URL names a disposable ``*_test`` database. No Apify call is made.
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
MIGRATION = ROOT / "bot/services/db/migrations/051_apify_source_runs.sql"
ROLLBACK = ROOT / "scripts/rollback/051_apify_source_runs_down.sql"
HASH = "a" * 64
INSERT = """insert into public.apify_source_runs (campaign_id, platform, purpose, actor_id, actor_build, input_hash,
                                                   max_charge_usd, run_id, status)
            values ($1::uuid, $2, $3, 'apify/instagram-scraper', '0.1.2', $4, 0.2, $5, $6)"""


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


async def _campaign(conn) -> str:
    return str(await conn.fetchval(
        """insert into public.campaigns (telegram_chat_id, requested_by, source_text, plan)
           values (1, 2, 'x', '{}') returning id"""))


async def test_051_is_the_last_migration_and_is_idempotent(conn):
    assert MIGRATIONS[-1] == MIGRATION
    await conn.execute(MIGRATION.read_text(encoding="utf-8"))
    cid = await _campaign(conn)
    await conn.execute(INSERT, cid, "instagram", "posts", HASH, None, "claimed")
    await conn.execute(MIGRATION.read_text(encoding="utf-8"))  # a second apply keeps the row
    assert await conn.fetchval("select count(*) from public.apify_source_runs") == 1


async def test_one_row_per_campaign_purpose_and_input(conn):
    import asyncpg

    cid = await _campaign(conn)
    await conn.execute(INSERT, cid, "instagram", "posts", HASH, None, "claimed")
    with pytest.raises(asyncpg.UniqueViolationError):
        await conn.execute(INSERT, cid, "instagram", "posts", HASH, None, "claimed")
    await conn.execute(INSERT, cid, "instagram", "comments", HASH, None, "claimed")   # another purpose
    await conn.execute(INSERT, cid, "instagram", "posts", "b" * 64, None, "claimed")  # another input


@pytest.mark.parametrize(("run_id", "status"), [
    ("pending:" + "0" * 32, "launching"),
    ("aBc123XyZ", "running"),
    ("aBc123XyZ", "succeeded"),
    ("pending:" + "0" * 32, "failed"),     # a launch that never confirmed: closed, never relaunched
    (None, "failed"),                       # refused outright, nothing paid
])
async def test_run_id_and_status_that_fit(conn, run_id, status):
    await conn.execute(INSERT, await _campaign(conn), "tiktok", "posts", HASH, run_id, status)


@pytest.mark.parametrize(("platform", "run_id", "status"), [
    ("twitter", None, "claimed"),                    # unknown network
    ("instagram", "pending:xyz", "launching"),      # not a uuid placeholder
    ("instagram", "unknown-1", "running"),          # only Apify ids or pending:<uuid>
    ("instagram", "aBc123", "claimed"),              # claimed has no run id yet
    ("instagram", None, "launching"),                # launching holds its placeholder
    ("instagram", "pending:" + "0" * 32, "running"),  # running needs the real id
    ("instagram", None, "succeeded"),
    ("instagram", None, "bogus"),
])
async def test_rows_that_do_not_fit_are_refused(conn, platform, run_id, status):
    import asyncpg

    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(INSERT, await _campaign(conn), platform, "posts", HASH, run_id, status)


async def test_latest_build_and_bad_hash_are_refused(conn):
    import asyncpg

    cid = await _campaign(conn)
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(INSERT.replace("'0.1.2'", "'latest'"), cid, "instagram", "posts", HASH, None, "claimed")
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(INSERT, cid, "instagram", "posts", "nothex", None, "claimed")


async def test_rollback_refused_with_rows_then_works(conn):
    import asyncpg

    cid = await _campaign(conn)
    await conn.execute(INSERT, cid, "linkedin", "people", HASH, None, "claimed")
    with pytest.raises(asyncpg.PostgresError, match="apify_source_runs still has rows"):
        await conn.execute(ROLLBACK.read_text(encoding="utf-8"))
    await conn.execute("rollback")  # the refused script left its transaction aborted
    assert await conn.fetchval("select count(*) from public.apify_source_runs") == 1
    await conn.execute("delete from public.apify_source_runs")
    await conn.execute(ROLLBACK.read_text(encoding="utf-8"))
    await conn.execute(ROLLBACK.read_text(encoding="utf-8"))  # repeated rollback does not fail
    assert await conn.fetchval("select to_regclass('public.apify_source_runs')") is None
