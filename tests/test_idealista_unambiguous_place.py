"""Q17 IDEALISTA_UNAMBIGUOUS_PLACE: a city goes to ``igolaizola/idealista-scraper`` as its centre and a circle, and an
output wholly from another place is ``apify_wrong_place`` (mocks only, no live calls).

Run G2 (Valencia): ``location: "Valencia"`` ran as València d'Àneu (Alt Àneu, Lleida) and returned 2 rows from there.
"""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.acquisition.apify_client import PermanentApifyError
from bot.campaign.summary import site_lines
from bot.utils import costs
from bot.web_search.models import SourceRun
from bot.web_search.queries import QueryTask
from bot.web_search.sources.apify import SourceRunContext, source_run_scope
from bot.web_search.sources.portals import ApifyIdealistaPortalSource
from bot.web_search.store import MemoryWebStore, _provider_error

VALENCIA = (39.4699, -0.3763)


def task(**kwargs):
    base = QueryTask("terreno", "terreno", "Valencia", {}, "real_estate",
                     constraints={"deal": "sale", "max_price": "300000"}, country_code="ES")
    return replace(base, **kwargs)


def context():
    return SourceRunContext(campaign_id="campaign", source_name="apify_idealista", run_id=None, dataset_id=None,
                            max_charge_usd=.05, launch_allowed=True, save_run=AsyncMock(), reconcile_usage=AsyncMock())


def row(**kwargs):
    base = {"propertyCode": "100000001", "url": "https://www.idealista.com/inmueble/100000001/",
            "operation": "sale", "propertyType": "land", "country": "es", "municipality": "València",
            "province": "Valencia", "address": "Terreno en Benimaclet", "price": 250000.0, "size": 900.0,
            "latitude": 39.487, "longitude": -0.358,
            "contactInfo": {"commercialName": "Agencia Uno", "phone1": {"phoneNumber": "900000001"}}}
    return {**base, **kwargs}


def lleida(code):
    return row(propertyCode=code, url=f"https://www.idealista.com/inmueble/{code}/", municipality="Alt Àneu",
               province="Lleida", address="Terreno en València d'Àneu", latitude=42.637, longitude=1.122)


def geocoder(center=VALENCIA):
    found = AsyncMock()
    found.resolve.return_value = center
    return found


def source(rows, requests, **kwargs):
    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run1", "status": "SUCCEEDED",
                                                      "defaultDatasetId": "data1", "usageTotalUsd": .01}})
        if "actor-runs" in request.url.path:
            return httpx.Response(200, json={"data": {"usageTotalUsd": .01}})
        return httpx.Response(200, json=rows)
    return ApifyIdealistaPortalSource("secret-token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                      sleep=AsyncMock(), **kwargs)


@pytest.fixture
def ledger(monkeypatch):
    found = costs.MemoryLedger()
    monkeypatch.setitem(costs._state, "ledger", found)
    monkeypatch.setitem(costs._state, "budget", 0.0)
    return found


async def _input(the_task=None, **kwargs):
    requests = []
    adapter = source([], requests, **kwargs)
    with source_run_scope(context()):
        await adapter.search(the_task or task(), limit=5)
    await adapter.aclose()
    return json.loads(next(r for r in requests if r.method == "POST").content)


async def test_flag_off_keeps_the_city_name_even_with_a_geocoder(ledger):
    found = geocoder()
    sent = await _input(geocoder=found)
    assert sent == {"maxItems": 5, "operation": "sale", "propertyType": "lands", "country": "es",
                    "sortBy": "mostRecent", "location": "Valencia", "maxPrice": "300000"}
    found.resolve.assert_not_awaited()


async def test_flag_on_sends_the_city_centre_and_a_circle(ledger):
    found = geocoder()
    sent = await _input(geocoder=found, unambiguous_place=True)
    assert sent == {"maxItems": 5, "operation": "sale", "propertyType": "lands", "country": "es",
                    "sortBy": "mostRecent", "latitude": 39.4699, "longitude": -0.3763, "distanceKm": 15,
                    "maxPrice": "300000"}
    found.resolve.assert_awaited_once_with("Valencia")


@pytest.mark.parametrize("found", [None, geocoder(None)])
async def test_flag_on_without_a_centre_keeps_the_name(ledger, found):
    sent = await _input(geocoder=found, unambiguous_place=True)
    assert sent["location"] == "Valencia" and "latitude" not in sent and "distanceKm" not in sent


async def test_flag_on_leaves_province_and_radius_tasks_as_they_were(ledger):
    province = await _input(task(place_level="province"), geocoder=geocoder(), unambiguous_place=True)
    assert province["location"] == "Valencia" and "latitude" not in province
    radius = await _input(task(radius_km=30.0), geocoder=geocoder(), unambiguous_place=True)
    assert radius["distanceKm"] == 30 and "location" not in radius


async def test_flag_on_lleida_rows_end_the_run_as_wrong_place(ledger):
    adapter = source([lleida("100000002"), lleida("100000003")], [], geocoder=geocoder(), unambiguous_place=True)
    with source_run_scope(context()), pytest.raises(PermanentApifyError) as raised:
        await adapter.search(task(), limit=5)
    assert raised.value.code == "apify_wrong_place"
    skips = [(e.kind, e.code, e.units) for e in ledger.entries if e.kind == "skip"]
    assert skips == [("skip", "apify_wrong_place", 2)]
    await adapter.aclose()


async def test_flag_off_lleida_rows_stay_invalid_listings(ledger):
    adapter = source([lleida("100000002"), lleida("100000003")], [])
    with source_run_scope(context()):
        assert await adapter.search(task(), limit=5) == []
    assert [(e.code, e.units) for e in ledger.entries if e.kind == "skip"] == [("apify_invalid_listing", 2)]
    await adapter.aclose()


async def test_flag_on_some_rows_from_elsewhere_are_counted_and_the_rest_kept(ledger):
    rows = [row(), lleida("100000002"), row(propertyCode="100000004", url="https://www.idealista.com/inmueble/100000004/",
                                            operation="rent")]
    adapter = source(rows, [], geocoder=geocoder(), unambiguous_place=True)
    with source_run_scope(context()):
        listings = await adapter.search(task(), limit=5)
    assert [x.url for x in listings] == ["https://www.idealista.com/inmueble/100000001/"]
    assert listings[0].plot_m2 == 900 and listings[0].phone == "900000001" and listings[0].advertiser == "Agencia Uno"
    skips = sorted((e.code, e.units) for e in ledger.entries if e.kind == "skip")
    assert skips == [("apify_invalid_listing", 1), ("apify_wrong_place", 1)]
    await adapter.aclose()


def test_wrong_place_needs_a_place_the_row_gives():
    adapter = ApifyIdealistaPortalSource("secret-token", unambiguous_place=True)
    assert adapter._wrong_place(lleida("1"), task(), VALENCIA)
    assert not adapter._wrong_place(row(municipality="", province=""), task(), VALENCIA)  # no place: not «elsewhere»
    assert not adapter._wrong_place(row(), task(), VALENCIA)
    assert adapter._wrong_place(lleida("1"), task(place_level="province"), None)
    assert adapter._wrong_place(lleida("1"), task(radius_km=20.0), VALENCIA)
    assert not ApifyIdealistaPortalSource("secret-token")._wrong_place(lleida("1"), task(), VALENCIA)  # flag off


async def test_wrong_place_is_its_own_report_line_not_a_portal_error():
    assert not _provider_error("apify_wrong_place")
    web = MemoryWebStore()
    web.sources[("c1", "apify_idealista")] = SourceRun("c1", "apify_idealista", "failed", error_code="apify_wrong_place")
    reports = await web.site_report("c1")
    assert [(r.host, r.api_wrong_place, r.api_failed, r.api_empty) for r in reports] == [
        ("idealista.com", True, False, False)]
    lines, _ = site_lines([], reports, ["idealista.com", "fotocasa.es"])
    assert "Idealista — портал вернул объекты из другого места" in lines


async def test_setting_reaches_the_source(monkeypatch):
    from bot.web_search.settings import WebSearchSettings

    for name in ("IDEALISTA_UNAMBIGUOUS_PLACE", "APIFY_IDEALISTA_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    env = {"APIFY_TOKEN": "secret-token", "APIFY_IDEALISTA_ENABLED": True, "APIFY_IDEALISTA_PROVIDER": "igolaizola"}
    off, = WebSearchSettings(_env_file=None, **env).sources()
    on, = WebSearchSettings(_env_file=None, IDEALISTA_UNAMBIGUOUS_PLACE=True, **env).sources()
    try:
        assert off.unambiguous_place is False and on.unambiguous_place is True
    finally:
        await off.aclose()
        await on.aclose()
