"""PostgreSQL (migration 026): comment reads queued by sent objects, people stored silently, sent by an investor search."""

from __future__ import annotations

from bot.campaign import plan_campaign
from bot.campaign.leads import CommentConfig, CommentLeadWorker, PostgresLeadStore
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import PostgresRunStore
from bot.campaign.store import PostgresCampaignStore
from bot.orchestra.store import SafetyLimits, challenge_breaker
from tests.test_comment_leads import FakeBrowser, FakeJudge
from tests.test_near_match import GOAL, USER, ButtonMessenger, _seed_findings, needs_db
from tests.test_near_match import pool as pool

CHAT = 4242


async def _profile(pool, state: str = "ready") -> str:
    return str(await pool.fetchval(
        """insert into browser_profiles (profile_name, platform, state, storage_locator)
           values ('facebook-main', 'facebook', $1, 'profiles/facebook-main') returning id""", state))


def _page(url: str) -> dict:
    return {"url": url, "title": "Facebook", "text": "",
            "post_author_url": "https://www.facebook.com/groups/pisos/user/100000000000009/",
            "comments": [
                {"author": "Juan Pérez", "author_url": "https://www.facebook.com/groups/pisos/user/100012345678/",
                 "text": "Soy inversor, me interesa. +34 612 345 678"},
                {"author": "Vendedor", "author_url": "https://www.facebook.com/groups/pisos/user/100000000000009/",
                 "text": "Soy inversor también, escríbeme"},  # the post's author: never a lead
                {"author": "Ana", "author_url": "https://www.facebook.com/ana.garcia", "text": "@Maria mira"},
            ]}


@needs_db
async def test_objects_feed_the_store_and_only_an_investor_search_sends_the_people(pool) -> None:
    campaigns = PostgresCampaignStore(pool)
    store = PostgresRunStore(pool, SafetyLimits())
    await _profile(pool)

    # 1. A property search sends its objects; each Facebook post is queued for one comment read.
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, config=RunnerConfig(relevance_fail_closed=False, comment_leads="all"))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=CHAT, requested_by=USER, source_text=GOAL, actor="t")
    await campaigns.set_state(cid, "running", "campaign:test")
    ids = await _seed_findings(pool, cid, {"a": 45_000, "b": 48_000})
    await runner.step(cid)
    queued = dict(await pool.fetch("select finding_id::text, state from campaign_comment_reads"))
    assert queued == {ids["a"]: "queued", ids["b"]: "queued"}
    assert await store.queue_comment_read(cid, ids["a"], "https://www.facebook.com/groups/pisos/posts/0/", 15) is False
    assert (await campaigns.get(cid)).state == "completed", "the search never waits for the comments"

    # 2. The comment worker reads them later and stores the people silently.
    urls = {r["post_url"] for r in await pool.fetch("select post_url from campaign_comment_reads")}
    browser = FakeBrowser({url: _page(url) for url in urls})
    leads = PostgresLeadStore(pool, SafetyLimits())

    async def no_sleep(_: float) -> None:
        return None

    worker = CommentLeadWorker(leads, browser, FakeJudge(), config=CommentConfig(reads_per_round=5), sleep=no_sleep)
    assert await worker.step() == 2
    rows = await pool.fetch("select profile_key, role, location, object_facts->>'property_type' as kind, "
                            "judged_by from investor_leads order by post_url")
    assert [(r["profile_key"], r["role"], r["location"], r["kind"]) for r in rows] == [
        ("fb:100012345678", "investor", "Madrid", "apartment")] * 2
    assert await pool.fetchval("select state from browser_profiles") == "ready"
    assert {r["state"] for r in await pool.fetch("select state from campaign_comment_reads")} == {"done"}
    assert await leads.reads_today() == 2
    sent_before = len(messenger.sent)
    await runner.step(cid)
    assert len(messenger.sent) == sent_before, "a property search never sends people"

    # 3. An investor search in Madrid sends the person once, with both objects.
    investors = await campaigns.create(plan_campaign("Найди инвесторов в Мадриде"), chat_id=CHAT, requested_by=USER,
                                       source_text="инвесторы", actor="t")
    await campaigns.set_state(investors, "running", "campaign:test")
    await runner.step(investors)
    await runner.step(investors)
    cards = [t for _, _, t in messenger.sent if t.startswith("💼 Потенциальный инвестор")]
    assert len(cards) == 1
    assert "Профиль: https://www.facebook.com/profile.php?id=100012345678" in cards[0]
    assert "Контакт из комментариев: +34 612 345 678" in cards[0]
    assert "Интерес к 2 объектам (Мадрид): квартиры" in cards[0]
    assert await pool.fetchval("select state from campaign_lead_deliveries where campaign_id = $1::uuid",
                               investors) == "sent"

    # A search in another city sees nobody.
    other = await campaigns.create(plan_campaign("Найди инвесторов в Барселоне"), chat_id=CHAT, requested_by=USER,
                                   source_text="инвесторы", actor="t")
    assert await store.stored_people(other, "Barcelona", 90, 10) == []


@needs_db
async def test_the_reader_never_takes_facebook_from_a_window_or_a_discovery(pool) -> None:
    profile = await _profile(pool)
    leads = PostgresLeadStore(pool, SafetyLimits())
    await pool.execute(
        """insert into acquisition_batches (platform, acquisition_method, vertical, state, max_items)
           values ('facebook', 'facebook_connector', 'real_estate', 'queued', 1)""")
    assert await leads.claim_profile() is None
    await pool.execute("update acquisition_batches set state = 'cancelled'")
    campaigns = PostgresCampaignStore(pool)
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=CHAT, requested_by=USER, source_text=GOAL, actor="t")
    await campaigns.set_state(cid, "discovering", "t")
    assert await leads.claim_profile() is None, "a discovery holds Facebook"
    await campaigns.set_state(cid, "running", "t")
    claimed = await leads.claim_profile()
    assert claimed is not None and claimed.id == profile
    assert await pool.fetchval("select state from browser_profiles") == "in_use"
    await leads.release_profile(profile, "human_verification_required")
    assert await pool.fetchval("select state from browser_profiles") == "human_verification_required"


@needs_db
async def test_a_challenge_on_a_comment_read_counts_towards_the_breaker(pool) -> None:
    campaigns = PostgresCampaignStore(pool)
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=CHAT, requested_by=USER, source_text=GOAL, actor="t")
    ids = await _seed_findings(pool, cid, {"a": 45_000, "b": 46_000})
    for n, finding in enumerate(ids.values()):
        await pool.execute(
            """insert into campaign_comment_reads (campaign_id, finding_id, post_url, state, error_code, read_at)
               values ($1::uuid, $2::uuid, $3, 'failed', 'facebook_challenge:checkpoint', now())""",
            cid, finding, f"https://www.facebook.com/groups/pisos/posts/{n}/")
    async with pool.acquire() as conn:
        reason = await challenge_breaker(conn, SafetyLimits(breaker_challenges=2), "facebook")
    assert reason is not None and "2 facebook challenges" in reason
    assert await PostgresLeadStore(pool, SafetyLimits(breaker_challenges=2)).breaker_reason() == reason


@needs_db
async def test_a_crashed_read_goes_back_to_the_queue_and_a_stopped_campaign_is_skipped(pool) -> None:
    campaigns = PostgresCampaignStore(pool)
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=CHAT, requested_by=USER, source_text=GOAL, actor="t")
    ids = await _seed_findings(pool, cid, {"a": 45_000})
    await pool.execute(
        """insert into campaign_comment_reads (campaign_id, finding_id, post_url, state)
           values ($1::uuid, $2::uuid, 'https://www.facebook.com/groups/pisos/posts/0/', 'reading')""", cid, ids["a"])
    leads = PostgresLeadStore(pool, SafetyLimits())
    assert await leads.recover() == 1
    assert await leads.has_reads()
    await campaigns.set_state(cid, "cancelled", "t")
    assert not await leads.has_reads() and await leads.claim_reads(5) == []
