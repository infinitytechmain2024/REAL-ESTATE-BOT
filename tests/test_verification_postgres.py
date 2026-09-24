"""The verification flow on a real PostgreSQL with migrations 001-009.

The job is created by the Facebook collector's own challenge code, so this is
the production path end to end. Skipped unless VERIFICATION_TEST_DATABASE_URL
names a disposable database whose name ends in ``_test``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bot.facebook_collector.store import PostgresCollectorStore
from bot.verification.models import Recovery
from bot.verification.service import ActionRefused, FlowConfig, VerificationService
from bot.verification.store import PostgresVerificationStore
from tests.test_verification_flow import (
    LOGIN,
    OPERATOR,
    OWNER,
    FakeLive,
    FakeNotifier,
    FakeWatchdog,
    token_of,
)

URL = os.environ.get("VERIFICATION_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"), reason="set VERIFICATION_TEST_DATABASE_URL to a *_test database")
MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "bot/services/db/migrations").glob("00*.sql"))


@pytest.fixture
async def db():
    import asyncpg

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=4)
    store = PostgresVerificationStore(URL)
    await store.connect()
    try:
        yield pool, store
    finally:
        await store.close()
        await pool.close()


async def challenged(pool, *, reason: str = "facebook_url:/checkpoint") -> dict[str, str]:
    """Queue a two-group batch and stop it on a challenge exactly as the collector does."""
    profile = await pool.fetchval("insert into browser_profiles(profile_name, platform, storage_locator, state) values('facebook-main','facebook','volume:x','ready') returning id::text")
    sources = [await pool.fetchval(
        "insert into monitoring_sources(platform, source_kind, vertical, canonical_url, acquisition_method, state) values('facebook','group','both',$1,'facebook_connector','active') returning id::text",
        f"https://www.facebook.com/groups/g{n}") for n in (1, 2)]
    batch = await pool.fetchval("insert into acquisition_batches(platform, acquisition_method, vertical, state, max_items) values('facebook','facebook_connector','both','planned',2) returning id::text")
    for n, source in enumerate(sources, 1):
        await pool.execute("insert into acquisition_batch_items(batch_id, source_id, sequence_no) values($1,$2,$3)", batch, source, n)
    await pool.execute("update acquisition_batches set state='queued' where id=$1", batch)
    collector = PostgresCollectorStore(pool)
    plan = await collector.load_plan(batch)
    batch_run = await collector.start(plan)
    run = await collector.start_item(plan.items[0], batch_run, plan.browser_profile_id, max_runtime_seconds=90)
    await collector.challenge(plan, batch_run, plan.items[0], run, reason)
    return {"profile": profile, "batch": batch, "batch_run": batch_run, "run": run, "source": sources[0]}


def service_for(store: PostgresVerificationStore, *recoveries: Recovery, live: FakeLive | None = None):
    notifier = FakeNotifier()
    service = VerificationService(
        store, live or FakeLive(), FakeWatchdog(*recoveries), notifier,
        FlowConfig(public_url="https://v.tail1.ts.net", operator_ids=frozenset({OWNER, OPERATOR}), owner_id=OWNER, tailscale_logins=frozenset({LOGIN})),
    )
    return service, notifier


async def states(pool, ids: dict[str, str]) -> dict[str, str]:
    row = await pool.fetchrow(
        """select (select state from browser_profiles where id=$1::uuid) profile,
                  (select state from acquisition_batches where id=$2::uuid) batch,
                  (select state from batch_runs where id=$3::uuid) batch_run,
                  (select state from acquisition_runs where id=$4::uuid) run,
                  (select state from monitoring_sources where id=$5::uuid) source,
                  (select string_agg(state, ',' order by sequence_no) from acquisition_batch_items where batch_id=$2::uuid) items""",
        ids["profile"], ids["batch"], ids["batch_run"], ids["run"], ids["source"])
    return dict(row)


@pytest.mark.asyncio
async def test_challenge_to_resumed_batch_on_the_real_schema(db) -> None:
    pool, store = db
    ids = await challenged(pool)
    service, notifier = service_for(store, Recovery(True))
    await service.tick()
    job = (await store.unannounced_jobs()) or [await store.get_job(await pool.fetchval("select id::text from verification_jobs"))]
    job = job[0]
    assert job.profile_id == ids["profile"] and job.batch_id == ids["batch"] and job.challenge_kind == "checkpoint"
    # Only hashes in the database.
    link = notifier.links(OPERATOR)[0]
    assert await pool.fetchval("select count(*) from verification_access_tokens where token_sha256 = $1", token_of(link)) == 0

    session = (await service.open(token_of(link), LOGIN)).session
    await service.claim(session)
    await service.view(session)
    assert await service.solve(session) is True
    assert await service.resume(session) == ids["batch"]

    assert await states(pool, ids) == {
        "profile": "ready", "batch": "queued", "batch_run": "stopped", "run": "stopped", "source": "active", "items": "queued,queued",
    }
    assert await pool.fetchval("select state from verification_jobs where id=$1::uuid", job.id) == "verified"
    events = [r[0] for r in await pool.fetch("select event from verification_events where verification_job_id=$1::uuid order by id", job.id)]
    assert events == ["detected", "token_issued", "notified", "token_issued", "notified", "opened", "claim", "view", "solve", "recovery_confirmed", "resume"]
    actors = {r[0] for r in await pool.fetch("select actor from orchestration_audit_log where entity_type='acquisition_batches'")}
    assert f"telegram:{OPERATOR}/tailscale:{LOGIN}" in actors

    # The collector can pick the batch up again from the challenged group.
    plan = await PostgresCollectorStore(pool).load_plan(ids["batch"])
    assert [item.sequence_no for item in plan.items] == [1, 2]


@pytest.mark.asyncio
async def test_single_use_tokens_and_append_only_events(db) -> None:
    pool, store = db
    await challenged(pool)
    service, notifier = service_for(store)
    await service.tick()
    token = token_of(notifier.links(OPERATOR)[0])
    await service.open(token, LOGIN)
    with pytest.raises(PermissionError):
        await service.open(token, LOGIN)
    import asyncpg

    with pytest.raises(asyncpg.PostgresError, match="append-only"):
        await pool.execute("delete from verification_events")


@pytest.mark.asyncio
async def test_fail_and_cancel_use_legal_transitions(db) -> None:
    pool, store = db
    ids = await challenged(pool)
    service, notifier = service_for(store)
    await service.tick()
    session = (await service.open(token_of(notifier.links(OPERATOR)[0]), LOGIN)).session
    await service.fail(session)  # requested -> active -> rejected
    assert await states(pool, ids) == {
        "profile": "quarantined", "batch": "failed", "batch_run": "stopped", "run": "stopped", "source": "human_verification_required", "items": "failed,queued",
    }
    with pytest.raises(ActionRefused):
        await service.cancel(session)


@pytest.mark.asyncio
async def test_cancel_and_expiry(db) -> None:
    pool, store = db
    ids = await challenged(pool)
    service, notifier = service_for(store)
    await service.tick()
    session = (await service.open(token_of(notifier.links(OPERATOR)[0]), LOGIN)).session
    await service.claim(session)
    await service.cancel(session)
    state = await states(pool, ids)
    assert state["batch"] == "cancelled" and state["run"] == "cancelled" and state["profile"] == "human_verification_required"
    assert await pool.fetchval("select count(*) from verification_access_tokens where revoked_at is null and used_at is null") == 0

    # A fresh challenge on the second group, left to expire.
    job2 = await pool.fetchval(
        "insert into verification_jobs(source_id, job_type, requested_by, resolution_note) select source_id, 'facebook_challenge', 't', 'facebook_url:/login' from acquisition_batch_items where sequence_no = 2 returning id::text")
    await service.tick()
    await pool.execute("update verification_jobs set expires_at = now() - interval '1 second' where id=$1::uuid", job2)
    await service.tick()
    assert await pool.fetchval("select state from verification_jobs where id=$1::uuid", job2) == "expired"
    assert await pool.fetchval("select count(*) from verification_events where verification_job_id=$1::uuid and event='expire'", job2) == 1


@pytest.mark.asyncio
async def test_a_sensitive_challenge_quarantines_and_tells_the_owner(db) -> None:
    pool, store = db
    ids = await challenged(pool, reason="facebook_page:account disabled")
    service, notifier = service_for(store)
    await service.tick()
    assert (await states(pool, ids))["profile"] == "quarantined"
    assert not notifier.links(OPERATOR) and notifier.sent[0][0] == OWNER
    assert await pool.fetchval("select sensitive from verification_jobs") is True
