"""Campaign discovery and migration 015 on a real PostgreSQL.

Skipped unless SYSTEM_TEST_DATABASE_URL names a disposable ``*_test`` database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bot.campaign import PostgresCampaignStore, plan_campaign
from bot.campaign.discovery import (
    CampaignGroup,
    DiscoveryRefused,
    FacebookDiscovery,
    PostgresDiscoveryStore,
    plan_seeds,
)
from bot.campaign.models import CampaignLimits
from bot.facebook_collector.models import GroupState
from tests.test_campaign_discovery import NOW, FakeBrowser, FakeReader, Sleeps, _read, link, url_of

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
    # 015 is idempotent: applying it again changes nothing.
    await conn.execute(MIGRATIONS[-1].read_text(encoding="utf-8"))
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


async def _profile(pool, name: str = "facebook-main", state: str = "ready") -> str:
    return await pool.fetchval(
        "insert into browser_profiles(profile_name, platform, state, storage_locator) values($1,'facebook',$2,'vol:x') returning id::text",
        name, state,
    )


async def _campaign(pool, max_groups: int = 40) -> tuple[PostgresCampaignStore, str]:
    campaigns = PostgresCampaignStore(pool)
    plan = plan_campaign(TEXT).model_copy(update={"limits": CampaignLimits(max_groups=max_groups, window_size=2)})
    return campaigns, await campaigns.create(plan, chat_id=-1, requested_by=2, source_text=TEXT, actor="telegram:2")


def _discovery(campaigns, pool, browser, reader) -> FacebookDiscovery:
    return FacebookDiscovery(campaigns, PostgresDiscoveryStore(pool), browser, reader, sleep=Sleeps(), now=lambda: NOW)


async def test_discovery_queues_audits_and_frees_the_profile(pool) -> None:
    profile_id = await _profile(pool)
    campaigns, cid = await _campaign(pool, max_groups=3)
    seeds = plan_seeds((await campaigns.get(cid)).plan, 12)
    browser = FakeBrowser({
        seeds[0][1]: [link("pisosmadrid", "Pisos alquiler Madrid", "10 posts a day"),
                      link("rentmadrid", "Madrid rent", "3 posts a week"),
                      link("123", "Pisos Barcelona", "10 posts a day")],
        seeds[1][1]: [link("pisosmadrid", "Pisos Madrid", "10 posts a day"),
                      link("privado", "Alquiler Madrid privado", ""),
                      link("flats", "Madrid flats for rent", "5 posts a day"),
                      link("more", "Madrid rooms for rent", "5 posts a day")],
    })
    reader = FakeReader({url_of("privado"): _read(GroupState.INACCESSIBLE)})

    report = await _discovery(campaigns, pool, browser, reader).run(cid)

    assert report.state == "running" and report.queued == 3
    rows = await pool.fetch(
        "select group_key, canonical_url, state, activity, reject_reason, window_no, relevance_score::float8 s, language, seed "
        "from campaign_groups where campaign_id=$1::uuid order by group_key", cid)
    by_key = {r["group_key"]: r for r in rows}
    assert set(by_key) == {"pisosmadrid", "rentmadrid", "123", "privado", "flats", "more"}
    assert by_key["pisosmadrid"]["canonical_url"] == "https://www.facebook.com/groups/pisosmadrid/"
    assert (by_key["123"]["state"], by_key["123"]["reject_reason"]) == ("rejected", "no_location")
    assert (by_key["privado"]["state"], by_key["privado"]["activity"]) == ("discovered", "INACCESSIBLE")
    queued = sorted((r for r in rows if r["state"] == "queued"), key=lambda r: (r["window_no"], -r["s"], r["group_key"]))
    assert [r["window_no"] for r in queued] == [1, 1, 2]
    assert sum(r["state"] == "skipped" for r in rows) == 1
    assert by_key["pisosmadrid"]["language"] == seeds[0][0] and by_key["pisosmadrid"]["seed"] == seeds[0][1]

    assert await pool.fetchval("select state from browser_profiles where id=$1::uuid", profile_id) == "ready"
    assert await pool.fetchval("select state from campaigns where id=$1::uuid", cid) == "running"
    actors = await pool.fetch(
        "select distinct actor from orchestration_audit_log where entity_type='campaign_groups'")
    assert [r["actor"] for r in actors] == ["campaign:discovery"]
    profile_actors = {r["actor"] for r in await pool.fetch(
        "select actor from orchestration_audit_log where entity_type='browser_profiles' and action='state_transition'")}
    assert profile_actors == {"campaign:discovery"}
    progress = await PostgresDiscoveryStore(pool).load_progress(cid)
    assert len(progress["seeds_done"]) == 2 and progress["visits"] == 1  # candidate cap (6) reached after two seeds


async def test_challenge_pauses_keeps_partial_results_and_resumes_without_duplicates(pool) -> None:
    profile_id = await _profile(pool)
    campaigns, cid = await _campaign(pool)
    seeds = plan_seeds((await campaigns.get(cid)).plan, 12)
    browser = FakeBrowser({seeds[0][1]: [link("pisosmadrid", "Pisos alquiler Madrid", "10 posts a day")],
                           seeds[1][1]: [link("pisosmadrid", "Pisos Madrid", "10 posts a day"),
                                         link("flats", "Madrid flats for rent", "5 posts a day")]})
    browser.challenge_on = seeds[1][1]
    discovery = _discovery(campaigns, pool, browser, FakeReader())

    report = await discovery.run(cid)

    assert report.state == "paused_verification" and browser.released == ["VERIFICATION_REQUIRED"]
    assert await pool.fetchval("select state from browser_profiles where id=$1::uuid", profile_id) == "human_verification_required"
    campaign = await campaigns.get(cid)
    assert campaign.state == "paused_verification" and campaign.stop_reason.startswith("facebook_challenge:")
    assert await pool.fetchval("select count(*) from campaign_groups where campaign_id=$1::uuid", cid) == 1
    with pytest.raises(DiscoveryRefused, match="facebook_profile_not_ready"):
        await discovery.run(cid)

    # The operator cleared the checkpoint in the live window.
    await pool.execute("update browser_profiles set state='ready' where id=$1::uuid", profile_id)
    browser.challenge_on = None
    report = await discovery.run(cid)
    assert report.state == "running"
    keys = [r["group_key"] for r in await pool.fetch(
        "select group_key from campaign_groups where campaign_id=$1::uuid order by group_key", cid)]
    assert keys == ["flats", "pisosmadrid"]
    assert browser.navigations.count(browser.navigations[0]) == 1  # the finished seed was not searched again


async def test_a_profile_held_by_the_collector_refuses_discovery(pool) -> None:
    await _profile(pool, "facebook-main", "in_use")
    await _profile(pool, "facebook-spare", "ready")
    campaigns, cid = await _campaign(pool)
    browser = FakeBrowser()
    with pytest.raises(DiscoveryRefused, match="facebook_profile_not_ready"):
        await _discovery(campaigns, pool, browser, FakeReader()).run(cid)
    assert browser.acquired == [] and (await campaigns.get(cid)).state == "planned"


async def test_upsert_never_rewrites_a_queued_group_and_constraints_hold(pool) -> None:
    import asyncpg

    _, cid = await _campaign(pool)
    store = PostgresDiscoveryStore(pool)
    group = CampaignGroup("pisosmadrid", "https://www.facebook.com/groups/pisosmadrid/", "Pisos", "es", "seed", 0.9, "ACTIVE")
    await store.save_group(cid, group, "campaign:discovery")
    await store.save_group(cid, group, "campaign:discovery")
    assert len(await store.groups(cid)) == 1
    assert await store.finalize(cid, max_groups=5, window_size=2, actor="campaign:discovery") == 1
    await store.save_group(cid, CampaignGroup(group.group_key, group.canonical_url, "x", "es", "seed", 0.1, "DEAD",
                                              state="rejected", reject_reason="dead"), "campaign:discovery")
    (stored,) = await store.groups(cid)
    assert (stored.state, stored.activity, stored.window_no) == ("queued", "ACTIVE", 1)
    # finalize again does not re-queue or re-window
    assert await store.finalize(cid, max_groups=5, window_size=2, actor="campaign:discovery") == 1

    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "insert into campaign_groups(campaign_id, group_key, canonical_url, activity) values($1::uuid,'g','https://www.facebook.com/groups/g/','GONE')", cid)
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute(
            "insert into campaign_groups(campaign_id, group_key, canonical_url) values($1::uuid,'g','https://evil.example/g/')", cid)
    await pool.execute("delete from campaigns where id=$1::uuid", cid)
    assert await pool.fetchval("select count(*) from campaign_groups") == 0
