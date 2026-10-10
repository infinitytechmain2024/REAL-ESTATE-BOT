"""The run's cost ledger and budget (bot/utils/costs.py, migration 042)."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from bot.utils import costs

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))


@pytest.fixture
def ledger():
    sink = costs.MemoryLedger()
    costs.install(sink, budget_usd=1.0)
    try:
        yield sink
    finally:
        costs.install(None)


def test_llm_cost_is_openrouters_own_else_an_estimate() -> None:
    assert costs.llm_cost("anthropic/claude-sonnet-4.5", {"usage": {"cost": 0.0123, "prompt_tokens": 10}}) == (0.0123, 10)
    cost, tokens = costs.llm_cost("anthropic/claude-sonnet-4.5", {"usage": {"prompt_tokens": 4000, "completion_tokens": 600}})
    assert tokens == 4600 and cost == pytest.approx((4000 * 3 + 600 * 15) / 1e6)
    assert costs.llm_cost("openai/gpt-4o-mini", {"usage": {"prompt_tokens": 1_000_000}})[0] == pytest.approx(0.15)
    assert costs.llm_cost("some/unknown", None) == (0.0, 0)


async def test_nothing_is_booked_or_capped_without_a_ledger() -> None:
    costs.install(None)
    await costs.record("llm", cost_usd=99)
    assert not await costs.over_budget("c1")


async def test_costs_are_booked_on_the_scoped_campaign_and_capped(ledger) -> None:
    with costs.scope("c1"):
        await costs.llm("anthropic/claude-sonnet-4.5", {"usage": {"cost": 0.6}})
        await costs.record("scrape", provider="scrape_api", item="idealista.com", cost_usd=0.3)
        await costs.error("llm", "http_401", item="m")
        await costs.skip("llm", "prefilter_deal", item="pisos.com")
        assert not await costs.over_budget()
        await costs.record("scrape", item="idealista.com", cost_usd=0.2)
        assert await costs.over_budget()  # 1.1 of 1.0
    await costs.record("llm", cost_usd=5)  # outside any campaign: booked, never on c1
    assert not await costs.over_budget("c2")
    summary = await ledger.summary("c1")
    assert summary.by_stage == {"llm": 0.6, "scrape": pytest.approx(0.5)} and summary.total == pytest.approx(1.1)
    assert summary.errors == {"llm:http_401": 1} and summary.skips == {"llm:prefilter_deal": 1}
    assert summary.by_item["idealista.com"] == pytest.approx(0.5)
    with pytest.raises(ValueError):
        await costs.record("bogus")


async def test_a_broken_ledger_never_stops_the_work() -> None:
    class Broken(costs.MemoryLedger):
        async def add(self, entry):
            raise RuntimeError("db down")

        async def spent(self, campaign_id):
            raise RuntimeError("db down")

    costs.install(Broken(), budget_usd=1)
    try:
        with costs.scope("c1"):
            await costs.record("llm", cost_usd=1)
            assert not await costs.over_budget()
    finally:
        costs.install(None)


async def test_the_openrouter_client_books_usage_and_errors(ledger) -> None:
    from bot.agents.llm import LLMError, OpenRouterJSON

    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if len(sent) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {"cost": 0.02}})
        return httpx.Response(402, json={"error": "no credits"})

    client = OpenRouterJSON("k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with costs.scope("c1"):
        assert await client.complete("anthropic/claude-sonnet-4.5", "s", "u") == "{}"
        with pytest.raises(LLMError):
            await client.complete("anthropic/claude-sonnet-4.5", "s", "u")
    assert b'"usage": {"include": true}' in sent[0].content or b'"usage":{"include":true}' in sent[0].content
    summary = await ledger.summary("c1")
    assert summary.by_stage == {"llm": 0.02} and summary.errors == {"llm:http_402": 1}


@pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"),
                    reason="set SYSTEM_TEST_DATABASE_URL to a *_test database")
async def test_the_postgres_ledger_on_migration_042() -> None:
    import asyncpg

    from bot.campaign import PostgresCampaignStore, plan_campaign

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=2)
    try:
        cid = await PostgresCampaignStore(pool).create(plan_campaign("участок в Мадриде от 2000 м², покупка"), chat_id=1,
                                                       requested_by=1, source_text="участок", actor="test")
        sink = costs.PostgresLedger(pool)
        costs.install(sink, budget_usd=0.05)
        with costs.scope(cid):
            await costs.llm("anthropic/claude-sonnet-4.5", {"usage": {"cost": 0.03}})
            await costs.error("llm", "model_request_refused_400")
            await costs.skip("llm", "prefilter_area", item="pisos.com")
            assert not await costs.over_budget()
            await costs.record("scrape", item="idealista.com", cost_usd=0.025)
            assert await costs.over_budget()
        await costs.record("llm", cost_usd=1)  # no campaign: a null campaign_id row
        summary = await sink.summary(cid)
        assert summary.by_stage == {"llm": pytest.approx(0.03), "scrape": pytest.approx(0.025)}
        assert summary.errors == {"llm:model_request_refused_400": 1} and summary.skips == {"llm:prefilter_area": 1}
        assert await pool.fetchval("select count(*) from campaign_costs where campaign_id is null") == 1
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("insert into campaign_costs (stage) values ('bogus')")
    finally:
        costs.install(None)
        await pool.close()


async def test_unique_source_cost_replaces_estimate_and_keeps_regular_calls(ledger) -> None:
    with costs.scope("c1"):
        for _ in range(3):
            await costs.record_unique("api", key="listing-source:c1:idealista", provider="apify", item="actor",
                                      cost_usd=0.5, code="estimated", units=1)
        await costs.record_unique("api", key="listing-source:c1:idealista", provider="apify", item="actor",
                                  cost_usd=0.03, units=1)
        await costs.record("api", provider="apify", item="actor", cost_usd=0.02)
    assert len(ledger.entries) == 2
    assert ledger.entries[0].code == ""
    assert await ledger.spent("c1") == pytest.approx(0.05)


async def test_unique_cost_actual_usage_survives_later_failure_estimates(ledger) -> None:
    with costs.scope("c1"):
        await costs.record_unique("api", key="source:c1", provider="apify", item="actor",
                                  cost_usd=0.03)
        await costs.record_unique("api", key="source:c1", provider="apify", item="actor",
                                  cost_usd=0.5, code="estimated_launch_uncertain")
        assert await ledger.spent("c1") == pytest.approx(0.03)
        await costs.record_unique("api", key="source:c1", provider="apify", item="actor",
                                  cost_usd=0.04)
    assert len(ledger.entries) == 1
    assert ledger.entries[0].code == ""
    assert await ledger.spent("c1") == pytest.approx(0.04)
