"""T8: retention of the Apify raw dataset items (bot/facebook_collector/apify/retention.py)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from bot.facebook_collector.apify.retention import cleanup, main, retention_days

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"),
                              reason="set SYSTEM_TEST_DATABASE_URL to a *_test database")
ROOT = Path(__file__).resolve().parents[1]


def test_retention_days() -> None:
    assert retention_days(None) == 7 and retention_days("") == 7 and retention_days("30") == 30
    for bad in ("0", "366"):
        with pytest.raises(ValueError):
            retention_days(bad)


async def test_main_needs_a_database_url(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert await main(["--dry-run"]) == 2 and "DATABASE_URL" in capsys.readouterr().err


def test_the_host_script_runs() -> None:
    out = subprocess.run([sys.executable, str(ROOT / "scripts/cleanup_apify_raw.py"), "--help"], capture_output=True,
                         text=True, check=True)
    assert "--dry-run" in out.stdout


@pytest.fixture
async def pool():
    import asyncpg

    from tests.test_apify_facebook_postgres import MIGRATIONS

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@needs_db
async def test_only_expired_raw_items_are_deleted_and_runs_stay(pool) -> None:
    from tests.test_apify_facebook_postgres import _batch

    _, batch = await _batch(pool)
    await pool.execute("""insert into apify_runs (batch_id, actor_id, actor_build, run_id, status, input_hash)
                          values ($1::uuid, 'apify/facebook-groups-scraper', '1.0', 'RUN1', 'SUCCEEDED', $2)""",
                       batch, "a" * 64)
    for index, age in enumerate((30, 8, 6, 0)):
        await pool.execute("""insert into apify_raw_items (run_id, item_index, payload, imported_at)
                              values ('RUN1', $1, '{"text": "x"}', now() - make_interval(days => $2))""", index, age)
    dry = await cleanup(pool, 7, dry_run=True)
    assert (dry.expired, dry.deleted) == (2, 0) and "would delete 2" in dry.line()
    assert await pool.fetchval("select count(*) from apify_raw_items") == 4  # dry run: nothing touched
    done = await cleanup(pool, 7, dry_run=False)
    assert (done.expired, done.deleted) == (2, 2) and "deleted 2" in done.line()
    assert sorted(r["item_index"] for r in await pool.fetch("select item_index from apify_raw_items")) == [2, 3]
    assert await pool.fetchval("select count(*) from apify_runs") == 1  # the audit trail stays
    assert (await cleanup(pool, 7, dry_run=False)).deleted == 0
    with pytest.raises(ValueError):
        await cleanup(pool, 0, dry_run=True)


@needs_db
async def test_main_prints_counts_only(pool, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("APIFY_FACEBOOK_RAW_RETENTION_DAYS", "3")
    assert await main(["--dry-run"]) == 0
    assert capsys.readouterr().out.strip() == "apify_raw_items older than 3 days: 0 expired, would delete 0"
