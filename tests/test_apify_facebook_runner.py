"""T6: which provider runs a Facebook batch, and the budget gate (FEATURE_APIFY_FACEBOOK)."""

from __future__ import annotations

import os

import httpx
import pytest

from bot.acquisition.apify_client import ApifyHttp
from bot.facebook_collector.collector import FacebookBatchCollector
from bot.facebook_collector.main import build_provider, choose_provider, run_batch_with
from bot.utils import costs
from tests.test_apify_facebook import TOKEN, Api, item
from tests.test_apify_facebook_settings import settings

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"),
                              reason="set SYSTEM_TEST_DATABASE_URL to a *_test database")
ON = {"FEATURE_APIFY_FACEBOOK": "true", "APIFY_TOKEN": TOKEN, "APIFY_FACEBOOK_ACTOR_BUILD": "1.2.3"}


def test_choose_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    off = settings(monkeypatch)
    assert [choose_provider(off, p) for p in ("apify", "local", None)] == ["local"] * 3
    on = settings(monkeypatch, **ON)
    assert [choose_provider(on, p) for p in ("apify", "local", None)] == ["apify", "local", "local"]
    everyone = settings(monkeypatch, APIFY_FACEBOOK_SCOPE="all", **ON)
    assert choose_provider(everyone, None) == "apify"
    assert choose_provider(everyone, "fallback") == "local" and choose_provider(on, "fallback") == "local"


async def test_runner_uses_local_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoDatabase:
        async def fetchval(self, *a, **k):
            raise AssertionError("with the flag off nothing is looked up")

    s = settings(monkeypatch)
    assert isinstance(build_provider(NoDatabase(), s), FacebookBatchCollector)  # type: ignore[arg-type]
    ran: list[str] = []

    async def fake_run(self, batch_id: str) -> str:
        ran.append(batch_id)
        return "succeeded"

    monkeypatch.setattr(FacebookBatchCollector, "run", fake_run)
    assert await run_batch_with(NoDatabase(), s, "batch-1") == "succeeded" and ran == ["batch-1"]  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="off"):
        build_provider(NoDatabase(), s, "apify")  # type: ignore[arg-type]


@pytest.fixture
async def pool():
    import asyncpg

    from tests.test_apify_facebook_postgres import MIGRATIONS

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


def _http(api: Api) -> ApifyHttp:
    async def no_sleep(_: float) -> None:
        return None

    return ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(api)), sleep=no_sleep, jitter=lambda: 0)


@needs_db
async def test_a_campaign_set_to_apify_is_read_through_apify(pool, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_apify_facebook_postgres import _batch

    cid, batch = await _batch(pool)
    s = settings(monkeypatch, **ON)
    api = Api(dataset=[item(1)])
    ran: list[str] = []

    async def local_run(self, batch_id: str) -> str:
        ran.append(batch_id)
        return "succeeded"

    monkeypatch.setattr(FacebookBatchCollector, "run", local_run)
    assert await run_batch_with(pool, s, batch, apify_http=_http(api)) == "succeeded"
    assert ran == [batch] and api.launches() == 0  # the campaign is still «local»
    second = str(await pool.fetchval(
        """insert into acquisition_batches (platform, acquisition_method, vertical, state, max_items, campaign_id)
           values ('facebook', 'facebook_connector', 'real_estate', 'queued', 20, $1::uuid) returning id""", cid))
    await pool.execute("""insert into acquisition_batch_items (batch_id, source_id, sequence_no)
                          select $1::uuid, id, row_number() over (order by created_at) from monitoring_sources""", second)
    await pool.execute("update campaigns set acquisition_provider = 'apify'")
    assert await run_batch_with(pool, s, second, apify_http=_http(api)) == "succeeded"
    assert api.launches() == 1 and await pool.fetchval("select count(*) from collected_posts") == 1


@needs_db
async def test_runner_pauses_on_budget_exceeded(pool, monkeypatch: pytest.MonkeyPatch) -> None:
    """A campaign whose CAMPAIGN_BUDGET_USD is spent launches no paid run: its batch ends ``budget_cap`` and the
    campaign goes on with its other sources (the web stage stops the same way)."""
    from bot.facebook_collector.main import install_ledger
    from tests.test_apify_facebook_postgres import _batch

    cid, batch = await _batch(pool)
    await pool.execute("update campaigns set acquisition_provider = 'apify'")
    s = settings(monkeypatch, CAMPAIGN_BUDGET_USD="1", **ON)
    install_ledger(pool, s)
    await costs.record("llm", cost_usd=1.5, campaign_id=cid)
    api = Api(dataset=[item(1)])
    assert await run_batch_with(pool, s, batch, apify_http=_http(api)) == "failed"
    assert api.launches() == 0
    assert await pool.fetchval("select stop_reason from batch_runs where batch_id = $1::uuid", batch) == "budget_cap"
    assert await pool.fetchval("select state from acquisition_batches where id = $1::uuid", batch) == "failed"
    assert await pool.fetchval("select state from campaigns where id = $1::uuid", cid) == "planned"  # not paused
    summary = await costs.ledger().summary(cid)
    assert summary.skips == {"api:budget_cap": 1}


@needs_db
async def test_closed_groups_get_one_browser_batch_that_never_goes_back_to_apify(pool, monkeypatch) -> None:
    from bot.facebook_collector.main import campaign_provider
    from tests.test_apify_facebook import G2
    from tests.test_apify_facebook_postgres import _batch

    cid, batch = await _batch(pool)
    await pool.execute("update campaigns set acquisition_provider = 'apify'")
    s = settings(monkeypatch, APIFY_FACEBOOK_SCOPE="all", **ON)
    api = Api(dataset=[item(1), {"facebookUrl": G2, "error": "This group is private"}])
    assert await run_batch_with(pool, s, batch, apify_http=_http(api)) == "succeeded"
    fallback = await pool.fetchrow(
        """select id::text, state, requested_by, campaign_id::text, max_items from acquisition_batches
            where requested_by like 'apify_fallback:%'""")
    assert fallback["requested_by"] == f"apify_fallback:{batch}" and fallback["state"] == "queued"
    assert fallback["campaign_id"] == cid and fallback["max_items"] == 1
    urls = await pool.fetch("""select s.canonical_url from acquisition_batch_items i join monitoring_sources s
                                on s.id = i.source_id where i.batch_id = $1::uuid""", fallback["id"])
    assert [r["canonical_url"] for r in urls] == [G2]
    assert await pool.fetchval("select state from collector_launch_requests where batch_id = $1::uuid",
                               fallback["id"]) == "pending"  # facebook-runner starts it
    assert await campaign_provider(pool, fallback["id"]) == "fallback"
    assert choose_provider(s, await campaign_provider(pool, fallback["id"])) == "local"  # even with scope=all
    from bot.facebook_collector.apify.store import PostgresApifyFacebookStore
    from bot.facebook_collector.models import BatchItem, BatchPlan

    again = await PostgresApifyFacebookStore(pool).queue_browser_fallback(
        BatchPlan(batch, "", "", "", 20, ()), [BatchItem("x", str(await pool.fetchval(
            "select id from monitoring_sources where canonical_url = $1", G2)), G2, 1)], cid)
    assert again is None  # once per Apify batch
