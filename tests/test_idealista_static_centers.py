"""Q30 IDEALISTA_STATIC_CENTERS: without a geocoder centre a listed city still goes to ``igolaizola/idealista-scraper``
with a centre and a circle (static list ``bot/web_search/city_centers.py``); a geocoder answer wins (mocks only)."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.utils import costs
from bot.web_search.city_centers import _ALIASES, _CENTERS, static_city_area
from bot.web_search.queries import QueryTask
from bot.web_search.sources.apify import SourceRunContext, source_run_scope
from bot.web_search.sources.portals import ApifyIdealistaPortalSource


def task(**kwargs):
    base = QueryTask("terreno", "terreno", "Valencia", {}, "real_estate",
                     constraints={"deal": "sale", "max_price": "300000"}, country_code="ES")
    return replace(base, **kwargs)


def context():
    return SourceRunContext(campaign_id="campaign", source_name="apify_idealista", run_id=None, dataset_id=None,
                            max_charge_usd=.05, launch_allowed=True, save_run=AsyncMock(), reconcile_usage=AsyncMock())


def geocoder(found=None, error=None):
    coder = AsyncMock()
    coder.resolve_area.return_value = found
    if error is not None:
        coder.resolve_area.side_effect = error
    return coder


@pytest.fixture(autouse=True)
def ledger(monkeypatch):
    found = costs.MemoryLedger()
    monkeypatch.setitem(costs._state, "ledger", found)
    monkeypatch.setitem(costs._state, "budget", 0.0)
    return found


async def _input(the_task=None, **kwargs):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run1", "status": "SUCCEEDED",
                                                      "defaultDatasetId": "data1", "usageTotalUsd": .01}})
        if "actor-runs" in request.url.path:
            return httpx.Response(200, json={"data": {"usageTotalUsd": .01}})
        return httpx.Response(200, json=[])

    adapter = ApifyIdealistaPortalSource("secret-token", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                         sleep=AsyncMock(), **kwargs)
    with source_run_scope(context()):
        await adapter.search(the_task or task(), limit=5)
    await adapter.aclose()
    return json.loads(next(r for r in requests if r.method == "POST").content)


BASE = {"maxItems": 5, "operation": "sale", "propertyType": "lands", "country": "es", "sortBy": "mostRecent",
        "location": "Valencia", "maxPrice": "300000"}


@pytest.mark.parametrize("found", [None, geocoder(None)])
async def test_flag_off_without_a_geocoder_centre_keeps_the_name(found):
    assert await _input(geocoder=found, unambiguous_place=True) == BASE
    assert await _input(geocoder=found, unambiguous_place=False, static_centers=True) == BASE


@pytest.mark.parametrize("found", [None, geocoder(None), geocoder(error=RuntimeError("redis down"))])
async def test_flag_on_without_a_geocoder_centre_sends_the_listed_centre(found):
    sent = await _input(geocoder=found, unambiguous_place=True, static_centers=True)
    assert sent["location"] == "Valencia"
    assert (sent["latitude"], sent["longitude"]) == (39.4699, -0.3763)
    assert 12 <= sent["distanceKm"] <= 30
    assert {key: value for key, value in sent.items() if key not in ("latitude", "longitude", "distanceKm")} == BASE


async def test_flag_on_believes_the_geocoder_when_it_answers():
    found = geocoder(((39.47, -0.38), (39.500, 39.518, -0.425, -0.402)))
    sent = await _input(geocoder=found, unambiguous_place=True, static_centers=True)
    assert (sent["latitude"], sent["longitude"], sent["distanceKm"]) == (39.47, -0.38, 3)
    found.resolve_area.assert_awaited_once_with("Valencia")


async def test_flag_on_leaves_an_unlisted_town_and_a_province_as_they_were():
    town = await _input(task(location="Burjassot"), geocoder=None, unambiguous_place=True, static_centers=True)
    assert town["location"] == "Burjassot" and "latitude" not in town and "distanceKm" not in town
    province = await _input(task(place_level="province"), geocoder=None, unambiguous_place=True, static_centers=True)
    assert "latitude" not in province and "distanceKm" not in province


@pytest.mark.parametrize("name", ["València", "VALENCIA", " valencia "])
def test_aliases_and_spelling_find_the_same_city(name):
    assert static_city_area(name) == _CENTERS["Valencia"]


def test_list_is_sane():
    assert static_city_area("") is None and static_city_area("Atlantis") is None
    assert all(name in _CENTERS for name in _ALIASES.values())
    for name in ("Madrid", "Barcelona", "Valencia", "Sevilla", "Málaga", "Alicante", "Zaragoza", "Bilbao", "Murcia",
                 "Palma", "Las Palmas de Gran Canaria", "Marbella"):
        assert name in _CENTERS
    for name, ((lat, lon), (south, north, west, east)) in _CENTERS.items():
        # Spain incl. the Canaries; the centre lies inside its own box
        assert 27 <= south < north <= 44 and -19 <= west < east <= 5, name
        assert south <= lat <= north and west <= lon <= east, name


async def test_setting_reaches_the_source(monkeypatch):
    from bot.web_search.settings import WebSearchSettings

    for name in ("IDEALISTA_STATIC_CENTERS", "APIFY_IDEALISTA_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    env = {"APIFY_TOKEN": "secret-token", "APIFY_IDEALISTA_ENABLED": True, "APIFY_IDEALISTA_PROVIDER": "igolaizola"}
    off, = WebSearchSettings(_env_file=None, **env).sources()
    on, = WebSearchSettings(_env_file=None, IDEALISTA_STATIC_CENTERS=True, **env).sources()
    try:
        assert off.static_centers is False and on.static_centers is True
    finally:
        await off.aclose()
        await on.aclose()
