"""Portal adapters on ``ApifyListingSource`` against a mocked Apify API (no live calls in CI).

Fotocasa rows copy the shape of a live ``igolaizola/fotocasa-scraper`` run (5 plots in Madrid, 2026-10-13); phone
numbers and agency names are replaced. Yaencontre and Idealista rows follow the output schemas the Apify Store infers
for ``igolaizola/yaencontre-scraper`` and ``igolaizola/idealista-scraper`` (no run of ours).
"""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.web_search.queries import QueryTask
from bot.web_search.sources.apify import SourceRunContext, source_run_scope
from bot.web_search.sources.portals import (
    ApifyFotocasaSource,
    ApifyHabitacliaSource,
    ApifyIdealistaPortalSource,
    ApifyMilanunciosSource,
    ApifyPisosSource,
    ApifyYaencontreSource,
    plot_area,
)
from bot.web_search.urls import classify_url

LIVE = Path(__file__).parent / "fixtures" / "portals_live"


def live_rows(name):
    """Rows of the live runs of 2026-10-16 (task: plots for sale in Madrid, 5 items); phones are replaced."""
    return json.loads((LIVE / f"{name}_items.json").read_text(encoding="utf-8"))


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
                                              agency=None, street=None, description="Parcela urbana."), task(), None)
    assert listing.price is None and listing.currency is None and listing.plot_m2 is None
    assert listing.phone is None and listing.advertiser is None
    home = adapter._normalize(fotocasa_row(rooms=3, surface=95), task(task_text="piso", goal="piso"), None)
    assert home.area_m2 == 95 and home.plot_m2 is None and home.rooms == 3 and home.property_type is None


def test_plot_area_is_dropped_when_the_description_names_another_plot():
    assert plot_area(2519, "Parcela de 2.519 m²") == 2519
    assert plot_area(7714, "terreno ... en parcela de 2500m2") is None
    assert plot_area(1089, "Suelo disponible, edificabilidad total de 6.100 m2") == 1089
    assert plot_area(None, "Parcela de 2.519 m²") == 2519  # no portal area: the text names exactly one
    assert plot_area(None, "Parcela de 232 m² con un edificio de 1280 m2 construidos") == 232
    assert plot_area(None, "este solar cuenta con una superficie de 4.140 m² y una edificabilidad de 3.519 m²t") == 4140
    assert plot_area(None, "Terreno de 500 m2, o bien dos lotes de 300 m2 y 900 m2") is None  # ambiguous: no guess
    assert plot_area(None, "Garsierra. En una de las zonas más exclusivas...") is None
    assert plot_area(None, "") is None
    for text in ("Solar. Edificabilidad 1.200 m2", "Terreno urbano con edificabilidad de 3.519 m²t",
                 "Terreno con nave de 800 m2"):
        assert plot_area(None, text) is None, text  # buildable or built area is not the plot
    assert plot_area(None, "parcela en urbanización con una superficie gráfica de 2.519 m2") == 2519


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


def test_idealista_live_rows_plot_is_size_and_phone_and_agency_come_from_the_portal():
    adapter = ApifyIdealistaPortalSource("secret-token")
    rows = live_rows("idealista")
    assert len(rows) == 3 and all("description" not in row for row in rows)
    listings = [adapter._normalize(row, task(), None) for row in rows]
    assert all(listings)
    first = listings[0]
    assert first.url == "https://www.idealista.com/inmueble/112824748/"
    assert first.plot_m2 == 67 and first.area_m2 is None  # a plot: size is the plot, never the house
    assert first.price == 339000 and first.currency == "EUR"
    assert first.phone == "+34900000001" and first.advertiser == "SELHOM"
    assert first.address == "Terreno en Calle de las Islas Palaos, Tres Olivos - Valverde, Madrid"
    assert [x.advertiser for x in listings][1] == "VIVEI Real Estate Urgel"
    assert all(x.phone and x.phone.startswith("+349000000") and x.deal == "sale" for x in listings)


def test_idealista_price_falls_back_to_the_euro_price_block_only():
    adapter = ApifyIdealistaPortalSource("secret-token")
    row = live_rows("idealista")[0]
    block = {"price": {"amount": 339000, "currencySuffix": "€"}}
    assert adapter._normalize({**row, "price": None, "priceInfo": block}, task(), None).price == 339000
    other = {"price": {"amount": 339000, "currencySuffix": "£"}}
    assert adapter._normalize({**row, "price": None, "priceInfo": other}, task(), None).price is None
    assert adapter._normalize({**row, "price": None, "priceInfo": None}, task(), None).price is None
    phone = {"phone1": {"phoneNumber": "900000001"}, "commercialName": None, "contactName": "Selhom"}
    listing = adapter._normalize({**row, "contactInfo": phone}, task(), None)
    assert listing.phone == "900000001" and listing.advertiser == "Selhom"
    assert adapter._normalize({**row, "contactInfo": None, "size": None}, task(), None).plot_m2 is None


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


# --- Pisos.com, Habitaclia, Milanuncios (B5): published schemas, synthetic values, no live run yet -----------------

PISOS_URL = "https://www.pisos.com/comprar/terreno-madrid_capital-45678901_100500/"
HAB_URL = "https://www.habitaclia.com/comprar-terreno-madrid-i500003456789.htm"
MIL_URL = "https://www.milanuncios.com/venta-de-terrenos-en-madrid/parcela-urbana-123456789.htm"


def pisos_row(**kwargs):
    row = {"detailUrl": PISOS_URL, "title": "Parcela urbana en Madrid", "price": 250000, "priceCurrency": "EUR",
           "areaSqm": 3200, "usableAreaSqm": None, "terrainAreaSqm": None, "rooms": None, "city": "Madrid",
           "province": "Madrid", "fullAddress": "Calle Ejemplo, Barajas, Madrid", "advertiserName": "Inmobiliaria Ejemplo",
           "transactionType": "sale", "shortDescription": "Parcela de 3.200 m2 de suelo urbano."}
    return {**row, **kwargs}


def habitaclia_row(**kwargs):
    row = {"detailUrl": HAB_URL, "title": "Terreno en Madrid", "price": 250000, "priceCurrency": "EUR", "areaSqm": 3200,
           "rooms": None, "city": "Madrid", "province": "Madrid", "location": "Barajas, Madrid",
           "advertiserName": "Inmobiliaria Ejemplo", "transactionType": "sale", "description": None,
           "shortDescription": "Parcela de 3.200 m2."}
    return {**row, **kwargs}


def milanuncios_row(**kwargs):
    row = {"url": MIL_URL, "title": "Parcela urbana", "price": 250000, "description": "Parcela de 3.200 m2 urbana.",
           "cityName": "Madrid", "provinceName": "Madrid", "categorySlug": "venta-de-terrenos", "sellerType": "private",
           "tag_sqm": 3200, "tag_bedrooms": None}
    return {**row, **kwargs}


CASES = {
    "pisos": (ApifyPisosSource, pisos_row, "logiover~pisos-com-property-scraper", "pisos.com", PISOS_URL),
    "habitaclia": (ApifyHabitacliaSource, habitaclia_row, "trev0n~habitaclia-com-spain-scraper", "habitaclia.com",
                   HAB_URL),
    "milanuncios": (ApifyMilanunciosSource, milanuncios_row, "rastriq~milanuncios-scraper", "milanuncios.com", MIL_URL),
}
ALL = pytest.mark.parametrize("key", CASES)


def build(key, rows, requests):
    return CASES[key][0]("secret-token", client=api(rows, requests), sleep=AsyncMock())


@ALL
@pytest.mark.parametrize("change", [
    {"country_code": "IT"}, {"vertical": "investors"}, {"constraints": {"deal": "both"}}, {"location": " "},
    {"task_text": "habitación", "goal": "habitación"}, {"suburbs_only": True}, {"radius_km": 25.0},
])
def test_portal_supports_spanish_sale_or_rent_without_radius(key, change):
    adapter = build(key, [], [])
    assert adapter.supports(task()) and adapter.supports(task(place_level="province"))
    assert not adapter.supports(task(**change))
    assert not adapter.supports(task(blocked_hosts=frozenset({CASES[key][3]})))
    assert not CASES[key][0]("").supports(task())
    assert "secret" not in repr(adapter)


def test_pisos_serves_only_places_it_has_a_slug_for():
    adapter = build("pisos", [], [])
    assert not adapter.supports(task(location="Getafe"))
    assert adapter.supports(task(location="Málaga")) and adapter.supports(task(location="Getafe", place_level="province")) is False


def test_milanuncios_serves_only_known_categories():
    adapter = build("milanuncios", [], [])
    rent_land = task(constraints={"deal": "rent"})
    assert not adapter.supports(rent_land)
    assert adapter.supports(task(task_text="piso", goal="piso", constraints={"deal": "rent"}))
    assert not adapter.supports(task(task_text="local comercial", goal="local comercial", constraints={"deal": "rent"}))


@ALL
def test_portal_urls_are_listings_and_search_pages_are_not(key):
    from bot.web_search.urls import classify_url

    assert classify_url(CASES[key][4]) == "listing"


async def test_pisos_launch_body_is_capped_pinned_and_row_mapped():
    requests = []
    adapter = build("pisos", [pisos_row(), pisos_row(city="Alcorcón")], requests)
    with source_run_scope(context("apify_pisos")):
        listings = await adapter.search(task(), limit=5)
    launch = next(r for r in requests if r.method == "POST")
    assert launch.url.path.endswith("/acts/logiover~pisos-com-property-scraper/runs")
    assert launch.url.params["maxTotalChargeUsd"] == "0.05" and launch.url.params["maxItems"] == "5"
    assert json.loads(launch.content) == {
        "transactionType": "sale", "propertyType": "land", "location": "madrid_capital", "sortBy": "newest",
        "maxListings": 5, "maxPages": 1, "fetchDetails": False, "maxPrice": 3000000, "minArea": 2000}
    assert len(listings) == 1  # Alcorcón is another municipality
    one = listings[0]
    assert one.url == PISOS_URL and one.price == 250000 and one.currency == "EUR" and one.deal == "sale"
    assert one.plot_m2 == 3200 and one.area_m2 is None and one.property_type == "landparcel"
    assert one.phone is None and one.advertiser == "Inmobiliaria Ejemplo"
    assert one.address == "Calle Ejemplo, Barajas, Madrid" and one.description.startswith("Parcela de 3.200")
    await adapter.aclose()


async def test_pisos_province_slug_and_house_area():
    adapter = build("pisos", [], [])
    _, body, _ = await adapter._plan(task(place_level="province", task_text="casa", goal="casa"), 5, None)
    assert body["location"] == "madrid" and body["propertyType"] == "house"
    house = task(task_text="casa", goal="casa", constraints={"deal": "sale"})
    listing = adapter._normalize(pisos_row(areaSqm=140, rooms=3, terrainAreaSqm=900), house, None)
    assert listing.area_m2 == 140 and listing.plot_m2 is None and listing.rooms == 3


async def test_habitaclia_launch_body_is_capped_pinned_and_row_mapped():
    requests = []
    adapter = build("habitaclia", [habitaclia_row()], requests)
    with source_run_scope(context("apify_habitaclia")):
        listings = await adapter.search(task(), limit=5)
    launch = next(r for r in requests if r.method == "POST")
    assert launch.url.path.endswith("/acts/trev0n~habitaclia-com-spain-scraper/runs")
    assert launch.url.params["maxTotalChargeUsd"] == "0.05" and launch.url.params["maxItems"] == "5"
    assert json.loads(launch.content) == {
        "transactionType": "sale", "propertyType": "land", "location": "Madrid", "sort": "newest", "maxListings": 5,
        "maxPages": 1, "fetchPropertyDetails": False, "maxPrice": 3000000, "minArea": 2000}
    one, = listings
    assert one.price == 250000 and one.plot_m2 == 3200 and one.phone is None and one.advertiser == "Inmobiliaria Ejemplo"
    assert one.address == "Barajas, Madrid" and one.description == "Parcela de 3.200 m2."  # shortDescription fallback
    await adapter.aclose()


async def test_milanuncios_launch_body_strips_seller_pii_and_row_is_mapped():
    requests = []
    adapter = build("milanuncios", [milanuncios_row()], requests)
    with source_run_scope(context("apify_milanuncios")):
        listings = await adapter.search(task(location="Las Palmas"), limit=5)
    assert listings == []  # the row is Madrid, not Las Palmas
    launch = next(r for r in requests if r.method == "POST")
    assert launch.url.path.endswith("/acts/rastriq~milanuncios-scraper/runs")
    assert launch.url.params["maxTotalChargeUsd"] == "0.05" and launch.url.params["maxItems"] == "5"
    assert json.loads(launch.content) == {
        "searchUrl": "https://www.milanuncios.com/venta-de-terrenos-en-las_palmas/", "maxItems": 5, "maxPages": 1,
        "fetchDetails": False, "stripSellerPII": True,
        "proxyConfiguration": {"useApifyProxy": True, "apifyProxyGroups": ["RESIDENTIAL"], "apifyProxyCountry": "ES"}}
    listing = adapter._normalize(milanuncios_row(), task(), None)
    assert listing.price == 250000 and listing.currency == "EUR" and listing.plot_m2 == 3200
    assert listing.area_m2 is None and listing.phone is None and listing.advertiser is None
    assert listing.address == "Madrid"
    await adapter.aclose()


@ALL
@pytest.mark.parametrize("change", [
    {"transactionType": "rent"}, {"transactionType": "new"}, {"city": "Alcorcón"}, {"cityName": "Alcorcón"},
    {"city": None, "cityName": None}, {"detailUrl": "https://www.idealista.com/inmueble/123456/"},
    {"url": "https://www.idealista.com/inmueble/123456/"}, {"detailUrl": "/not-a-listing"}, {"url": "javascript:alert(1)"},
])
def test_portal_rejects_foreign_deal_place_and_non_listing_rows(key, change):
    adapter = build(key, [], [])
    row = CASES[key][1]()
    if ("detailUrl" in change and key == "milanuncios") or ("url" in change and key != "milanuncios"):
        return  # a field this actor does not have
    if ("city" in change and key == "milanuncios") or ("cityName" in change and key != "milanuncios"):
        return
    assert adapter._normalize({**row, **change}, task(), None) is None
    assert adapter._normalize("not a row", task(), None) is None


@ALL
def test_portal_rejects_a_rent_url_for_a_sale_task_and_a_search_page(key):
    adapter = build(key, [], [])
    field = "url" if key == "milanuncios" else "detailUrl"
    rent = {"pisos": "https://www.pisos.com/alquilar/piso-madrid_capital-45678902_100500/",
            "habitaclia": "https://www.habitaclia.com/alquiler-piso-madrid-i500003456790.htm",
            "milanuncios": "https://www.milanuncios.com/alquiler-de-pisos-en-madrid/piso-centro-123456790.htm"}[key]
    search = {"pisos": "https://www.pisos.com/venta/terrenos-madrid/",
              "habitaclia": "https://www.habitaclia.com/terrenos-madrid.htm",
              "milanuncios": "https://www.milanuncios.com/venta-de-terrenos-en-madrid/"}[key]
    row = CASES[key][1]()
    row.pop("transactionType", None)
    assert adapter._normalize({**row, field: rent}, task(), None) is None
    assert adapter._normalize({**row, field: search}, task(), None) is None


def test_pisos_live_rows_capital_city_null_fields_and_plot_from_the_text():
    adapter = ApifyPisosSource("secret-token")
    rows = live_rows("pisos")
    listings = [adapter._normalize(row, task(), None) for row in rows]
    assert len(rows) == 4 and all(listings)  # city "Madrid Capital" is the task's Madrid
    assert all(classify_url(x.url) == "listing" and "/comprar/terreno-" in x.url for x in listings)
    assert [x.plot_m2 for x in listings] == [None, 1063, 232, 860]  # null areaSqm and no area in the text: None
    assert [x.advertiser for x in listings] == ["Garsierra", "Anna Serveis Inmobiliaris",
                                                   "Inmobiliaria Internacional Cpm", None]  # advertiserName null stays empty
    assert all(x.phone is None and x.area_m2 is None and x.currency == "EUR" for x in listings)
    assert listings[0].price == 1550000 and listings[0].address == "El Plantío, Moncloa-Aravaca, Madrid Capital, Madrid"
    assert rows[3]["advertiserType"] is None and rows[0]["terrainAreaSqm"] is None
    # an area written in the text of a row that has no areaSqm is the plot
    row = {**rows[0], "shortDescription": "Parcela urbana de 1.200 m2 en El Plantío."}
    assert adapter._normalize(row, task(), None).plot_m2 == 1200
    assert adapter._normalize({**rows[0], "city": "Getafe"}, task(), None) is None


def test_habitaclia_live_rows_plot_comes_only_from_the_text_and_portal_place_is_kept():
    adapter = ApifyHabitacliaSource("secret-token")
    rows = live_rows("habitaclia")
    listings = [adapter._normalize(row, task(), None) for row in rows]
    assert len(rows) == 3 and all(listings)  # subtypes null / industrialland / residentialland are all plots
    assert [r["propertySubtype"] for r in rows] == [None, "industrialland", "residentialland"]
    assert all(r["areaSqm"] is None for r in rows)
    assert all(classify_url(x.url) == "listing" for x in listings)
    assert [x.plot_m2 for x in listings] == [2519, 232, 4140]  # not the 1280 built or the 3519 buildable
    assert [x.advertiser for x in listings][:2] == ["GARSIERRA", "CPM INMOBILIARIA INTERNACIONAL - CASAS PISOS MADRID"]
    assert all(x.phone is None and x.area_m2 is None for x in listings)
    # the third ad is a plot in Mijas by its text, the portal files it under Madrid: kept as the portal gave it
    assert "Mijas" in listings[2].description and listings[2].address.startswith("Barrio de Salamanca, Madrid Capital")


@ALL
def test_portal_never_guesses_missing_facts(key):
    adapter = build(key, [], [])
    row = CASES[key][1]()
    nothing = {"shortDescription": "Parcela urbana.", "description": "Parcela urbana."}
    listing = adapter._normalize({**row, **nothing, "price": None, "areaSqm": None, "tag_sqm": None,
                                  "advertiserName": None, "fullAddress": None, "location": None}, task(), None)
    assert listing.price is None and listing.currency is None and listing.plot_m2 is None
    assert listing.phone is None and listing.advertiser is None and listing.rooms is None
    for price in ("250.000 €", 0, "consultar"):
        assert adapter._normalize({**row, "price": price}, task(), None).price is None
    contradicted = {"shortDescription": "Parcela de 900 m2.", "description": "Parcela de 900 m2."}
    assert adapter._normalize({**row, **contradicted}, task(), None).plot_m2 is None


@pytest.mark.parametrize("key", ["pisos", "habitaclia"])
def test_portal_price_in_another_currency_is_not_a_price(key):
    adapter = build(key, [], [])
    assert adapter._normalize({**CASES[key][1](), "priceCurrency": "USD"}, task(), None).price is None


@ALL
async def test_portal_setting_builds_the_source_only_when_enabled(key, monkeypatch):
    from bot.web_search.settings import WebSearchSettings

    flag = f"APIFY_{key.upper()}_ENABLED"
    monkeypatch.delenv(flag, raising=False)
    assert WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token").sources() == ()
    assert WebSearchSettings(_env_file=None, **{flag: True}).sources() == ()  # no token
    settings = WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token", **{
        flag: True, f"APIFY_{key.upper()}_MAX_RESULTS": 7, f"APIFY_{key.upper()}_MAX_CHARGE_USD": .03})
    source, = settings.sources()
    try:
        assert isinstance(source, CASES[key][0]) and source.actor_id == CASES[key][2]
        assert source.max_items == 7 and source.max_charge_usd == .03
    finally:
        await source.aclose()
    with pytest.raises(ValueError):
        WebSearchSettings(_env_file=None, **{f"APIFY_{key.upper()}_MAX_RESULTS": 51})


@ALL
@pytest.mark.parametrize("deal", ["SALE", "Venta"])
def test_portal_deal_is_case_insensitive_and_accepts_spanish(key, deal):
    adapter = build(key, [], [])
    row = {**CASES[key][1](), "transactionType": deal}
    assert adapter._normalize(row, task(), None).deal == "sale"
    assert adapter._normalize({**row, "transactionType": "ALQUILER"}, task(), None) is None


@ALL
def test_portal_accepts_the_capital_spelling_of_a_place(key):
    adapter = build(key, [], [])
    row = {**CASES[key][1](), "city": "Madrid Capital", "cityName": "Madrid Capital"}
    assert adapter._normalize(row, task(), None) is not None


@pytest.mark.parametrize(("key", "change"), [
    ("pisos", {"propertyType": "apartment"}), ("pisos", {"propertyType": "house"}),
    ("habitaclia", {"propertyType": "house"}), ("habitaclia", {"propertySubtype": "Piso"}),
    ("milanuncios", {"categorySlug": "venta-de-pisos"}),
    ("milanuncios", {"url": "https://www.milanuncios.com/venta-de-pisos-en-madrid/piso-centro-123456789.htm"}),
])
def test_land_task_drops_other_property_types(key, change):
    adapter = build(key, [], [])
    assert adapter._normalize({**CASES[key][1](), **change}, task(), None) is None


@pytest.mark.parametrize(("key", "change"), [
    ("pisos", {"propertyType": "land"}), ("pisos", {"propertyType": "ruralEstate"}),
    ("habitaclia", {"propertyType": "land", "propertySubtype": "Terreno urbano"}),
    ("milanuncios", {"categorySlug": "venta-de-terrenos"}),
])
def test_land_task_keeps_land_rows(key, change):
    adapter = build(key, [], [])
    assert adapter._normalize({**CASES[key][1](), **change}, task(), None).plot_m2 == 3200


@pytest.mark.parametrize("sell", ["demand", "busco", "unknown"])
def test_milanuncios_drops_wanted_ads_and_unknown_sell_types(sell):
    adapter = build("milanuncios", [], [])
    assert adapter._normalize(milanuncios_row(sellType=sell), task(), None) is None
    assert adapter._normalize(milanuncios_row(sellType="sell"), task(), None) is not None
    assert adapter._normalize(milanuncios_row(), task(), None) is not None


async def test_milanuncios_wanted_ads_are_counted_as_invalid(monkeypatch):
    from bot.web_search.sources import apify as module

    record = AsyncMock()
    monkeypatch.setattr(module.costs, "record", record)
    monkeypatch.setattr(module.costs, "over_budget", AsyncMock(return_value=False))
    adapter = build("milanuncios", [milanuncios_row(sellType="demand")], [])
    with source_run_scope(context("apify_milanuncios")):
        assert await adapter.search(task(), limit=5) == []
    assert any(c.kwargs.get("code") == "apify_invalid_listing" for c in record.await_args_list)
    await adapter.aclose()


async def test_milanuncios_search_url_uses_city_and_province_for_capitals():
    adapter = build("milanuncios", [], [])
    for location, level, expected in (("Madrid", "city", "venta-de-terrenos-en-madrid-madrid"),
                                      ("Madrid", "province", "venta-de-terrenos-en-madrid"),
                                      ("Alcalá de Henares", "city", "venta-de-terrenos-en-alcala_de_henares")):
        _, body, _ = await adapter._plan(task(location=location, place_level=level), 5, None)
        assert body["searchUrl"] == f"https://www.milanuncios.com/{expected}/"


def test_slug_is_ascii_letters_digits_and_underscores_only():
    from bot.web_search.sources.portals import _slug

    assert _slug("A Coruña") == "a_coruna" and _slug("L'Hospitalet de Llobregat") == "l_hospitalet_de_llobregat"
    assert _slug("  Sant-Cugat / Vallès! ") == "sant_cugat_valles"


def test_live_portal_url_shapes_are_listings_and_their_search_pages_are_not():
    for url in ("https://www.habitaclia.com/i15441000024727.htm",
                "https://www.habitaclia.com/comprar-terreno-madrid-i500003456789.htm",
                "https://www.pisos.com/comprar/terreno-san_blas_amposta-68412217734_101800/"):
        assert classify_url(url) == "listing", url
    assert classify_url("https://www.habitaclia.com/terrenos-madrid.htm") != "listing"


def test_text_area_is_a_plot_only_for_a_land_task_and_habitaclia_address_has_no_repeats():
    adapter = ApifyHabitacliaSource("secret-token")
    row = {**live_rows("habitaclia")[0], "shortDescription": "Parcela de 500 m² en venta.", "areaSqm": None}
    house = adapter._normalize(row, task(task_text="casa", goal="casa"), None)
    assert house is not None and house.plot_m2 is None
    listing = adapter._normalize(row, task(), None)
    assert listing.plot_m2 == 500
    assert listing.address == "Moncloa - Aravaca, Madrid Capital"
