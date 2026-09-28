"""PostgreSQL: the Facebook read quota goes to live groups; groups read lately with nothing found are skipped."""

from __future__ import annotations

import json
import uuid

from bot.campaign import plan_campaign
from bot.campaign.runs import PostgresRunStore
from bot.campaign.store import PostgresCampaignStore
from bot.orchestra.store import SafetyLimits
from tests.test_near_match import GOAL, USER, listing, needs_db
from tests.test_near_match import pool as pool


async def _group(conn, cid: str, key: str, score: float) -> None:
    await conn.execute(
        """insert into campaign_groups (campaign_id, group_key, canonical_url, name, relevance_score, state, window_no)
           values ($1::uuid, $2, $3, $4, $5, 'queued', 1)""",
        cid, key, f"https://www.facebook.com/groups/{key}/", key.title(), score)


async def _read(conn, key: str, *, days_ago: int, finding: bool) -> None:
    """A past read of the group by another campaign: a source, a succeeded run, and optionally a finding."""
    url = f"https://www.facebook.com/groups/{key}/"
    source = await conn.fetchval(
        """insert into monitoring_sources (platform, source_kind, vertical, canonical_url, acquisition_method, state)
           values ('facebook', 'group', 'real_estate', $1, 'facebook_connector', 'active') returning id""", url)
    run = await conn.fetchval(
        """insert into acquisition_runs (source_id, state, acquisition_method, finished_at)
           values ($1, 'succeeded', 'facebook_connector', now() - make_interval(days => $2)) returning id""",
        source, days_ago)
    if finding:
        post = await conn.fetchval(
            """insert into collected_posts (acquisition_run_id, source_id, platform_post_id, canonical_url, body_text,
                                            state, content_hash)
               values ($1, $2, '1', $3, 'Vendo piso', 'analysed', $4) returning id""",
            run, source, url + "posts/1/", uuid.uuid4().hex)
        await conn.execute(
            """insert into findings (post_id, source_id, vertical, finding_type, state, structured_payload, confidence,
                                     dedupe_key)
               values ($1, $2, 'real_estate', 'real_estate_proposition', 'ready', $3::jsonb, 0.9, $4)""",
            post, source, json.dumps(listing(45_000)), uuid.uuid4().hex)


@needs_db
async def test_live_groups_first_empty_recent_reads_skipped(pool) -> None:
    campaigns = PostgresCampaignStore(pool)
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=1, requested_by=USER, source_text=GOAL, actor="t")
    async with pool.acquire() as conn:
        for key, score in (("alpha", 0.9), ("empty", 0.8), ("live", 0.1), ("old", 0.5), ("fresh", 0.7)):
            await _group(conn, cid, key, score)
        await _read(conn, "empty", days_ago=1, finding=False)   # read yesterday, nothing: skipped
        await _read(conn, "live", days_ago=1, finding=True)     # gave a finding: read first
        await _read(conn, "old", days_ago=10, finding=False)    # read long ago: may be read again
    store = PostgresRunStore(pool, SafetyLimits())
    groups = await store.next_groups(cid, 20)
    assert [g.group_key for g in groups] == ["live", "alpha", "fresh", "old"]
    rows = dict(await pool.fetch("select group_key, state || ':' || coalesce(reject_reason, '') from campaign_groups"))
    assert rows["empty"] == "skipped:no findings in recent reads"
    assert rows["alpha"] == "queued:"
