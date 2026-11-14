"""Q43 REPORT_PRECISE_REASONS: a portal our own caps stopped is not «до него не дошла очередь запросов», and a listing
rejected for more rooms than asked is «больше комнат», not «меньше комнат» (mocks only, no live calls).

Sim run b3-g2-s4 (a flat in Valencia, up to 250 000 €, 2-3 rooms, WEB_SEARCH_MAX_PAGES_PER_CAMPAIGN=30): the source
runs are imported in name order, Fotocasa (20) and Habitaclia (10) used the 30 pages, the first valid Idealista row
hit ``_source_limit`` = 0 and the run ended ``apify_import_limits``; the report said «Idealista — до него не дошла
очередь запросов». Two of the «меньше комнат — 11» listings had 4 rooms.
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.final_report import REASONS, FinalReporter
from bot.campaign.metrics import REASONS as METRIC_REASONS
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore, OutcomeCount
from bot.campaign.settings import CampaignRunnerSettings
from bot.campaign.summary import site_lines, without_limited
from bot.campaign.tolerance import Match, Request
from bot.web_search.models import SiteReport
from bot.web_search.sources import SourceListing
from bot.web_search.store import MemoryWebStore
from tests.test_campaign_runner import FakeMessenger
from tests.test_listing_source_worker import (  # noqa: F401
    FakeSource,
    provider_worker,
    source_ledger,
)
from tests.test_web_search import campaign, run_until_done

PORTALS = ("idealista.com", "fotocasa.es", "habitaclia.com")
REPORTS = [SiteReport("fotocasa.es", links=20, read=20, read_api=20), SiteReport("idealista.com", api_limited=True)]
LIMITED = "Idealista — не прочитан: исчерпан лимит страниц или бюджета поиска"


class FotocasaSource(FakeSource):
    name = "apify_fotocasa"
    hosts = frozenset({"fotocasa.es"})

    async def search(self, task, *, limit):
        found = await super().search(task, limit=limit)
        return [SourceListing(f"https://www.fotocasa.es/es/comprar/terreno/madrid-capital/{191376520 + n}/d",
                              x.title, plot_m2=x.plot_m2, area_m2=x.area_m2, deal=x.deal,
                              property_type=x.property_type, description="x") for n, x in enumerate(found)]


async def test_page_cap_stops_the_later_portal_and_the_store_says_so(source_ledger):  # noqa: F811
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store = MemoryWebStore(campaigns)
    fotocasa, idealista = FotocasaSource(rows=3), FakeSource(rows=2)
    worker = provider_worker(campaigns, store, fotocasa, max_pages_per_campaign=3)
    worker.sources = (idealista, fotocasa)  # the plan's order; imports go by name: fotocasa first
    await run_until_done(worker, cid)
    runs = {r.name: r for r in await store.source_runs(cid)}
    assert idealista.launches == 1 and runs["apify_idealista"].listings, "Idealista ran and returned rows"
    assert (runs["apify_idealista"].state, runs["apify_idealista"].error_code) == ("failed", "apify_import_limits")
    report = {r.host: r for r in await store.site_report(cid)}
    assert report["idealista.com"].api_limited and not report["idealista.com"].links
    assert not report["fotocasa.es"].api_limited


def test_flag_on_a_portal_stopped_by_our_caps_says_so():
    lines, _ = site_lines([], REPORTS, PORTALS, limited=True)
    assert lines[0] == LIMITED
    lines, _ = site_lines([], [*REPORTS, SiteReport("habitaclia.com", api_limited=True)], PORTALS,
                          every_portal=True, limited=True)
    assert "Habitaclia — не прочитан: исчерпан лимит страниц или бюджета поиска" in lines


def test_flag_off_the_line_is_as_before_and_the_limited_only_row_is_dropped():
    lines, _ = site_lines([], REPORTS, PORTALS)
    assert lines[0] == "Idealista — до него не дошла очередь запросов"
    assert without_limited(REPORTS) == [REPORTS[0]]
    kept = SiteReport("idealista.com", queries=1, api_limited=True)
    assert without_limited([kept]) == [kept], "a row with other numbers stays"


@pytest.mark.parametrize("flag", [True, False])
async def test_final_report_names_the_cap_only_with_the_flag(flag):
    text = await FinalReporter(precise_reasons=flag).build("Квартира", Request(), [], [], [], REPORTS, PORTALS)
    assert (LIMITED in text) is flag
    assert ("до него не дошла очередь запросов" in text) is not flag


async def test_final_report_lists_more_rooms_apart():
    outcomes = [OutcomeCount("held", "excluded", "rooms", 9), OutcomeCount("held", "excluded", "rooms_more", 2)]
    text = await FinalReporter().build("Квартира", Request(rooms=2), outcomes, [], [], [], PORTALS)
    assert "• меньше комнат — 9" in text and "• больше комнат — 2" in text
    assert REASONS["rooms_more"] == METRIC_REASONS["rooms_more"] == "больше комнат"


GOAL = "Купить квартиру в Мадриде до 250 000 €, от 2 комнат"


async def _rejected_why(flag: bool, rooms: int) -> str | None:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    runner = CampaignRunner(campaigns, store, FakeMessenger(), None, owner_ids={7},
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=0,
                                                report_precise_reasons=flag))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=1, requested_by=42, source_text=GOAL, actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    store.add_finding(cid, "f1", "🏠 f1", payload={"price_amount": 200_000, "price_currency": "EUR", "deal_type": "sale",
                                                   "property_type": "apartment", "rooms": rooms, "location": "Madrid"},
                      original="post", vertical="real_estate")
    runner._judge = AsyncMock(return_value=Match("excluded", math.inf, "rooms"))  # the reviewer failed «rooms»
    await runner.tick()
    assert store.buckets["f1"][0] == "excluded"
    return store.whys["f1"]


@pytest.mark.parametrize(("flag", "rooms", "why"), [(True, 4, "rooms_more"), (True, 1, "rooms"),
                                                    (False, 4, "rooms"), (False, 1, "rooms")])
async def test_more_rooms_is_its_own_reason_only_with_the_flag(flag, rooms, why):
    assert await _rejected_why(flag, rooms) == why


def test_settings_pass_the_flag(monkeypatch):
    monkeypatch.delenv("REPORT_PRECISE_REASONS", raising=False)
    base = {"_env_file": None, "DATABASE_URL": "postgresql://x", "TELEGRAM_TOKEN": "t", "OPENROUTER_API_KEY": ""}
    off = CampaignRunnerSettings(**base)
    assert off.report_precise_reasons is False and off.final_reporter().precise_reasons is False
    on = CampaignRunnerSettings(**base, REPORT_PRECISE_REASONS=True)
    assert on.final_reporter().precise_reasons is True and on.runner_config().report_precise_reasons is True
    assert off.runner_config().report_precise_reasons is False
