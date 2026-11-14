"""Q33 (CAMPAIGN_EMPTY_REPORT): an investor search whose sources all came back empty (the web stage found nothing or
failed, the reach found nobody, no Facebook group) ends and sends «📋 Отчёт по поиску» with why. In-memory stores and
fakes only."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.final_report import FinalReporter
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore, SearchOutcome
from bot.web_search.models import WebStatus
from tests.test_campaign_runner import CHAT, Clock, FakeMessenger

GOAL = "ищу соинвесторов в проекты в Малаге, тикет от 50 000 €"
REPORT = "📋 Отчёт по поиску"


class NoGroups:
    """The discovery with no group in the task and no browser (the bench's ``NoSearchDiscovery``): Facebook adds
    nothing and the campaign goes on with its other sources."""

    def __init__(self, campaigns: MemoryCampaignStore) -> None:
        self.campaigns = campaigns

    async def run(self, campaign_id: str) -> None:
        await self.campaigns.set_state(campaign_id, "running", "campaign:discovery")


class Web:
    """The web stage's status: searching until ``active`` is cleared, then done with nothing found."""

    def __init__(self) -> None:
        self.active = True

    async def web_status(self, campaign_id: str) -> WebStatus:
        return WebStatus(self.active, None, "сайты: запросов 80/80 · поиск" if self.active else "")

    async def site_report(self, campaign_id: str) -> list:
        return []


OUTCOME = SearchOutcome(web_queries=80, web_hits=0, web_failed=12, reach_queries=16, reach_hits=0, reach_failed=2,
                        errors=(("http_503", 12), ("timeout", 2)))


async def _campaign(**config):
    campaigns = MemoryCampaignStore()
    store, messenger, clock, web = MemoryRunStore(campaigns), FakeMessenger(), Clock(), Web()
    runner = CampaignRunner(campaigns, store, messenger, NoGroups(campaigns), now=clock, owner_ids=set(), web=web,
                            final_report=FinalReporter(),
                            config=RunnerConfig(relevance_fail_closed=False, **config))
    plan = plan_campaign(GOAL, vertical="investors", location="Málaga")
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=8, source_text=GOAL, actor="test")
    store.search_outcomes[cid] = OUTCOME
    return campaigns, store, messenger, clock, web, runner, cid


def _reports(messenger: FakeMessenger) -> list[str]:
    return [t for _, _, t in messenger.sent if t.startswith(REPORT)]


async def _run(campaigns, runner, clock, web, cid, *, deliver=None) -> None:
    """Steps while the web stage searches (nothing may end), then the web stage ends and the drain completes."""
    for _ in range(3):
        await runner.step(cid)
        clock.advance(3600)  # past every grace: only the web stage holds the campaign
        assert (await campaigns.get(cid)).state == "running", "the web stage still searches: no premature end"
    if deliver is not None:
        deliver()
    web.active = False
    await runner.step(cid)
    await runner.step(cid)


async def test_flag_on_an_empty_investor_search_completes_with_the_nothing_found_report() -> None:
    campaigns, _store, messenger, clock, web, runner, cid = await _campaign(empty_report=True)
    await _run(campaigns, runner, clock, web, cid)
    campaign = await campaigns.get(cid)
    assert (campaign.state, campaign.stop_reason) == ("completed", "queue_exhausted")
    reports = _reports(messenger)
    assert len(reports) == 1, "one report, once, though nothing was spent and CAMPAIGN_INVESTOR_REPORT is off"
    report = reports[0]
    assert "Людей и контактов пока не найдено." in report
    assert "Ничего не нашёл." in report
    assert "• Сайты: запросов 80, с результатами 0, с ошибкой 12 — выдача пустая" in report
    assert "• Люди и контакты: запросов 16, с результатами 0, с ошибкой 2 — выдача пустая" in report
    assert "• Группы Facebook: ни одной не прочитано" in report
    assert "⚠️ Ошибки поиска: http_503 ×12, timeout ×2" in report


async def test_flag_on_with_the_investor_report_flag_too_the_report_has_the_reasons() -> None:
    campaigns, _store, messenger, clock, web, runner, cid = await _campaign(empty_report=True, investor_report=True)
    await _run(campaigns, runner, clock, web, cid)
    reports = _reports(messenger)
    assert len(reports) == 1 and "Ничего не нашёл." in reports[0] and "выдача пустая" in reports[0]


async def test_flag_off_the_empty_investor_search_ends_as_before_without_a_report() -> None:
    for investor_report in (False, True):
        campaigns, _store, messenger, clock, web, runner, cid = await _campaign(investor_report=investor_report)
        assert runner.config.empty_report is False
        await _run(campaigns, runner, clock, web, cid)
        assert (await campaigns.get(cid)).state == "completed"
        assert _reports(messenger) == [], "nothing found and nothing spent: no report, as before"
        assert messenger.statuses()[-1] == "Пока ничего подходящего не нашёл."


async def test_with_leads_the_flag_changes_nothing() -> None:
    texts = {}
    for investor_report in (False, True):
        for flag in (False, True):
            campaigns, store, messenger, clock, web, runner, cid = await _campaign(empty_report=flag,
                                                                                    investor_report=investor_report)

            def deliver(store=store, cid=cid) -> None:
                store.deliveries[(cid, "reach:linkedin.com/in/someone")] = 99  # a reach contact was sent

            await _run(campaigns, runner, clock, web, cid, deliver=deliver)
            texts[(investor_report, flag)] = _reports(messenger)
    assert texts[(False, False)] == texts[(False, True)] == [], "without the investor report flag: none, as before"
    assert texts[(True, False)] == texts[(True, True)]
    assert len(texts[(True, True)]) == 1 and "Ничего не нашёл" not in texts[(True, True)][0]
    assert "доставок из поиска 1" in texts[(True, True)][0]


async def test_a_cancelled_empty_investor_search_gets_no_report() -> None:
    campaigns, _store, messenger, clock, _web, runner, cid = await _campaign(empty_report=True)
    await campaigns.set_state(cid, "running", "test")
    await campaigns.cancel(cid, "test")
    clock.at = (await campaigns.get(cid)).finished_at + timedelta(minutes=1)
    await runner.step(cid)
    assert _reports(messenger) == []


# --- PostgreSQL: what the queries gave (web_search_queries, reach_queries) ------------------------------------------

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"),
                              reason="set SYSTEM_TEST_DATABASE_URL to a *_test database")
MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))


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


@needs_db
async def test_postgres_search_outcome_counts_queries_hits_and_errors(pool) -> None:
    from bot.campaign.runs import PostgresRunStore
    from bot.campaign.store import PostgresCampaignStore
    from bot.orchestra.store import SafetyLimits

    campaigns = PostgresCampaignStore(pool)
    store = PostgresRunStore(pool, SafetyLimits())
    plan = plan_campaign(GOAL, vertical="investors", location="Málaga")
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=8, source_text=GOAL, actor="test")
    assert await store.search_outcome(cid) == SearchOutcome()
    rows = [("q1", "searched", 0, None), ("q2", "searched", 3, None), ("q3", "failed", None, "http_503"),
            ("q4", "failed", None, None), ("q5", "pending", None, None), ("q6", "skipped", None, None)]
    for text, state, results, error in rows:
        await pool.execute(
            """insert into web_search_queries (campaign_id, round_no, query_text, query_key, state, result_count, error_code)
               values ($1::uuid, 1, $2, $2, $3, $4, $5)""", cid, text, state, results, error)
    for text, hits, error in (("r1", 0, None), ("r2", 0, "http_503"), ("r3", 2, None)):
        await pool.execute("""insert into reach_queries (campaign_id, query, platform, hits, kept, error_code)
                              values ($1::uuid, $2, 'web', $3, 0, $4)""", cid, text, hits, error)
    assert await store.search_outcome(cid) == SearchOutcome(
        web_queries=4, web_hits=1, web_failed=2, reach_queries=3, reach_hits=1, reach_failed=1,
        errors=(("http_503", 2), ("error", 1)))
