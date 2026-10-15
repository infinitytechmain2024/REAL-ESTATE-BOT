"""Campaign store and migration 014 on a real PostgreSQL.

Skipped unless SYSTEM_TEST_DATABASE_URL names a disposable ``*_test`` database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bot.campaign import PostgresCampaignStore, plan_campaign

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"), reason="set SYSTEM_TEST_DATABASE_URL to a *_test database")
MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))
TEXT = "Найди квартиры в аренду в Мадриде до 1200 евро"


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
        await pool.close()


async def test_create_get_transitions_and_audit(pool) -> None:
    import asyncpg

    store = PostgresCampaignStore(pool)
    plan = plan_campaign(TEXT)
    cid = await store.create(plan, chat_id=-100, requested_by=42, source_text=TEXT, actor="telegram:42")
    campaign = await store.get(cid)
    assert campaign is not None
    assert (campaign.state, campaign.chat_id, campaign.requested_by, campaign.source_text) == ("planned", -100, 42, TEXT)
    assert campaign.plan == plan and campaign.status_message_id is None and campaign.finished_at is None
    assert await store.get("not-a-uuid") is None
    assert await store.get("00000000-0000-0000-0000-000000000000") is None

    assert not await store.set_state(cid, "completed", "campaign:test")  # planned -> completed is illegal
    assert await store.set_state(cid, "discovering", "campaign:test")
    assert await store.set_state(cid, "paused_verification", "campaign:test", reason="checkpoint")
    assert await store.set_state(cid, "running", "campaign:test")
    assert await store.set_state(cid, "completed", "campaign:test", reason="done")
    assert not await store.set_state(cid, "running", "campaign:test")  # terminal
    with pytest.raises(ValueError):
        await store.set_state(cid, "bogus", "campaign:test")

    done = await store.get(cid)
    assert done and done.state == "completed" and done.stop_reason == "done" and done.finished_at is not None

    assert await store.set_status_message(cid, 555, actor="telegram:bot")
    assert (await store.get(cid)).status_message_id == 555

    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("update campaigns set state='running' where id=$1::uuid", cid)

    rows = await pool.fetch(
        "select actor, action, old_state, new_state from orchestration_audit_log "
        "where entity_type='campaigns' and entity_id=$1::uuid order by id", cid)
    assert [(r["actor"], r["action"], r["old_state"], r["new_state"]) for r in rows] == [
        ("telegram:42", "insert", None, "planned"),
        ("campaign:test", "state_transition", "planned", "discovering"),
        ("campaign:test", "state_transition", "discovering", "paused_verification"),
        ("campaign:test", "state_transition", "paused_verification", "running"),
        ("campaign:test", "state_transition", "running", "completed"),
        ("telegram:bot", "update", "completed", "completed"),
    ]


async def test_db_guard_and_checks(pool) -> None:
    import asyncpg

    store = PostgresCampaignStore(pool)
    cid = await store.create(plan_campaign(TEXT), chat_id=1, requested_by=2, source_text=TEXT, actor="t")
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("update campaigns set state='paused_verification' where id=$1::uuid", cid)
    await pool.execute("update campaigns set state='cancelled' where id=$1::uuid", cid)
    assert await pool.fetchval("select finished_at is not null from campaigns where id=$1::uuid", cid)
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("update campaigns set state='discovering' where id=$1::uuid", cid)
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("insert into campaigns(telegram_chat_id, requested_by, source_text, plan) values (1, 2, $1, '{}')", "x" * 2001)
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("insert into campaigns(telegram_chat_id, requested_by, source_text, plan) values (1, 2, 'x', '[]')")
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("insert into campaigns(telegram_chat_id, requested_by, source_text, plan, state) values (1, 2, 'x', '{}', 'bogus')")
    with pytest.raises(ValueError):
        await store.create(plan_campaign(TEXT), chat_id=1, requested_by=2, source_text="", actor="t")


async def test_migration_047_group_join_flow(pool) -> None:
    """047: closed (group_private) groups are found once, confirmed once, and queued again for the browser."""
    import uuid

    from bot.campaign.runs import PostgresRunStore
    from bot.facebook_collector.apify.store import PostgresApifyFacebookStore
    from bot.facebook_collector.models import BatchItem, GroupState
    from bot.orchestra.store import SafetyLimits

    cols = {r["column_name"] for r in await pool.fetch(
        "select column_name from information_schema.columns where table_name = 'campaign_groups'")}
    assert {"join_notice_at", "join_confirmed_at"} <= cols
    cid = await PostgresCampaignStore(pool).create(plan_campaign(TEXT), chat_id=1, requested_by=2, source_text=TEXT, actor="t")
    url = "https://www.facebook.com/groups/closed/"
    source = await pool.fetchval(
        """insert into monitoring_sources (platform, source_kind, vertical, canonical_url, display_name, acquisition_method,
                                           state) values ('facebook', 'group', 'real_estate', $1, $1, 'facebook_connector',
                                           'active') returning id""", url)
    batch = await pool.fetchval(
        """insert into acquisition_batches (platform, acquisition_method, vertical, state, max_items, campaign_id)
           values ('facebook', 'facebook_connector', 'real_estate', 'queued', 20, $1::uuid) returning id""", cid)
    item = await pool.fetchval("insert into acquisition_batch_items (batch_id, source_id, sequence_no) values ($1, $2, 1) returning id",
                               batch, source)
    group = await pool.fetchval(
        """insert into campaign_groups (campaign_id, group_key, canonical_url, name, state, batch_id)
           values ($1::uuid, 'closed', $2, 'Closed', 'queued', $3) returning id::text""", cid, url, batch)
    store = PostgresRunStore(pool, SafetyLimits())
    assert await store.groups_needing_join(cid) == []  # nothing read yet
    await PostgresApifyFacebookStore(pool).finish_item(BatchItem(str(item), str(source), url, 1), str(uuid.uuid4()), "failed",
                                                       GroupState.INACCESSIBLE, "group_private")
    found = await store.groups_needing_join(cid)
    assert [(g.id, g.name, g.url) for g in found] == [(group, "Closed", url)]
    assert not await store.confirm_join(group)  # no notice yet: the button is not valid
    await store.mark_join_notice(group)
    assert await store.groups_needing_join(cid) == []  # one notice per group
    assert await store.confirm_join(group) and not await store.confirm_join(group)
    row = await pool.fetchrow("select state, activity, batch_id, join_confirmed_at from campaign_groups where id = $1::uuid", group)
    assert (row["state"], row["activity"], row["batch_id"]) == ("queued", "UNKNOWN", None) and row["join_confirmed_at"]
    assert await store.groups_needing_join(cid) == []
