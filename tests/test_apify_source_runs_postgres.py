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


# --- the run store (bot/acquisition/apify_source.py) against the table, mocked Apify ------------------------------


@pytest.fixture
async def pool(conn):
    import asyncpg

    from bot.utils import costs

    p = await asyncpg.create_pool(URL, min_size=1, max_size=4)
    try:
        yield p
    finally:
        costs.install(None)
        await p.close()


async def test_store_runs_once_bills_once_and_keeps_the_journal(conn, pool):
    from bot.acquisition.apify_source import ApifySocialRun, PostgresSourceRunStore
    from bot.utils import costs
    from tests.test_apify_source_run import CONFIG, INPUT, Api, Sink, comment, runner

    costs.install(costs.PostgresLedger(pool), budget_usd=5)
    cid = await _campaign(conn)
    api, store, sink = Api(dataset=[comment(n) for n in range(3)]), PostgresSourceRunStore(pool), Sink()
    source: ApifySocialRun = runner(api, store)
    first = await source.run(cid, INPUT, sink, owner_user_id=7)
    again = await source.run(cid, INPUT, sink, owner_user_id=7)
    assert (first.status, first.items_imported, again.status) == ("succeeded", 3, "succeeded")
    assert len(api.launches()) == 1
    saved = await conn.fetchrow("select * from public.apify_source_runs")
    assert (saved["platform"], saved["purpose"], saved["actor_id"], saved["actor_build"], saved["owner_user_id"],
            saved["run_id"], saved["dataset_id"], saved["status"], float(saved["billed_usd"]),
            float(saved["max_charge_usd"]), saved["items_imported"], saved["import_checkpoint"]) == (
        "instagram", "comments", CONFIG.actor_id, "0.1.2", 7, "RUN1", "DS1", "succeeded", 0.05, 0.2, 3, 3)
    assert saved["launched_at"] is not None and saved["finished_at"] is not None
    booked = await conn.fetch("select idempotency_key, cost_usd::float8 as c, code from public.campaign_costs "
                              "where idempotency_key like 'apify:%'")
    assert [(r["idempotency_key"], r["c"], r["code"]) for r in booked] == [("apify:RUN1", 0.05, None)]


async def test_store_launch_unknown_keeps_its_placeholder_and_refusal_reopens(conn, pool):
    import httpx

    from bot.acquisition.apify_source import PostgresSourceRunStore
    from bot.utils import costs
    from tests.test_apify_source_run import INPUT, Api, Sink, runner

    costs.install(costs.PostgresLedger(pool), budget_usd=5)
    cid = await _campaign(conn)
    store = PostgresSourceRunStore(pool)
    lost = Api(launch=httpx.ConnectError("lost"))
    assert (await runner(lost, store).run(cid, INPUT, Sink())).error == "launch_unconfirmed"
    lost.launch = None
    assert (await runner(lost, store).run(cid, INPUT, Sink())).status == "failed"
    assert len(lost.launches()) == 1
    saved = await conn.fetchrow("select status, run_id, error_code, billed_usd::float8 as b from public.apify_source_runs")
    assert (saved["status"], saved["error_code"], saved["b"]) == ("failed", "launch_unconfirmed", 0.2)
    assert saved["run_id"].startswith("pending:")

    other = {**INPUT, "resultsLimit": 5}
    refused = Api(launch=400)
    assert (await runner(refused, store).run(cid, other, Sink())).status == "failed"
    row = await conn.fetchrow("select status, run_id from public.apify_source_runs where run_id is null")
    assert (row["status"], row["run_id"]) == ("failed", None)
    refused.launch = None
    assert (await runner(refused, store).run(cid, other, Sink())).status == "succeeded"
    assert len(refused.launches()) == 2


async def test_store_lets_one_worker_launch(conn, pool):
    from bot.acquisition.apify_source import PostgresSourceRunStore, input_hash
    from tests.test_apify_source_run import CONFIG, INPUT

    cid = await _campaign(conn)
    store = PostgresSourceRunStore(pool)
    run = await store.claim(cid, CONFIG, input_hash(CONFIG.actor_id, INPUT))
    assert run.status == "claimed" and run.run_id is None
    assert await store.mark_launching(run) is not None
    assert await store.mark_launching(run) is None  # only one worker launches
