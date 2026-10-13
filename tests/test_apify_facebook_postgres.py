"""T4: migration 046 and the Apify provider's Postgres store on a real database (mocked Apify API).

Skipped unless SYSTEM_TEST_DATABASE_URL names a disposable ``*_test`` database.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from bot.acquisition.apify_client import ApifyHttp
from bot.campaign import PostgresCampaignStore, plan_campaign
from bot.facebook_collector.apify import ApifyFacebookConfig, ApifyFacebookProvider
from bot.facebook_collector.apify.store import PostgresApifyFacebookStore
from bot.utils import costs
from tests.test_apify_facebook import G1, G2, TOKEN, Api, item

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"),
                                reason="set SYSTEM_TEST_DATABASE_URL to a *_test database")
ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))
ROLLBACK = ROOT / "scripts/rollback/046_facebook_apify_runs_down.sql"
CONFIG = ApifyFacebookConfig(build="1.2.3", page_size=2, checkpoint_every=1, max_usd_per_run=1.0)


@pytest.fixture
async def pool():
    import asyncpg

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        costs.install(None)
        await pool.close()


async def _batch(pool) -> tuple[str, str]:
    cid = await PostgresCampaignStore(pool).create(plan_campaign("участок в Мадриде от 2000 м², покупка"), chat_id=1,
                                                   requested_by=1, source_text="участок", actor="test")
    sources = [await pool.fetchval(
        """insert into monitoring_sources (platform, source_kind, vertical, canonical_url, display_name, acquisition_method,
                                           state) values ('facebook', 'group', 'real_estate', $1, $1, 'facebook_connector',
                                           'active') returning id""", url) for url in (G1, G2)]
    batch = await pool.fetchval(
        """insert into acquisition_batches (platform, acquisition_method, vertical, state, max_items, campaign_id)
           values ('facebook', 'facebook_connector', 'real_estate', 'queued', 20, $1::uuid) returning id""", cid)
    for n, source in enumerate(sources, start=1):
        await pool.execute("insert into acquisition_batch_items (batch_id, source_id, sequence_no) values ($1, $2, $3)",
                           batch, source, n)
    return cid, str(batch)


def _provider(pool, api: Api) -> ApifyFacebookProvider:
    async def no_sleep(_: float) -> None:
        return None

    http = ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(api)), sleep=no_sleep, jitter=lambda: 0)
    return ApifyFacebookProvider(PostgresApifyFacebookStore(pool), http, CONFIG)


async def test_a_mocked_run_lands_in_the_batch_tables_and_collected_posts(pool) -> None:
    costs.install(costs.PostgresLedger(pool), budget_usd=5)
    cid, batch = await _batch(pool)
    api = Api(dataset=[item(1), item(2), item(3, G2), {"facebookUrl": G2, "error": "Group not found"}])
    assert await _provider(pool, api).run(batch) == "succeeded"
    run = await pool.fetchrow("select * from apify_runs where batch_id = $1::uuid", batch)
    assert (run["run_id"], run["status"], run["campaign_id"].hex) == ("RUN1", "SUCCEEDED", cid.replace("-", ""))
    assert (run["import_checkpoint"], run["items_imported"], float(run["billed_usd"])) == (4, 3, 0.4)
    assert run["finished_at"] is not None and run["actor_build"] == "1.2.3"
    assert await pool.fetchval("select count(*) from apify_raw_items where run_id = 'RUN1'") == 4
    posts = await pool.fetch("select platform_post_id, state, raw_payload->>'provider' as p from collected_posts order by 1")
    assert [(r["platform_post_id"], r["state"], r["p"]) for r in posts] == [
        ("1000001", "normalised", "apify"), ("1000002", "normalised", "apify"), ("1000003", "normalised", "apify")]
    runs = await pool.fetch("select acquisition_method, state, error_code from acquisition_runs order by created_at")
    assert {(r["acquisition_method"], r["state"]) for r in runs} == {("apify", "succeeded"), ("apify", "failed")}
    items = await pool.fetch("select state, last_error_code from acquisition_batch_items order by sequence_no")
    assert [(r["state"], r["last_error_code"]) for r in items] == [("succeeded", None), ("failed", "group_not_found")]
    assert await pool.fetchval("select state from acquisition_batches where id = $1::uuid", batch) == "succeeded"
    assert await pool.fetchval("select stop_reason from batch_runs where batch_id = $1::uuid", batch) is None
    group_state = await pool.fetchval(
        "select configuration->>'facebook_group_state' from monitoring_sources where canonical_url = $1", G2)
    assert group_state == "INACCESSIBLE"
    assert await PostgresApifyFacebookStore(pool).spent(cid) == pytest.approx(0.4)
    assert (await costs.ledger().summary(cid)).by_stage == {"api": pytest.approx(0.4)}


async def test_a_lost_launch_is_stored_and_never_relaunched(pool) -> None:
    _, batch = await _batch(pool)
    api = Api(launch=httpx.ConnectError("reset"))
    assert await _provider(pool, api).run(batch) == "failed"
    row = await pool.fetchrow("select run_id, status, finished_at from apify_runs where batch_id = $1::uuid", batch)
    assert row["status"] == "LAUNCH_UNKNOWN" and row["run_id"] == f"unknown-{batch}" and row["finished_at"] is not None
    assert await pool.fetchval("select state from acquisition_batches where id = $1::uuid", batch) == "failed"
    assert api.launches() == 1


async def test_raw_items_are_stored_once_and_checks_hold(pool) -> None:
    import asyncpg

    store = PostgresApifyFacebookStore(pool)
    _, batch = await _batch(pool)
    await store.save_run(batch, None, actor_id="apify/facebook-groups-scraper", build="1.0", run_id="R1",
                         status="RUNNING", input_hash="a" * 64)
    await store.save_run(batch, None, actor_id="apify/facebook-groups-scraper", build="1.0", run_id="R2",
                         status="RUNNING", input_hash="a" * 64)  # one run per batch: the second is ignored
    assert (await store.get_run(batch)).run_id == "R1"
    assert await store.save_raw("R1", 0, {"text": "x"}) is True
    assert await store.save_raw("R1", 0, {"text": "x"}) is False
    await store.update_run("R1", import_checkpoint=7, items_imported=5)
    assert (await store.get_run(batch)).import_checkpoint == 7
    with pytest.raises(ValueError):
        await store.update_run("R1", run_id="evil")
    with pytest.raises(asyncpg.CheckViolationError):
        await store.update_run("R1", status="BOGUS")
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("""insert into apify_runs (batch_id, actor_id, actor_build, run_id, status, input_hash)
                              values ($1::uuid, 'a/b', 'latest', 'R9', 'RUNNING', $2)""", batch, "b" * 64)
    assert await pool.fetchval("select acquisition_provider from campaigns limit 1") == "local"


async def test_the_rollback_undoes_046_and_the_migration_applies_again(pool) -> None:
    import asyncpg

    up = (ROOT / "bot/services/db/migrations/046_facebook_apify_runs.sql").read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(ROLLBACK.read_text(encoding="utf-8"))
        assert await conn.fetchval("select to_regclass('public.apify_runs')") is None
        assert await conn.fetchval("""select count(*) from information_schema.columns
                                       where table_name = 'campaigns' and column_name = 'acquisition_provider'""") == 0
        await conn.execute(up)
        await conn.execute(up)  # idempotent, as every migration
        assert await conn.fetchval("select to_regclass('public.apify_runs')") is not None
    await _batch(pool)
    source = await pool.fetchval("select id from monitoring_sources limit 1")
    await pool.execute("insert into acquisition_runs (source_id, acquisition_method, state) values ($1, 'apify', 'queued')",
                       source)
    with pytest.raises(asyncpg.RaiseError, match="apify rows"):
        await pool.execute(ROLLBACK.read_text(encoding="utf-8"))
