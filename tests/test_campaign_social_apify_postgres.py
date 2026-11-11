"""P1.8: the Apify social posts' store on a real database (``collected_posts``, ``campaign_social_posts``, recovery).

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


@pytest.fixture
async def pool():
    import asyncpg

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    p = await asyncpg.create_pool(URL, min_size=1, max_size=4)
    try:
        yield p
    finally:
        await p.close()


async def test_pg_save_posts_and_recover(pool) -> None:
    from bot.acquisition.apify_instagram import instagram_post
    from bot.campaign import PostgresCampaignStore, plan_campaign
    from bot.campaign.social_apify import PostgresApifySocialStore
    from bot.social_search.queries import SocialQuery
    from tests.test_apify_social import ig_post_row

    campaigns = PostgresCampaignStore(pool)
    cid = await campaigns.create(plan_campaign("участок в Марбелье"), chat_id=1, requested_by=1, source_text="x",
                                 actor="t")
    cid2 = await campaigns.create(plan_campaign("участок в Марбелье"), chat_id=1, requested_by=2, source_text="x",
                                  actor="t")
    store = PostgresApifySocialStore(pool)
    url = "https://www.instagram.com/p/C1aBcDeFg01/"
    post = instagram_post(ig_post_row(url=url), campaign_id=cid, actor_id="apify/instagram-scraper", run_id="RUN1")
    assert await store.save_posts(cid, "real_estate", [post]) == 1
    assert await store.save_posts(cid, "real_estate", [post]) == 0  # the same page twice
    assert await store.save_posts(cid2, "real_estate", [post]) == 0  # another campaign, the same URL
    row = await pool.fetchrow(
        """select p.state, p.raw_payload->>'title' as title, s.source_kind, s.acquisition_method, s.platform
             from collected_posts p join monitoring_sources s on s.id = p.source_id""")
    assert row["state"] == "normalised" and row["source_kind"] == "search" and row["platform"] == "instagram"
    assert row["acquisition_method"] == "social_search" and row["title"].startswith("Instagram")
    links = await pool.fetch("select campaign_id::text, platform from campaign_social_posts")
    assert [(r["campaign_id"], r["platform"]) for r in links] == [(cid, "instagram")]
    # another URL form of the same shortcode: the (source, platform_post_id) unique catches it
    again = instagram_post(ig_post_row(url="https://instagram.com/p/C1aBcDeFg01"), campaign_id=cid,
                           actor_id="apify/instagram-scraper", run_id="RUN2")
    assert await store.save_posts(cid, "real_estate", [again]) == 0

    await store.add_queries(cid, "instagram", 1,
                            [SocialQuery("instagram", "hashtag", "terrenomarbella", "terrenomarbella", "es")])
    query = await store.next_query(cid, "instagram")
    assert query is not None
    await store.start_query(query.id, None)
    await store.set_campaign_social(cid, "instagram", "running", current_query=query.text)
    assert await store.recover_apify() == 1
    state = await pool.fetchrow("select state, started_at from social_search_queries")
    assert state["state"] == "planned" and state["started_at"] is None
    social = await pool.fetchrow("select state, current_query from campaign_social_state")
    assert social["state"] == "pending" and social["current_query"] is None
