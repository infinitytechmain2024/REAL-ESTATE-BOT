"""Dispatcher and collector behaviour against the real migrations 001-005.

Skipped unless ``ORCHESTRA_TEST_DATABASE_URL`` names a disposable database
whose name ends in ``_test``: every test drops and recreates its ``public``
schema. Example::

    createdb monitoring_test
    ORCHESTRA_TEST_DATABASE_URL=postgresql://localhost/monitoring_test pytest tests/test_orchestra_postgres.py
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import pytest_asyncio

from bot.facebook_collector.models import BatchCancelled, GroupState
from bot.facebook_collector.store import PostgresCollectorStore
from bot.orchestra.dispatcher import OrchestraDispatcher
from bot.orchestra.models import ClaimLost, ConfirmedCommand
from bot.orchestra.parser import parse_run
from bot.orchestra.store import PostgresOrchestraStore

DATABASE_URL = os.environ.get("ORCHESTRA_TEST_DATABASE_URL", "")
MIGRATIONS = Path(__file__).parents[1] / "bot" / "services" / "db" / "migrations"
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="ORCHESTRA_TEST_DATABASE_URL is not set")
GROUP = "https://www.facebook.com/groups/one"


@pytest_asyncio.fixture
async def store() -> AsyncIterator[PostgresOrchestraStore]:
    if not urlsplit(DATABASE_URL).path.rstrip("/").endswith("_test"):
        pytest.fail("ORCHESTRA_TEST_DATABASE_URL must name a disposable *_test database")
    orchestra = PostgresOrchestraStore(DATABASE_URL)
    await orchestra.connect()
    pool = orchestra.pool
    assert pool is not None
    await pool.execute("drop schema public cascade; create schema public")
    for migration in sorted(MIGRATIONS.glob("00[1-5]_*.sql")):
        await pool.execute(migration.read_text(encoding="utf-8"))
    await pool.execute(
        """insert into browser_profiles(profile_name, platform, storage_locator) values ('fb', 'facebook', 'x');
           update browser_profiles set state='ready'"""
    )
    try:
        yield orchestra
    finally:
        await orchestra.close()


class Operator:
    def __init__(self, store: PostgresOrchestraStore) -> None:
        self.dispatcher = OrchestraDispatcher(store, operator_ids=frozenset({42}), notifier=self._notify)
        self.notices: list[str] = []
        self.message_id = 0

    async def _notify(self, _chat_id: int, text: str) -> None:
        self.notices.append(text)

    async def send(self, command: str, arguments: str) -> str:
        self.message_id += 1
        await self.dispatcher.enqueue(ConfirmedCommand(command, arguments, 1, 42, self.message_id))
        assert await self.dispatcher.process_once()
        return self.notices[-1]


def _pool(store: PostgresOrchestraStore):  # type: ignore[no-untyped-def]
    assert store.pool is not None
    return store.pool


@pytest.mark.asyncio
async def test_cancelling_a_running_batch_stops_the_collector_and_frees_the_profile(store: PostgresOrchestraStore) -> None:
    operator, pool = Operator(store), _pool(store)
    await operator.send("run", f"facebook-groups {GROUP} {GROUP}/two")
    batch_id = await pool.fetchval("select id from acquisition_batches")
    collector = PostgresCollectorStore(pool)
    plan = await collector.load_plan(str(batch_id))
    batch_run = await collector.start(plan)
    run_id = await collector.start_item(plan.items[0], batch_run, plan.browser_profile_id, max_runtime_seconds=30)

    assert "cancelled (1 affected)" in await operator.send("cancel", f"batch:{batch_id}")
    await collector.finish_item(plan.items[0], run_id, "succeeded", GroupState.ACTIVE)
    with pytest.raises(BatchCancelled):
        await collector.start_item(plan.items[1], batch_run, plan.browser_profile_id, max_runtime_seconds=30)
    await collector.finish_batch(plan, batch_run, "cancelled", "operator_cancelled")

    assert await pool.fetchval("select state from acquisition_batches") == "cancelled"
    assert await pool.fetchval("select state from batch_runs") == "cancelled"
    assert await pool.fetchval("select state from browser_profiles") == "ready"
    assert "queued Facebook batch" in await operator.send("run", "facebook-groups https://www.facebook.com/groups/next")


@pytest.mark.asyncio
async def test_a_challenge_after_cancellation_still_quarantines_the_profile(store: PostgresOrchestraStore) -> None:
    operator, pool = Operator(store), _pool(store)
    await operator.send("run", f"facebook-groups {GROUP}")
    batch_id = await pool.fetchval("select id from acquisition_batches")
    collector = PostgresCollectorStore(pool)
    plan = await collector.load_plan(str(batch_id))
    batch_run = await collector.start(plan)
    run_id = await collector.start_item(plan.items[0], batch_run, plan.browser_profile_id, max_runtime_seconds=30)
    await operator.send("cancel", f"batch:{batch_id}")

    await collector.challenge(plan, batch_run, plan.items[0], run_id, "checkpoint")
    assert await pool.fetchval("select state from browser_profiles") == "human_verification_required"
    assert await pool.fetchval("select count(*) from verification_jobs") == 1


@pytest.mark.asyncio
async def test_run_does_not_override_a_paused_source(store: PostgresOrchestraStore) -> None:
    operator, pool = Operator(store), _pool(store)
    await operator.send("run", f"facebook-groups {GROUP}")
    source_id = await pool.fetchval("select id from monitoring_sources")
    await operator.send("pause", f"source:{source_id}")

    assert "is paused" in await operator.send("run", f"facebook-groups {GROUP}")
    assert await pool.fetchval("select count(*) from acquisition_batches") == 1


@pytest.mark.asyncio
async def test_a_lost_claim_rolls_back_the_planned_batch(store: PostgresOrchestraStore) -> None:
    pool = _pool(store)
    await store.enqueue(ConfirmedCommand("run", f"facebook-groups {GROUP}", 1, 42, 1))
    item = await store.claim_next(lease_seconds=30)
    assert item is not None
    await pool.execute("update orchestration_commands set lease_expires_at=now() - interval '1 second'")
    await store.reclaim_expired()

    with pytest.raises(ClaimLost):
        await store.plan_run(parse_run(item.arguments), item, actor="telegram:42")
    assert await pool.fetchval("select count(*) from acquisition_batches") == 0
    assert await pool.fetchval("select state from orchestration_commands") == "queued"


@pytest.mark.asyncio
async def test_lifecycle_changes_are_attributed_to_the_telegram_user(store: PostgresOrchestraStore) -> None:
    operator, pool = Operator(store), _pool(store)
    await operator.send("run", f"facebook-groups {GROUP}")
    source_id = await pool.fetchval("select id from monitoring_sources")
    await operator.send("pause", f"source:{source_id}")

    actors = await pool.fetch(
        "select distinct actor from orchestration_audit_log where entity_type in ('monitoring_sources', 'orchestration_commands')"
    )
    assert {row["actor"] for row in actors} <= {"telegram:42", "orchestra:dispatcher"}


@pytest.mark.asyncio
async def test_a_batch_whose_collector_died_is_failed_and_frees_the_profile(store: PostgresOrchestraStore) -> None:
    operator, pool = Operator(store), _pool(store)
    await operator.send("run", f"facebook-groups {GROUP} {GROUP}/two")
    batch_id = await pool.fetchval("select id from acquisition_batches")
    collector = PostgresCollectorStore(pool)
    plan = await collector.load_plan(str(batch_id))
    batch_run = await collector.start(plan)
    await collector.start_item(plan.items[0], batch_run, plan.browser_profile_id, max_runtime_seconds=30)

    assert await store.reap_stale_batches(stale_seconds=600) == []
    # Simulate ten minutes of silence (the trigger stamps updated_at, so disable it).
    await pool.execute(
        """alter table batch_runs disable trigger user; alter table acquisition_batch_items disable trigger user;
           alter table acquisition_runs disable trigger user;
           update batch_runs set updated_at = now() - interval '11 minutes';
           update acquisition_batch_items set updated_at = now() - interval '11 minutes';
           update acquisition_runs set updated_at = now() - interval '11 minutes';
           alter table batch_runs enable trigger user; alter table acquisition_batch_items enable trigger user;
           alter table acquisition_runs enable trigger user;"""
    )
    assert await store.reap_stale_batches(stale_seconds=600) == [str(batch_id)]
    assert await pool.fetchval("select state from acquisition_batches") == "failed"
    assert await pool.fetchval("select state from batch_runs") == "failed"
    assert await pool.fetchval("select state from acquisition_batch_items where sequence_no = 1") == "failed"
    assert await pool.fetchval("select state from acquisition_batch_items where sequence_no = 2") == "skipped"
    assert await pool.fetchval("select state from browser_profiles") == "ready"
    assert "queued Facebook batch" in await operator.send("run", "facebook-groups https://www.facebook.com/groups/next")
