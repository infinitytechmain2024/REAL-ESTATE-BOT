"""Q37 REPORT_EVERY_PORTAL: every portal source that ran is in «По источникам», and the Habitaclia actor's
``noResults`` marker row is an empty output, not an ``apify_invalid_listing`` (mocks only, no live calls).

Sim run b2-n4-s3 (plots, Comunidad de Madrid): Habitaclia returned one ``{"noResults": true, ...}`` row; it was
booked as «apify_invalid_listing 1» in «Отсеяно до ИИ» and the portal was missing from the report.
"""

from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.campaign.final_report import FinalReporter
from bot.campaign.runs import SourceCount
from bot.campaign.settings import CampaignRunnerSettings
from bot.campaign.summary import site_lines
from bot.campaign.tolerance import Request
from bot.utils import costs
from bot.web_search.models import SiteReport
from bot.web_search.queries import QueryTask
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources.apify import SourceRunContext, source_run_scope
from bot.web_search.sources.portals import ApifyHabitacliaSource

MARKER = {"noResults": True, "source": "https://www.habitaclia.com/terrenos_y_solares-madrid.htm",
          "url": "https://www.habitaclia.com/terrenos_y_solares-madrid.htm",
          "reason": "The search returned zero listings - nothing matches the requested location and filters.",
          "error": None, "scrapedAt": "2026-11-14T03:35:40.222Z"}
ROW = {"detailUrl": "https://www.habitaclia.com/comprar-terreno-madrid-i500003456789.htm", "title": "Terreno en Madrid",
       "price": 250000, "priceCurrency": "EUR", "areaSqm": 3200, "rooms": None, "city": "Madrid", "province": "Madrid",
       "location": "Barajas, Madrid", "advertiserName": "Inmobiliaria Ejemplo", "transactionType": "sale",
       "description": None, "shortDescription": "Parcela de 3.200 m2."}
# What the land task searches (bot/web_search/urls.py SPAIN_PORTALS_BY_KIND["land"]): no Habitaclia.
LAND_PORTALS = ("idealista.com", "fotocasa.es", "terrenos.es", "sareb.es", "milanuncios.com", "pisos.com", "kyero.com")
REPORTS = [SiteReport("idealista.com", links=20, read=20, read_api=20),
           SiteReport("fotocasa.es", links=20, read=20, read_api=20),
           SiteReport("milanuncios.com", links=5, read=5, read_api=5),
           SiteReport("habitaclia.com", api_empty=True)]
SOURCES = [SourceCount("website", "idealista.com", posts=20, held=18),
           SourceCount("website", "fotocasa.es", posts=20, sent=1, held=18),
           SourceCount("website", "milanuncios.com", posts=5, held=1)]


def task(**kwargs):
    base = QueryTask("terreno", "terreno", "Madrid", {}, "real_estate",
                     constraints={"deal": "sale", "min_area": "2000", "max_price": "3000000"}, country_code="ES")
    return replace(base, **kwargs)


def context():
    return SourceRunContext(campaign_id="campaign", source_name="apify_habitaclia", run_id=None, dataset_id=None,
                            max_charge_usd=.05, launch_allowed=True, save_run=AsyncMock(), reconcile_usage=AsyncMock())


def habitaclia(rows, **kwargs):
    def handler(request):
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run1", "status": "SUCCEEDED",
                                                      "defaultDatasetId": "data1", "usageTotalUsd": .01}})
        if "actor-runs" in request.url.path:
            return httpx.Response(200, json={"data": {"usageTotalUsd": .01}})
        return httpx.Response(200, json=rows)
    return ApifyHabitacliaSource("secret-token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                 sleep=AsyncMock(), **kwargs)


@pytest.fixture
def ledger(monkeypatch):
    found = costs.MemoryLedger()
    monkeypatch.setitem(costs._state, "ledger", found)
    monkeypatch.setitem(costs._state, "budget", 0.0)
    return found


async def _search(rows, **kwargs):
    adapter = habitaclia(rows, **kwargs)
    with source_run_scope(context()):
        found = await adapter.search(task(), limit=5)
    await adapter.aclose()
    return found


def _skips(ledger):
    return [(e.code, e.units) for e in ledger.entries if e.kind == "skip"]


async def test_flag_on_the_no_results_marker_is_an_empty_output_not_an_invalid_listing(ledger):
    assert await _search([MARKER], no_results_empty=True) == []
    assert _skips(ledger) == []


async def test_flag_on_a_real_bad_row_is_still_an_invalid_listing(ledger):
    found = await _search([MARKER, ROW, {**ROW, "city": "Getafe"}], no_results_empty=True)
    assert [x.url for x in found] == [ROW["detailUrl"]]
    assert _skips(ledger) == [("apify_invalid_listing", 1)]


async def test_flag_off_the_marker_stays_an_invalid_listing(ledger):
    assert await _search([MARKER]) == []
    assert _skips(ledger) == [("apify_invalid_listing", 1)]


def test_flag_on_a_portal_that_ran_and_found_nothing_has_its_line():
    lines, nothing = site_lines(SOURCES, REPORTS, LAND_PORTALS, every_portal=True)
    assert lines[-1] == "Habitaclia — объявлений не найдено"
    assert len(lines) == 4 and [s.host for s in nothing] == ["terrenos.es", "sareb.es", "pisos.com", "kyero.com"]
    # a portal of the kind list whose run failed or found nothing is shown too, the others stay «nothing»
    reports = [*REPORTS, SiteReport("pisos.com", api_failed=True), SiteReport("yaencontre.com", api_wrong_place=True)]
    lines, nothing = site_lines(SOURCES, reports, LAND_PORTALS, every_portal=True)
    assert any(line.endswith("— портал вернул ошибку") for line in lines)
    assert any(line.endswith("портал вернул объекты из другого места") for line in lines)
    assert "pisos.com" not in [s.host for s in nothing]


def test_flag_off_the_lines_are_as_before():
    lines, _nothing = site_lines(SOURCES, REPORTS, LAND_PORTALS)
    assert len(lines) == 3 and not any("Habitaclia" in line for line in lines)
    empty = [SiteReport("idealista.com", api_empty=True), SiteReport("fotocasa.es", links=1, read=1)]
    assert site_lines([], empty, LAND_PORTALS)[0][0] == "Idealista — 0 объявлений по запросу"
    assert site_lines([], empty, LAND_PORTALS, every_portal=True)[0][0] == "Idealista — объявлений не найдено"


@pytest.mark.parametrize("flag", [True, False])
async def test_final_report_shows_habitaclia_only_with_the_flag(flag):
    text = await FinalReporter(every_portal=flag).build("Участок", Request(), [], [], SOURCES, REPORTS, LAND_PORTALS)
    assert "По источникам:" in text and "Milanuncios" in text
    assert ("Habitaclia — объявлений не найдено" in text) is flag


async def test_settings_pass_the_flag(monkeypatch):
    monkeypatch.delenv("REPORT_EVERY_PORTAL", raising=False)
    assert WebSearchSettings(_env_file=None).report_every_portal is False
    assert CampaignRunnerSettings(_env_file=None, DATABASE_URL="postgresql://x", TELEGRAM_TOKEN="t"
                                  ).report_every_portal is False
    assert ApifyHabitacliaSource("secret-token").no_results_empty is False
    settings = WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token", APIFY_HABITACLIA_ENABLED=True,
                                 REPORT_EVERY_PORTAL=True)
    sources = settings.sources()
    try:
        assert [s.no_results_empty for s in sources] == [True]
    finally:
        for source in sources:
            await source.aclose()
    reporter = CampaignRunnerSettings(_env_file=None, DATABASE_URL="postgresql://x", TELEGRAM_TOKEN="t",
                                      REPORT_EVERY_PORTAL=True, OPENROUTER_API_KEY="").final_reporter()
    assert reporter is not None and reporter.every_portal is True
