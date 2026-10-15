"""Portal adapters on ``ApifyListingSource`` against a mocked Apify API (no live calls in CI).

Fotocasa rows copy the shape of a live ``igolaizola/fotocasa-scraper`` run (5 plots in Madrid, 2026-10-13); phone
numbers and agency names are replaced. Yaencontre and Idealista rows follow the output schemas the Apify Store infers
for ``igolaizola/yaencontre-scraper`` and ``igolaizola/idealista-scraper`` (no run of ours).
"""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.web_search.queries import QueryTask
from bot.web_search.sources.apify import SourceRunContext, source_run_scope
from bot.web_search.sources.portals import (
    ApifyFotocasaSource,
    ApifyHabitacliaSource,
    ApifyIdealistaPortalSource,
    ApifyPisosSource,
    ApifyYaencontreSource,
    plot_area,
)


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

    for name in ("APIFY_IDEALISTA_ENABLED", "APIFY_FOTOCASA_ENABLED", "APIFY_YAENCONTRE_ENABLED"):
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
    both = WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token", APIFY_FOTOCASA_ENABLED=True,
                             APIFY_YAENCONTRE_ENABLED=True).sources()
    try:
        assert [s.name for s in both] == ["apify_fotocasa", "apify_yaencontre"]
        assert both[1].actor_id == "igolaizola~yaencontre-scraper"
    finally:
        for source in both:
            await source.aclose()


def yaencontre_row(**kwargs):
    row = {"id": "41123456", "url": "https://www.yaencontre.com/venta/terreno/inmueble-41123456",
           "title": "Terreno en venta en Aravaca", "price": 1200000, "area": 2400, "rooms": 0,
           "operation": "sale", "family": "land", "description": "Parcela urbana de 2.400 m² en Aravaca.",
           "address": {"qualifiedName": "Aravaca, Madrid", "geoLocation": {"lat": 40.46, "lon": -3.78}},
           "owner": {"name": "Agencia Dos", "phoneAvailable": True, "virtualPhoneNumber": "910000002"}}
    return {**row, **kwargs}


def yaencontre(rows, requests, **kwargs):
    return ApifyYaencontreSource("secret-token", client=api(rows, requests), sleep=AsyncMock(), **kwargs)


async def test_yaencontre_launches_with_safe_filter_bounds_and_maps_rows():
    requests = []
    adapter = yaencontre([yaencontre_row(), yaencontre_row(address={"qualifiedName": "Getafe"})], requests)
    with source_run_scope(context("apify_yaencontre")):
        listings = await adapter.search(task(), limit=5)
    launch = next(r for r in requests if r.method == "POST")
    assert launch.url.path.endswith("/acts/igolaizola~yaencontre-scraper/runs")
    # min_area 2000 -> 1000 and max_price 3000000 -> 3000000: bounds never drop a matching listing
    assert json.loads(launch.content) == {"maxItems": 5, "operation": "sale", "propertyType": "land",
                                          "sortBy": "newest", "location": "Madrid", "locationType": "municipality",
                                          "maxPrice": "3000000", "minArea": "1000"}
    assert len(listings) == 1  # Getafe is not Madrid
    listing = listings[0]
    assert listing.plot_m2 == 2400 and listing.price == 1200000 and listing.currency == "EUR"
    assert listing.phone == "910000002" and listing.advertiser == "Agencia Dos"
    assert listing.title == "Terreno en venta en Aravaca" and listing.property_type == "landparcel"
    await adapter.aclose()


@pytest.mark.parametrize("change", [
    {"operation": "rent"}, {"url": "https://www.yaencontre.com/venta/terrenos/madrid"},
    {"url": "https://www.fotocasa.es/es/comprar/terreno/x/191059959/d"},
])
def test_yaencontre_rejects_wrong_deal_and_non_listing_urls(change):
    assert yaencontre([], [])._normalize(yaencontre_row(**change), task(), None) is None


def test_yaencontre_relative_url_hidden_phone_and_unknown_operation():
    adapter = yaencontre([], [])
    listing = adapter._normalize(yaencontre_row(url="/venta/terreno/inmueble-41123456", operation=None,
                                                owner={"name": "Particular", "phoneAvailable": False,
                                                       "virtualPhoneNumber": "910000003"}), task(), None)
    assert listing.url == "https://www.yaencontre.com/venta/terreno/inmueble-41123456"
    assert listing.phone is None and listing.advertiser == "Particular"


def idealista_row(**kwargs):
    row = {"propertyCode": "109876543", "url": "https://www.idealista.com/inmueble/109876543/",
           "operation": "sale", "propertyType": "land", "country": "es", "municipality": "Madrid",
           "province": "Madrid", "address": "Aravaca", "price": 2100000.0, "size": 2600.0,
           "latitude": 40.46, "longitude": -3.78, "description": "Parcela de 2.600 m² edificable.",
           "contactInfo": {"commercialName": "Agencia Tres", "userType": "professional",
                           "phone1": {"phoneNumberForMobileDialing": "+34910000004", "formattedPhone": "910 000 004"}},
           "suggestedTexts": {"title": "Terreno en Aravaca"}}
    return {**row, **kwargs}


async def test_idealista_igolaizola_searches_any_city_and_maps_rows():
    requests = []
    rows = [idealista_row(), idealista_row(url="https://www.idealista.com/inmueble/109876544/", municipality="Getafe")]
    adapter = ApifyIdealistaPortalSource("secret-token", client=api(rows, requests), sleep=AsyncMock())
    assert adapter.supports(task(location="Valencia")) and adapter.name == "apify_idealista"
    with source_run_scope(context("apify_idealista")):
        listings = await adapter.search(task(), limit=5)
    launch = next(r for r in requests if r.method == "POST")
    assert launch.url.path.endswith("/acts/igolaizola~idealista-scraper/runs")
    assert json.loads(launch.content) == {"maxItems": 5, "operation": "sale", "propertyType": "lands", "country": "es",
                                          "sortBy": "mostRecent", "location": "Madrid", "maxPrice": "3000000",
                                          "minSize": "300"}
    assert len(listings) == 1
    listing = listings[0]
    assert listing.plot_m2 == 2600 and listing.price == 2100000 and listing.title == "Terreno en Aravaca"
    assert listing.phone == "+34910000004" and listing.advertiser == "Agencia Tres"
    await adapter.aclose()


@pytest.mark.parametrize("change", [
    {"operation": "rent"}, {"country": "it"}, {"propertyType": "flat"},
    {"url": "https://www.idealista.com/venta-terrenos/madrid-madrid/"},
])
def test_idealista_igolaizola_rejects_foreign_rows(change):
    adapter = ApifyIdealistaPortalSource("secret-token")
    assert adapter._normalize(idealista_row(**change), task(), None) is None


async def test_idealista_provider_setting_picks_the_actor_family(monkeypatch):
    from bot.web_search.settings import WebSearchSettings

    monkeypatch.delenv("APIFY_IDEALISTA_PROVIDER", raising=False)
    env = {"APIFY_TOKEN": "secret-token", "APIFY_IDEALISTA_ENABLED": True, "APIFY_IDEALISTA_MAX_RESULTS": 9}
    old, = WebSearchSettings(_env_file=None, **env).sources()
    new, = WebSearchSettings(_env_file=None, APIFY_IDEALISTA_PROVIDER="igolaizola", **env).sources()
    try:
        assert type(old).__name__ == "ApifyIdealistaSource" and old.actor_id == "axlymxp~idealista-scraper"
        assert isinstance(new, ApifyIdealistaPortalSource) and new.actor_id == "igolaizola~idealista-scraper"
        assert new.max_items == 9
    finally:
        await old.aclose()
        await new.aclose()
    with pytest.raises(ValueError):
        WebSearchSettings(_env_file=None, APIFY_IDEALISTA_PROVIDER="other")


# --- Pisos.com (B5): rows follow assumed field names, no live run yet --------------------------------------------


def pisos_row(**kwargs):
    row = {
        "url": "https://www.pisos.com/comprar/terreno-madrid_capital-45678901_100500/", "operation": "venta", "title": "Parcela urbana en Pisos.com", "price": 250000,
        "currency": "EUR", "surface": 3200, "rooms": None, "municipality": "Madrid", "province": "Madrid",
        "neighbourhood": "Barajas", "street": "Calle Ejemplo", "phone": "+34910000002",
        "agency": {"name": "Inmobiliaria Ejemplo S.L."},
        "description": "Parcela de 3.200 m2 de suelo urbano.",
    }
    return {**row, **kwargs}


def pisos(rows, requests, **kwargs):
    return ApifyPisosSource("secret-token", client=api(rows, requests), sleep=AsyncMock(), **kwargs)


@pytest.mark.parametrize("change", [
    {"country_code": "IT"}, {"vertical": "investors"}, {"constraints": {"deal": "both"}}, {"location": " "},
    {"task_text": "habitación", "goal": "habitación"}, {"blocked_hosts": frozenset({"pisos.com"})},
    {"suburbs_only": True}, {"radius_km": 25.0},
])
def test_pisos_supports_spanish_sale_or_rent_without_radius(change):
    adapter = pisos([], [])
    assert adapter.supports(task()) and adapter.supports(task(place_level="province"))
    assert not adapter.supports(task(**change))
    assert not ApifyPisosSource("").supports(task())
    assert "secret" not in repr(adapter)


def test_pisos_url_classification_matches_the_portal_regexes():
    from bot.web_search.urls import classify_url

    assert classify_url("https://www.pisos.com/comprar/terreno-madrid_capital-45678901_100500/") == "listing"
    assert classify_url("https://www.pisos.com/alquilar/piso-madrid_capital-45678902_100500/") == "listing"
    assert classify_url("https://www.pisos.com/venta/terrenos-madrid/") != "listing"


async def test_pisos_launch_body_is_capped_pinned_and_rows_are_mapped():
    requests = []
    adapter = pisos([pisos_row(), pisos_row(municipality="Alcorcón")], requests)
    with source_run_scope(context("apify_pisos")):
        listings = await adapter.search(task(), limit=5)
    launch = next(r for r in requests if r.method == "POST")
    assert launch.url.path.endswith("/acts/logiover~pisos-com-property-scraper/runs")
    assert launch.url.params["maxTotalChargeUsd"] == "0.05" and launch.url.params["maxItems"] == "5"
    assert json.loads(launch.content) == {"maxItems": 5, "operation": "sale", "propertyType": "land",
                                          "location": "Madrid", "locationType": "municipality",
                                          "minSize": 2000, "maxPrice": 3000000}
    assert len(listings) == 1  # Alcorcón is another municipality
    listing = listings[0]
    assert listing.url == "https://www.pisos.com/comprar/terreno-madrid_capital-45678901_100500/" and listing.title == "Parcela urbana en Pisos.com"
    assert listing.price == 250000 and listing.currency == "EUR" and listing.deal == "sale"
    assert listing.plot_m2 == 3200 and listing.area_m2 is None and listing.property_type == "landparcel"
    assert listing.phone == "+34910000002" and listing.advertiser == "Inmobiliaria Ejemplo S.L."
    assert listing.address == "Calle Ejemplo, Barajas, Madrid"
    await adapter.aclose()


def test_pisos_house_area_is_built_area_not_plot():
    adapter = pisos([], [])
    house = task(task_text="casa", goal="casa", constraints={"deal": "sale"})
    listing = adapter._normalize(pisos_row(surface=140, rooms=3), house, None)
    assert listing.area_m2 == 140 and listing.plot_m2 is None and listing.rooms == 3
    assert listing.property_type == "house"


@pytest.mark.parametrize("change", [
    {"operation": "alquiler"}, {"operation": "rent"}, {"operation": "unknown"},
    {"url": "https://www.pisos.com/alquilar/piso-madrid_capital-45678902_100500/", "operation": None},
    {"url": "https://www.idealista.com/inmueble/123456/"}, {"url": "https://www.pisos.com/venta/terrenos-madrid/"},
    {"url": "/not-a-listing"}, {"url": "javascript:alert(1)"},
    {"municipality": "Alcorcón"}, {"municipality": None, "city": None}, {"country": "FR"},
])
def test_pisos_rejects_foreign_deal_place_and_non_listing_rows(change):
    adapter = pisos([], [])
    assert adapter._normalize(pisos_row(**change), task(), None) is None
    assert adapter._normalize("not a row", task(), None) is None


def test_pisos_never_guesses_missing_facts():
    adapter = pisos([], [])
    listing = adapter._normalize(pisos_row(price=None, surface=None, phone="", agency=None, street=None,
                                         neighbourhood=None), task(), None)
    assert listing.price is None and listing.currency is None and listing.plot_m2 is None
    assert listing.phone is None and listing.advertiser is None and listing.rooms is None
    # a price text, a price in another currency and a price without a number are not prices
    for price, currency in (("250.000 €", None), (250000, "USD"), (0, "EUR"), ("consultar", None)):
        row = pisos_row(price=price, currency=currency)
        assert adapter._normalize(row, task(), None).price is None
    # a surface that the description contradicts is not a plot
    other = pisos_row(description="Parcela de 900 m2 de suelo.")
    assert adapter._normalize(other, task(), None).plot_m2 is None


def test_pisos_reads_field_synonyms_and_an_explicit_plot_area():
    adapter = pisos([], [])
    row = {"link": "https://www.pisos.com/comprar/terreno-madrid_capital-45678901_100500/", "transactionType": "SALE", "price": {"amount": 99000}, "plotArea": 1500,
           "surface": 80, "city": "Madrid", "phones": ["+34910000003"], "advertiser": "Particular", "text": "Solar."}
    listing = adapter._normalize(row, task(), None)
    assert listing.plot_m2 == 1500 and listing.area_m2 is None and listing.phone == "+34910000003"
    assert listing.advertiser == "Particular" and listing.description == "Solar."
    assert listing.price == 99000 and listing.currency == "EUR"


async def test_pisos_setting_builds_the_source_only_when_enabled(monkeypatch):
    from bot.web_search.settings import WebSearchSettings

    monkeypatch.delenv("APIFY_PISOS_ENABLED", raising=False)
    assert WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token").apify_pisos_enabled is False
    assert WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token").sources() == ()
    assert WebSearchSettings(_env_file=None, APIFY_PISOS_ENABLED=True).sources() == ()  # no token
    settings = WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token", APIFY_PISOS_ENABLED=True,
                                 APIFY_PISOS_MAX_RESULTS=7, APIFY_PISOS_MAX_CHARGE_USD=.03)
    source, = settings.sources()
    try:
        assert isinstance(source, ApifyPisosSource) and source.actor_id == "logiover~pisos-com-property-scraper"
        assert source.max_items == 7 and source.max_charge_usd == .03
    finally:
        await source.aclose()
    with pytest.raises(ValueError):
        WebSearchSettings(_env_file=None, APIFY_PISOS_MAX_RESULTS=51)


# --- Habitaclia (B5): rows follow assumed field names, no live run yet --------------------------------------------


def habitaclia_row(**kwargs):
    row = {
        "url": "https://www.habitaclia.com/comprar-terreno-sant_cugat_del_valles-i500003456789.htm", "operation": "venta", "title": "Parcela urbana en Habitaclia", "price": 250000,
        "currency": "EUR", "surface": 3200, "rooms": None, "municipality": "Madrid", "province": "Madrid",
        "neighbourhood": "Barajas", "street": "Calle Ejemplo", "phone": "+34910000002",
        "agency": {"name": "Inmobiliaria Ejemplo S.L."},
        "description": "Parcela de 3.200 m2 de suelo urbano.",
    }
    return {**row, **kwargs}


def habitaclia(rows, requests, **kwargs):
    return ApifyHabitacliaSource("secret-token", client=api(rows, requests), sleep=AsyncMock(), **kwargs)


@pytest.mark.parametrize("change", [
    {"country_code": "IT"}, {"vertical": "investors"}, {"constraints": {"deal": "both"}}, {"location": " "},
    {"task_text": "habitación", "goal": "habitación"}, {"blocked_hosts": frozenset({"habitaclia.com"})},
    {"suburbs_only": True}, {"radius_km": 25.0},
])
def test_habitaclia_supports_spanish_sale_or_rent_without_radius(change):
    adapter = habitaclia([], [])
    assert adapter.supports(task()) and adapter.supports(task(place_level="province"))
    assert not adapter.supports(task(**change))
    assert not ApifyHabitacliaSource("").supports(task())
    assert "secret" not in repr(adapter)


def test_habitaclia_url_classification_matches_the_portal_regexes():
    from bot.web_search.urls import classify_url

    assert classify_url("https://www.habitaclia.com/comprar-terreno-sant_cugat_del_valles-i500003456789.htm") == "listing"
    assert classify_url("https://www.habitaclia.com/alquiler-piso-sant_cugat_del_valles-i500003456790.htm") == "listing"
    assert classify_url("https://www.habitaclia.com/terrenos-sant_cugat_del_valles.htm") != "listing"


async def test_habitaclia_launch_body_is_capped_pinned_and_rows_are_mapped():
    requests = []
    adapter = habitaclia([habitaclia_row(), habitaclia_row(municipality="Alcorcón")], requests)
    with source_run_scope(context("apify_habitaclia")):
        listings = await adapter.search(task(), limit=5)
    launch = next(r for r in requests if r.method == "POST")
    assert launch.url.path.endswith("/acts/trev0n~habitaclia-com-spain-scraper/runs")
    assert launch.url.params["maxTotalChargeUsd"] == "0.05" and launch.url.params["maxItems"] == "5"
    assert json.loads(launch.content) == {"maxItems": 5, "operation": "sale", "propertyType": "land",
                                          "location": "Madrid", "locationType": "municipality",
                                          "minSize": 2000, "maxPrice": 3000000}
    assert len(listings) == 1  # Alcorcón is another municipality
    listing = listings[0]
    assert listing.url == "https://www.habitaclia.com/comprar-terreno-sant_cugat_del_valles-i500003456789.htm" and listing.title == "Parcela urbana en Habitaclia"
    assert listing.price == 250000 and listing.currency == "EUR" and listing.deal == "sale"
    assert listing.plot_m2 == 3200 and listing.area_m2 is None and listing.property_type == "landparcel"
    assert listing.phone == "+34910000002" and listing.advertiser == "Inmobiliaria Ejemplo S.L."
    assert listing.address == "Calle Ejemplo, Barajas, Madrid"
    await adapter.aclose()


def test_habitaclia_house_area_is_built_area_not_plot():
    adapter = habitaclia([], [])
    house = task(task_text="casa", goal="casa", constraints={"deal": "sale"})
    listing = adapter._normalize(habitaclia_row(surface=140, rooms=3), house, None)
    assert listing.area_m2 == 140 and listing.plot_m2 is None and listing.rooms == 3
    assert listing.property_type == "house"


@pytest.mark.parametrize("change", [
    {"operation": "alquiler"}, {"operation": "rent"}, {"operation": "unknown"},
    {"url": "https://www.habitaclia.com/alquiler-piso-sant_cugat_del_valles-i500003456790.htm", "operation": None},
    {"url": "https://www.idealista.com/inmueble/123456/"}, {"url": "https://www.habitaclia.com/terrenos-sant_cugat_del_valles.htm"},
    {"url": "/not-a-listing"}, {"url": "javascript:alert(1)"},
    {"municipality": "Alcorcón"}, {"municipality": None, "city": None}, {"country": "FR"},
])
def test_habitaclia_rejects_foreign_deal_place_and_non_listing_rows(change):
    adapter = habitaclia([], [])
    assert adapter._normalize(habitaclia_row(**change), task(), None) is None
    assert adapter._normalize("not a row", task(), None) is None


def test_habitaclia_never_guesses_missing_facts():
    adapter = habitaclia([], [])
    listing = adapter._normalize(habitaclia_row(price=None, surface=None, phone="", agency=None, street=None,
                                         neighbourhood=None), task(), None)
    assert listing.price is None and listing.currency is None and listing.plot_m2 is None
    assert listing.phone is None and listing.advertiser is None and listing.rooms is None
    # a price text, a price in another currency and a price without a number are not prices
    for price, currency in (("250.000 €", None), (250000, "USD"), (0, "EUR"), ("consultar", None)):
        row = habitaclia_row(price=price, currency=currency)
        assert adapter._normalize(row, task(), None).price is None
    # a surface that the description contradicts is not a plot
    other = habitaclia_row(description="Parcela de 900 m2 de suelo.")
    assert adapter._normalize(other, task(), None).plot_m2 is None


def test_habitaclia_reads_field_synonyms_and_an_explicit_plot_area():
    adapter = habitaclia([], [])
    row = {"link": "https://www.habitaclia.com/comprar-terreno-sant_cugat_del_valles-i500003456789.htm", "transactionType": "SALE", "price": {"amount": 99000}, "plotArea": 1500,
           "surface": 80, "city": "Madrid", "phones": ["+34910000003"], "advertiser": "Particular", "text": "Solar."}
    listing = adapter._normalize(row, task(), None)
    assert listing.plot_m2 == 1500 and listing.area_m2 is None and listing.phone == "+34910000003"
    assert listing.advertiser == "Particular" and listing.description == "Solar."
    assert listing.price == 99000 and listing.currency == "EUR"


async def test_habitaclia_setting_builds_the_source_only_when_enabled(monkeypatch):
    from bot.web_search.settings import WebSearchSettings

    monkeypatch.delenv("APIFY_HABITACLIA_ENABLED", raising=False)
    assert WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token").apify_habitaclia_enabled is False
    assert WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token").sources() == ()
    assert WebSearchSettings(_env_file=None, APIFY_HABITACLIA_ENABLED=True).sources() == ()  # no token
    settings = WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token", APIFY_HABITACLIA_ENABLED=True,
                                 APIFY_HABITACLIA_MAX_RESULTS=7, APIFY_HABITACLIA_MAX_CHARGE_USD=.03)
    source, = settings.sources()
    try:
        assert isinstance(source, ApifyHabitacliaSource) and source.actor_id == "trev0n~habitaclia-com-spain-scraper"
        assert source.max_items == 7 and source.max_charge_usd == .03
    finally:
        await source.aclose()
    with pytest.raises(ValueError):
        WebSearchSettings(_env_file=None, APIFY_HABITACLIA_MAX_RESULTS=51)
