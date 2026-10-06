"""Migration 022 and social search on a real PostgreSQL: LinkedIn, dedup, linkage to the campaign stream.

Skipped unless SYSTEM_TEST_DATABASE_URL names a disposable ``*_test`` database.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from bot.campaign import PostgresCampaignStore, plan_campaign
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.social_search.adapters import Block, SocialItem
from bot.social_search.queries import QueryPlanner, normalise_query
from bot.social_search.store import PostgresSocialStore, source_url
from bot.social_search.worker import SocialConfig, SocialSearchWorker
from tests.test_near_match import listing
from tests.test_social_search import RESULTS, FakeBrowser, FakeGenerator, q

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"),
                                reason="set SYSTEM_TEST_DATABASE_URL to a *_test database")
MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))
GOAL = "Купить квартиру в Мадриде до 50 000 €"


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


def item(key: str = "video:7301234567890123456", text: str = "Vendo parcela 1.200 m² en Boadilla, 45.000 €") -> SocialItem:
    video = key.split(":", 1)[1]
    return SocialItem("tiktok", "post", key, f"https://www.tiktok.com/@parcelas.madrid/video/{video}", text, "@parcelas.madrid")


async def running_campaign(pool, text: str = GOAL) -> str:
    campaigns = PostgresCampaignStore(pool)
    cid = await campaigns.create(plan_campaign(text), chat_id=-1, requested_by=7, source_text=text, actor="t")
    await campaigns.set_state(cid, "running", "t")
    return cid


async def test_migration_022_adds_linkedin_the_search_source_and_its_tables(pool) -> None:
    import asyncpg

    await pool.execute("insert into browser_profiles (profile_name, platform, storage_locator) values ('linkedin-main', 'linkedin', 'volume:x')")
    await pool.execute(
        """insert into monitoring_sources (platform, source_kind, vertical, canonical_url, acquisition_method, state)
           values ('linkedin', 'search', 'investors', 'https://www.linkedin.com/search#x', 'social_search', 'active')""")
    await pool.execute(
        "insert into acquisition_batches (platform, acquisition_method, vertical) values ('linkedin', 'agent_ridge', 'investors')")
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("insert into browser_profiles (profile_name, platform, storage_locator) values ('m', 'myspace', 'v')")
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            """insert into monitoring_sources (platform, source_kind, vertical, canonical_url, acquisition_method)
               values ('tiktok', 'search', 'both', 'https://www.tiktok.com/x', 'facebook_connector')""")
    tables = {r["table_name"] for r in await pool.fetch(
        "select table_name from information_schema.tables where table_schema = 'public' and table_name like '%social%'")}
    assert tables == {"social_search_queries", "social_seen_items", "campaign_social_posts", "campaign_social_state",
                      "social_platform_state"}


async def test_a_post_is_collected_once_globally_and_facebook_urls_are_skipped(pool) -> None:
    store = PostgresSocialStore(pool)
    first, second = await running_campaign(pool), await running_campaign(pool)
    assert await store.add_queries(first, "tiktok", 1, [q("tiktok", "hashtag", "terrenomadrid")]) == 1
    assert await store.add_queries(first, "tiktok", 1, [q("tiktok", "hashtag", "#TerrenoMadrid")]) == 0  # the same query
    await store.add_queries(second, "tiktok", 1, [q("tiktok", "hashtag", "parcelaenventa")])
    q1 = (await store.next_query(first, "tiktok")).id  # type: ignore[union-attr]
    q2 = (await store.next_query(second, "tiktok")).id  # type: ignore[union-attr]

    assert await store.save_item(first, "real_estate", q1, item()) is True
    assert await store.save_item(first, "real_estate", q1, item()) is False  # the same post twice
    assert await store.save_item(second, "real_estate", q2, item()) is False  # seen in another campaign
    assert await pool.fetchval("select count(*) from collected_posts") == 1
    assert await pool.fetchval("select seen_count from social_seen_items") == 3
    row = await pool.fetchrow(
        """select p.state, p.author_handle, p.raw_payload->>'title' as title, s.platform, s.source_kind,
                  s.acquisition_method, s.vertical, s.canonical_url
             from collected_posts p join monitoring_sources s on s.id = p.source_id""")
    assert dict(row) == {"state": "normalised", "author_handle": "@parcelas.madrid", "title": "TikTok · @parcelas.madrid",
                         "platform": "tiktok", "source_kind": "search", "acquisition_method": "social_search",
                         "vertical": "real_estate", "canonical_url": source_url("tiktok", "real_estate")}
    assert [str(r["campaign_id"]) for r in await pool.fetch("select campaign_id from campaign_social_posts")] == [first]

    # A URL another source already collected (e.g. a Facebook share) is not filed again.
    shared = item("video:7301234567890129999")
    await pool.execute(
        """insert into collected_posts (source_id, platform_post_id, canonical_url, body_text, content_hash, state)
           select id, 'fb1', $1, 'x', 'h', 'normalised' from monitoring_sources limit 1""", shared.url)
    assert await store.seen("tiktok", [item(), shared, item("video:7301234567890120000")]) == {item().key, shared.key}
    assert await store.save_item(first, "real_estate", q1, shared) is False
    assert await pool.fetchval("select count(*) from campaign_social_posts") == 1


async def test_queries_caps_pacing_profiles_challenge_and_recovery(pool) -> None:
    store = PostgresSocialStore(pool)
    cid, other = await running_campaign(pool), await running_campaign(pool)
    await store.add_queries(cid, "linkedin", 1, [q("linkedin", "people", "business angel Madrid"),
                                                  q("linkedin", "posts", "inversores Madrid")])
    await store.add_queries(other, "linkedin", 1, [q("linkedin", "companies", "fondo inmobiliario Madrid")])
    profile_id = str(await pool.fetchval(
        "insert into browser_profiles (profile_name, platform, storage_locator, state) values ('linkedin-main', 'linkedin', 'v', 'ready') returning id"))
    profile = await store.ready_profile("linkedin")
    assert profile is not None and profile.id == profile_id and await store.claim_profile(profile_id)
    assert not await store.claim_profile(profile_id) and await store.ready_profile("linkedin") is None
    planned = await store.next_query(cid, "linkedin")
    assert planned is not None and (planned.kind, planned.text) == ("people", "business angel Madrid")
    await store.start_query(planned.id, profile_id)
    assert await store.queries_today("linkedin") == 1
    # The other campaign's started query counts as used for everyone; unstarted ones only for their campaign.
    other_q = await store.next_query(other, "linkedin")
    await store.start_query(other_q.id, profile_id)  # type: ignore[union-attr]
    used = await store.used_queries(cid, "linkedin", 7)
    assert {text for _, _, text in used} == {"business angel Madrid", "inversores Madrid", "fondo inmobiliario Madrid"}
    assert {text for _, _, text in await store.used_queries(cid, "linkedin", 0)} == {"business angel Madrid", "inversores Madrid"}

    await store.finish_query(other_q.id, "planned")  # type: ignore[union-attr]  # never reached the network
    assert await store.queries_today("linkedin") == 1

    # A checkpoint: the profile needs a human and the verification flow gets a login job.
    await store.challenge("linkedin", profile_id, planned.id, Block("checkpoint", "linkedin_url:/checkpoint"), "investors")
    assert await pool.fetchval("select state from browser_profiles where id = $1::uuid", profile_id) == "human_verification_required"
    job = await pool.fetchrow("select job_type, state, challenge_kind, browser_profile_id::text as p from verification_jobs")
    assert dict(job) == {"job_type": "login", "state": "requested", "challenge_kind": "checkpoint", "p": profile_id}
    assert await pool.fetchval("select state from social_search_queries where id = $1::uuid", planned.id) == "failed"
    # The live view's «Готово» (complete_verification) brings it back.
    from bot.control_plane.models import LiveProfile
    from bot.control_plane.store import PostgresControlPlaneStore, PostgresLiveViewStore

    control = PostgresControlPlaneStore(URL)
    await control.connect()
    live = PostgresLiveViewStore(control)
    try:
        assert await live.platform_states() == {"linkedin": "human_verification_required"}
        await live.complete_verification(LiveProfile(profile_id, "linkedin-main", "linkedin", "human_verification_required"), "telegram:7")
        assert await live.platform_states() == {"linkedin": "ready"}
    finally:
        await control.close()
    assert await pool.fetchval("select state from verification_jobs") == "verified"

    # A crash while a query ran: the query failed, the profile is ready again.
    assert await store.claim_profile(profile_id)
    running = await store.next_query(cid, "linkedin")
    await store.start_query(running.id, profile_id)  # type: ignore[union-attr]
    await store.set_campaign_social(cid, "linkedin", "running", current_query="inversores Madrid")
    assert await store.recover() == 1
    assert await pool.fetchval("select state from browser_profiles where id = $1::uuid", profile_id) == "ready"
    assert (await store.campaign_social(cid, "linkedin")).state == "pending"

    await store.set_pacing("linkedin", paused_until=None, next_action_at=None, reason=None)
    assert (await store.pacing("linkedin")).paused_until is None


async def test_social_findings_stream_to_the_campaign_like_facebook_posts(pool) -> None:
    from bot.analysis_pipeline.store import PostgresAnalysisStore
    from bot.campaign.runs import PostgresRunStore
    from bot.orchestra.store import SafetyLimits
    from tests.test_near_match import ButtonMessenger, cards

    campaigns = PostgresCampaignStore(pool)
    cid = await running_campaign(pool)
    await pool.execute(
        "insert into browser_profiles (profile_name, platform, storage_locator, state) values ('tiktok-main', 'tiktok', 'v', 'ready')")
    social = PostgresSocialStore(pool)
    worker = SocialSearchWorker(social, campaigns, FakeBrowser(RESULTS),
                                QueryPlanner(FakeGenerator([q("tiktok", "keyword", "piso en venta Madrid")])),
                                SocialConfig(platforms=("tiktok",)))
    assert await worker.tick() == 1
    assert await pool.fetchval("select count(*) from campaign_social_posts") == 2
    runs = PostgresRunStore(pool, SafetyLimits())
    assert await runs.pending_analysis(cid) == 2
    activity = await runs.social_activity(cid)
    assert activity.pending and activity.searching is None and not activity.notes

    # The ordinary analysis worker claims the posts (their search source is active) ...
    analysis = PostgresAnalysisStore(URL)
    await analysis.connect()
    try:
        claimed = await analysis.pending(10)
        assert {r["canonical_url"] for r in claimed} == {
            "https://www.tiktok.com/@parcelas.madrid/video/7301234567890123456",
            "https://www.tiktok.com/@casas_es/photo/7301234567890123999"}
        assert {r["title"] for r in claimed} == {"TikTok · @parcelas.madrid", "TikTok · @casas_es"}
        post = claimed[0]
        finding = await pool.fetchval(
            """insert into findings (post_id, source_id, vertical, finding_type, state, structured_payload, confidence, dedupe_key)
               values ($1, $2, 'real_estate', 'real_estate_proposition', 'ready', $3::jsonb, 0.9, $4) returning id""",
            post["id"], post["source_id"], json.dumps(listing(45_000)), uuid.uuid4().hex)
        # ... and the digest leaves campaign findings to the campaign.
        assert await analysis.campaign_finding_ids([str(finding)]) == {str(finding)}
    finally:
        await analysis.close()

    # The campaign streams it as a Russian card, exactly once.
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, runs, messenger, None, owner_ids={7},
                            config=RunnerConfig(relevance_fail_closed=False))
    await runner.step(cid)
    await runner.step(cid)
    assert len(cards(messenger)) == 1 and "45 000" in cards(messenger)[0]
    assert await pool.fetchval("select state from campaign_findings where finding_id = $1", finding) == "sent"
    assert await pool.fetchval("select count(*) from social_search_queries where state = 'done'") == 1
    assert normalise_query("tiktok", "keyword", "piso en venta Madrid") is not None
