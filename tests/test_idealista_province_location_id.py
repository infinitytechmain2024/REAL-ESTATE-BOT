"""Q36 IDEALISTA_PROVINCE_LOCATION_ID: a province task goes to ``igolaizola/idealista-scraper`` as Idealista's
Location ID ``0-EU-ES-<INE code>`` (``bot/web_search/provinces.py``), not by name (mocks only)."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.utils import costs
from bot.web_search.provinces import _PROVINCES, province_code, province_location_id
from bot.web_search.queries import QueryTask
from bot.web_search.sources.apify import SourceRunContext, source_run_scope
from bot.web_search.sources.portals import ApifyIdealistaPortalSource


def task(**kwargs):
    base = QueryTask("terreno", "terreno", "Madrid", {}, "real_estate",
                     constraints={"deal": "sale", "max_price": "600000", "min_area": "300"}, country_code="ES",
                     place_level="province")
    return replace(base, **kwargs)


def context():
    return SourceRunContext(campaign_id="campaign", source_name="apify_idealista", run_id=None, dataset_id=None,
                            max_charge_usd=.05, launch_allowed=True, save_run=AsyncMock(), reconcile_usage=AsyncMock())


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
        await adapter.search(the_task or task(), limit=10)
    await adapter.aclose()
    return json.loads(next(r for r in requests if r.method == "POST").content)


# The input of the live check run Ieo05WJB7ARinWhOB (2026-11-14), which returned rows from all over the province.
LIVE = {"maxItems": 10, "operation": "sale", "propertyType": "lands", "country": "es", "sortBy": "mostRecent",
        "location": "0-EU-ES-28", "maxPrice": "600000", "minSize": "300"}


async def test_flag_on_sends_madrid_province_as_its_location_id():
    assert await _input(province_location_id=True) == LIVE


async def test_flag_off_keeps_the_name():
    assert await _input() == LIVE | {"location": "Madrid"}
    assert await _input(province_location_id=False) == LIVE | {"location": "Madrid"}


@pytest.mark.parametrize(("name", "location"), [
    ("Comunidad de Madrid", "0-EU-ES-28"), ("comunidad de madrid", "0-EU-ES-28"),
    ("Alacant", "0-EU-ES-03"), ("Bizkaia", "0-EU-ES-48"), ("Vizcaya", "0-EU-ES-48"),
    ("Región de Murcia", "0-EU-ES-30"), ("Cantabria", "0-EU-ES-39"), ("La Rioja", "0-EU-ES-26"),
    ("Asturias", "0-EU-ES-33"), ("Navarra", "0-EU-ES-31"), ("València", "0-EU-ES-46"),
])
async def test_flag_on_names_and_one_province_communities(name, location):
    assert (await _input(task(location=name), province_location_id=True))["location"] == location


@pytest.mark.parametrize("name", ["Andalucía", "Cataluña", "Comunidad Valenciana", "Costa del Sol"])
async def test_flag_on_keeps_the_name_of_a_community_of_several_provinces(name):
    sent = await _input(task(location=name), province_location_id=True)
    assert sent == LIVE | {"location": name}


async def test_flag_on_leaves_a_city_task_as_it_was():
    city = task(place_level="city")
    assert await _input(city, province_location_id=True) == await _input(city) == LIVE | {"location": "Madrid"}
    # Q17/Q30: the city's circle still comes from the static list, the name stays the name
    sent = await _input(city, province_location_id=True, unambiguous_place=True, static_centers=True)
    assert sent["location"] == "Madrid" and "latitude" in sent and "distanceKm" in sent


async def test_flag_on_province_gets_no_city_circle():
    sent = await _input(province_location_id=True, unambiguous_place=True, static_centers=True,
                        district_search=True)
    assert sent == LIVE


def test_province_table():
    assert sorted(_PROVINCES) == [f"{n:02d}" for n in range(1, 53)]
    assert province_code("Araba") == province_code("Álava") == "01"
    assert province_code("Melilla") == "52" and province_code("Ceuta") == "51"
    for one, two, code in [("Alicante", "Alacant", "03"), ("Valencia", "València", "46"),
                           ("Illes Balears", "Baleares", "07"), ("Gipuzkoa", "Guipúzcoa", "20"),
                           ("A Coruña", "La Coruña", "15"), ("Ourense", "Orense", "32"), ("Lleida", "Lérida", "25"),
                           ("Girona", "Gerona", "17"), ("Las Palmas", "LAS PALMAS", "35"),
                           ("Santa Cruz de Tenerife", " santa cruz de tenerife ", "38")]:
        assert province_code(one) == province_code(two) == code, one
    for name in ("", "  ", "Andalucía", "Cataluña", "Comunitat Valenciana", "Atlantis"):
        assert province_location_id(name) is None, name


async def test_setting_reaches_the_source(monkeypatch):
    from bot.web_search.settings import WebSearchSettings

    for name in ("IDEALISTA_PROVINCE_LOCATION_ID", "APIFY_IDEALISTA_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    env = {"APIFY_TOKEN": "secret-token", "APIFY_IDEALISTA_ENABLED": True, "APIFY_IDEALISTA_PROVIDER": "igolaizola"}
    off, = WebSearchSettings(_env_file=None, **env).sources()
    on, = WebSearchSettings(_env_file=None, IDEALISTA_PROVINCE_LOCATION_ID=True, **env).sources()
    try:
        assert off.province_location_id is False and on.province_location_id is True
    finally:
        await off.aclose()
        await on.aclose()
