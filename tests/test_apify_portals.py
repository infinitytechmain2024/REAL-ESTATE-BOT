"""Portal adapters on ``ApifyListingSource`` against a mocked Apify API (no live calls in CI).

Fotocasa rows copy the shape of a live ``igolaizola/fotocasa-scraper`` run (5 plots in Madrid, 2026-10-13); phone
numbers and agency names are replaced.
"""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.web_search.queries import QueryTask
from bot.web_search.sources.apify import SourceRunContext, source_run_scope
from bot.web_search.sources.portals import ApifyFotocasaSource, plot_area


def task(**kwargs):
    base = QueryTask("terreno", "terreno", "Madrid", {}, "real_estate",
                     constraints={"deal": "sale", "min_area": "2000", "max_price": "3000000"}, country_code="ES")
    return replace(base, **kwargs)


def context(name, **kwargs):
    return SourceRunContext(campaign_id="campaign", source_name=name, run_id=kwargs.get("run_id"), dataset_id=None,
                            max_charge_usd=.05, launch_allowed=True, save_run=AsyncMock(), reconcile_usage=AsyncMock())


def fotocasa_row(**kwargs):
    row = {
        "id": "1_190995925", "propertyId": "190995925", "propertyType": "4", "propertySubtype": "58",
        "url": "https://www.fotocasa.es/es/comprar/terreno/madrid-capital/la-florida-el-plantio/190995925/d",
        "transaction": {"type": "SALE", "price": 1550000, "priceDrop": None},
        "surface": 2519, "rooms": None, "baths": None, "phone": "+34910000001", "street": "Calle Gobelas",
        "agency": {"id": "1", "name": "Agencia Uno S.L.", "type": "professional", "isBank": False},
        "location": {"level1Name": "Madrid", "level2Name": "Madrid", "level5Name": "Madrid",
                     "level7Name": "Moncloa - Aravaca", "level8Name": "La Florida -  El  Plantío",
                     "latitude": "40.473492246994276", "longitude": "-3.8252989462015687"},
        "description": "Parcela de 2.519 m² en La Florida–El Plantío. Edificabilidad de 741 m².",
        "publicationDate": "2026-10-01T18:08:14.633",
    }
    return {**row, **kwargs}


def api(rows, requests):
    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run1", "status": "SUCCEEDED",
                                                      "defaultDatasetId": "data1", "usageTotalUsd": .01}})
        if "actor-runs" in request.url.path:
            return httpx.Response(200, json={"data": {"usageTotalUsd": .01}})
        return httpx.Response(200, json=rows)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def fotocasa(rows, requests, **kwargs):
    return ApifyFotocasaSource("secret-token", client=api(rows, requests), sleep=AsyncMock(), **kwargs)


@pytest.mark.parametrize("change", [
    {"country_code": "IT"}, {"vertical": "investors"}, {"constraints": {"deal": "both"}}, {"location": " "},
    {"task_text": "habitación", "goal": "habitación"}, {"blocked_hosts": frozenset({"fotocasa.es"})},
    {"suburbs_only": True}, {"radius_km": 25.0},
])
def test_fotocasa_supports_spanish_sale_or_rent(change):
    adapter = fotocasa([], [])
    assert adapter.supports(task()) and adapter.supports(task(place_level="province"))
    assert not adapter.supports(task(**change))
    assert not ApifyFotocasaSource("").supports(task())
    assert "secret" not in repr(adapter)


async def test_fotocasa_imports_live_shaped_rows_with_facts_phone_and_plot():
    requests = []
    adapter = fotocasa([fotocasa_row(), fotocasa_row(url="https://www.fotocasa.es/es/comprar/terreno/x/191059959/d",
                                                     location={"level5Name": "Alcorcón", "level2Name": "Madrid"})],
                       requests)
    with source_run_scope(context("apify_fotocasa")):
        listings = await adapter.search(task(), limit=5)
    launch = next(r for r in requests if r.method == "POST")
    assert launch.url.path.endswith("/acts/igolaizola~fotocasa-scraper/runs")
    assert launch.url.params["maxTotalChargeUsd"] == "0.05" and launch.url.params["maxItems"] == "5"
    assert json.loads(launch.content) == {"maxItems": 5, "operation": "buy", "propertyType": "land",
                                          "sortBy": "latest", "location": "Madrid", "locationType": "municipality",
                                          "maxPrice": 3000000, "minSize": 2000}
    assert len(listings) == 1  # Alcorcón is another municipality
    listing = listings[0]
    assert listing.price == 1550000 and listing.currency == "EUR" and listing.deal == "sale"
    assert listing.plot_m2 == 2519 and listing.area_m2 is None and listing.property_type == "landparcel"
    assert listing.phone == "+34910000001" and listing.advertiser == "Agencia Uno S.L."
    assert listing.title == "Terreno en venta, La Florida -  El  Plantío"
    assert listing.address == "Calle Gobelas, La Florida -  El  Plantío, Madrid"
    await adapter.aclose()


@pytest.mark.parametrize("change", [
    {"transaction": {"type": "RENT", "price": 900}},
    {"url": "https://www.idealista.com/inmueble/123456/"},
    {"url": "https://www.fotocasa.es/es/comprar/terrenos/madrid-capital/l"},
    {"url": "javascript:alert(1)"},
])
def test_fotocasa_rejects_wrong_deal_foreign_host_and_search_pages(change):
    adapter = fotocasa([], [])
    assert adapter._normalize(fotocasa_row(**change), task(), None) is None
    assert adapter._normalize("not a row", task(), None) is None


def test_fotocasa_never_guesses_missing_facts():
    adapter = fotocasa([], [])
    listing = adapter._normalize(fotocasa_row(transaction={"type": "SALE", "price": None}, surface=None, phone="",
                                              agency=None, street=None), task(), None)
    assert listing.price is None and listing.currency is None and listing.plot_m2 is None
    assert listing.phone is None and listing.advertiser is None
    home = adapter._normalize(fotocasa_row(rooms=3, surface=95), task(task_text="piso", goal="piso"), None)
    assert home.area_m2 == 95 and home.plot_m2 is None and home.rooms == 3 and home.property_type is None


def test_plot_area_is_dropped_when_the_description_names_another_plot():
    assert plot_area(2519, "Parcela de 2.519 m²") == 2519
    assert plot_area(7714, "terreno ... en parcela de 2500m2") is None
    assert plot_area(1089, "Suelo disponible, edificabilidad total de 6.100 m2") == 1089
    assert plot_area(None, "Parcela de 2.519 m²") is None


async def test_fotocasa_explicit_radius_searches_around_the_geocoded_centre():
    requests, geocoder = [], AsyncMock()
    geocoder.resolve.return_value = (40.4168, -3.7038)
    near = fotocasa_row(location={"level5Name": "Pozuelo", "level2Name": "Madrid",
                                  "latitude": "40.43", "longitude": "-3.81"})
    far = fotocasa_row(url="https://www.fotocasa.es/es/comprar/terreno/toledo/191000001/d",
                       location={"level5Name": "Toledo", "latitude": "39.86", "longitude": "-4.02"})
    adapter = fotocasa([near, far], requests, geocoder=geocoder)
    radius = task(radius_km=25.0)
    assert adapter.supports(radius)
    with source_run_scope(context("apify_fotocasa")):
        listings = await adapter.search(radius, limit=5)
    body = json.loads(next(r for r in requests if r.method == "POST").content)
    assert body["latitude"] == 40.4168 and body["distanceKm"] == 25 and "location" not in body
    assert [x.url for x in listings] == [near["url"]]
    await adapter.aclose()


async def test_settings_build_only_the_enabled_portals(monkeypatch):
    from bot.web_search.settings import WebSearchSettings

    for name in ("APIFY_IDEALISTA_ENABLED", "APIFY_FOTOCASA_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    assert WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token").sources() == ()
    assert WebSearchSettings(_env_file=None, APIFY_FOTOCASA_ENABLED=True).sources() == ()  # no token
    settings = WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token", APIFY_FOTOCASA_ENABLED=True,
                                 APIFY_FOTOCASA_MAX_RESULTS=7, APIFY_FOTOCASA_MAX_CHARGE_USD=.03,
                                 APIFY_PORTALS_TIMEOUT_SECONDS=90)
    source, = settings.sources()
    try:
        assert isinstance(source, ApifyFotocasaSource) and source.actor_id == "igolaizola~fotocasa-scraper"
        assert source.max_items == 7 and source.max_charge_usd == .03 and source.timeout_seconds == 90
    finally:
        await source.aclose()
