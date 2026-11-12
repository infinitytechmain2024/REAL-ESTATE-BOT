"""Q16 PORTAL_FACTS_FROM_ROW: the facts line of a portal row carries the row's type and deal, and an auction mark.

The rows are synthetic in the shape of the recorded actor output (``trev0n/habitaclia-com-spain-scraper``,
``igolaizola/fotocasa-scraper``, ``igolaizola/idealista-scraper``, ``rastriq/milanuncios-scraper``,
``igolaizola/yaencontre-scraper``, ``logiover/pisos-com-property-scraper``); names, streets, phones and ids are
made up. Mocks only.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from bot.analysis_pipeline.formatters import finding_payload, source_line_facts
from bot.analysis_pipeline.models import Evidence
from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer
from bot.campaign.tolerance import (
    AUCTION_NOTE,
    Request,
    classify,
    deal_kind_of,
    facts_from_row_enabled,
    request_for,
)
from bot.utils.listing_text import auction_named
from bot.web_search.queries import QueryTask
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources.portals import (
    ApifyFotocasaSource,
    ApifyHabitacliaSource,
    ApifyIdealistaPortalSource,
    ApifyMilanunciosSource,
    ApifyPisosSource,
    ApifyYaencontreSource,
)
from bot.web_search.worker import WebSearchConfig, WebSearchWorker

_REAL_CLIENT = httpx.AsyncClient
FLAT = QueryTask("piso", "piso", "Valencia", {}, "real_estate", constraints={"deal": "sale"}, country_code="ES")
LAND = QueryTask("terreno", "terreno", "Madrid", {}, "real_estate", constraints={"deal": "sale"}, country_code="ES")
AUCTION_TEXT = ("PROPIEDAD EN PROCESO DE SUBASTA – PROXIMA APERTURA DE SUBASTA\r\n\r\nPuedes acceder al estudio de "
                "este inmueble antes de que se fije la fecha.")


def habitaclia_row(**kwargs):
    row = {"detailUrl": "https://www.habitaclia.com/comprar-piso-valencia-i500009990001.htm",
           "title": "Subasta vivienda en Valencia, Calle Ejemplo 1", "price": 89250, "priceCurrency": "EUR",
           "areaSqm": 65, "rooms": 2, "city": "Valencia Capital", "province": "Valencia",
           "location": "Calle Ejemplo 1, L'Olivereta, Valencia Capital", "advertiserName": "Subastas Ejemplo",
           "propertyType": "flat", "propertySubtype": "flat", "transactionType": "sale", "advertType": "secondhand",
           "shortDescription": AUCTION_TEXT[:60], "description": AUCTION_TEXT}
    return {**row, **kwargs}


BARE = habitaclia_row(detailUrl="https://www.habitaclia.com/comprar-piso-valencia-i500009990002.htm",
                      title="Piso en venta en Valencia", propertyType=None, propertySubtype=None, transactionType=None,
                      shortDescription="Piso exterior de 55 m2.", description="Piso exterior de 55 m2.")


def fotocasa_row(**kwargs):
    row = {"url": "https://www.fotocasa.es/es/comprar/vivienda/valencia-capital/torrefiel/100000101/d",
           "propertyType": "2", "propertySubtype": "1", "transactionType": "1",
           "transaction": {"type": "SALE", "price": 150000}, "surface": 80, "rooms": 3, "phone": "+34900000101",
           "street": "Calle Ejemplo", "agency": {"name": "Agencia Ejemplo S.L."},
           "location": {"level2Name": "Valencia", "level5Name": "Valencia", "level8Name": "Torrefiel",
                        "latitude": "39.49", "longitude": "-0.37"},
           "description": "Piso de 80 m2 con 3 habitaciones."}
    return {**row, **kwargs}


def idealista_row(**kwargs):
    row = {"url": "https://www.idealista.com/inmueble/100000201/", "operation": "sale", "propertyType": "chalet",
           "detailedType": {"subTypology": "terracedHouse", "typology": "chalet"}, "country": "es",
           "municipality": "Valencia", "province": "Valencia", "address": "Chalet adosado en Benimaclet",
           "price": 390000, "size": 180, "rooms": 4, "latitude": 39.48, "longitude": -0.36,
           "contactInfo": {"commercialName": "Agencia Tres", "phone1": {"phoneNumberForMobileDialing": "+34900000201"}},
           "suggestedTexts": {"title": "Chalet adosado en Benimaclet"}}
    return {**row, **kwargs}


def milanuncios_row(**kwargs):
    row = {"url": "https://www.milanuncios.com/venta-de-pisos-en-valencia-valencia/valencia-100000301.htm",
           "title": "Valencia", "price": 103000, "description": "Ref: 0000. PISO EN SUBASTA de 75m2, tres dormitorios.",
           "cityName": "Valencia", "provinceName": "Valencia", "categorySlug": "venta-de-pisos", "sellType": "supply",
           "sellerType": "professional", "tag_sqm": 75, "tag_bedrooms": 3}
    return {**row, **kwargs}


def yaencontre_row(**kwargs):
    row = {"url": "https://www.yaencontre.com/venta/piso/inmueble-100000401", "title": "Piso en venta en Ruzafa",
           "price": 210000, "area": 90, "rooms": 3, "operation": "BUY", "family": "FLAT",
           "description": "Piso reformado.", "address": {"qualifiedName": "Ruzafa, Ensanche, Valencia"},
           "owner": {"name": "Agencia Cuatro", "phoneAvailable": True, "virtualPhoneNumber": "900000401"}}
    return {**row, **kwargs}


def pisos_row(**kwargs):
    row = {"detailUrl": "https://www.pisos.com/comprar/piso-valencia_capital-100000501_100500/",
           "title": "Piso en Valencia", "price": 180000, "priceCurrency": "EUR", "areaSqm": 70, "rooms": 2,
           "city": "Valencia Capital", "province": "Valencia", "fullAddress": "Calle Ejemplo, Valencia",
           "advertiserName": "Inmobiliaria Ejemplo", "transactionType": "sale", "shortDescription": "Piso luminoso."}
    return {**row, **kwargs}


CASES = {
    "habitaclia": (ApifyHabitacliaSource, habitaclia_row),
    "fotocasa": (ApifyFotocasaSource, fotocasa_row),
    "idealista": (ApifyIdealistaPortalSource, idealista_row),
    "milanuncios": (ApifyMilanunciosSource, milanuncios_row),
    "yaencontre": (ApifyYaencontreSource, yaencontre_row),
    "pisos": (ApifyPisosSource, pisos_row),
}

# The first line of each post with the flag off, as ``main`` wrote it before Q16 (type and deal: the task's).
OFF = {
    "habitaclia": 'JSON-LD: {"url": "https://www.habitaclia.com/comprar-piso-valencia-i500009990001.htm", '
                  '"title": "Subasta vivienda en Valencia, Calle Ejemplo 1", "price": 89250.0, "currency": "EUR", '
                  '"area_m2": 65.0, "rooms": 2, "address": "Calle Ejemplo 1, L\'Olivereta, Valencia Capital", '
                  '"property_type": "apartment", "deal": "sale", "advertiser": "Subastas Ejemplo"}',
    "fotocasa": 'JSON-LD: {"url": "https://www.fotocasa.es/es/comprar/vivienda/valencia-capital/torrefiel/100000101/d", '
                '"title": "Vivienda en venta, Torrefiel", "price": 150000.0, "currency": "EUR", "area_m2": 80.0, '
                '"rooms": 3, "address": "Calle Ejemplo, Torrefiel, Valencia", "deal": "sale", '
                '"phone": "+34900000101", "advertiser": "Agencia Ejemplo S.L."}',
    "idealista": 'JSON-LD: {"url": "https://www.idealista.com/inmueble/100000201/", '
                 '"title": "Chalet adosado en Benimaclet", "price": 390000.0, "currency": "EUR", "area_m2": 180.0, '
                 '"rooms": 4, "address": "Chalet adosado en Benimaclet, Valencia", "deal": "sale", '
                 '"phone": "+34900000201", "advertiser": "Agencia Tres"}',
    "milanuncios": 'JSON-LD: {"url": "https://www.milanuncios.com/venta-de-pisos-en-valencia-valencia/'
                   'valencia-100000301.htm", "title": "Valencia", "price": 103000.0, "currency": "EUR", '
                   '"area_m2": 75.0, "rooms": 3, "address": "Valencia", "property_type": "apartment", "deal": "sale"}',
    "yaencontre": 'JSON-LD: {"url": "https://www.yaencontre.com/venta/piso/inmueble-100000401", '
                  '"title": "Piso en venta en Ruzafa", "price": 210000.0, "currency": "EUR", "area_m2": 90.0, '
                  '"rooms": 3, "address": "Ruzafa, Ensanche, Valencia", "property_type": "apartment", '
                  '"deal": "sale", "phone": "900000401", "advertiser": "Agencia Cuatro"}',
    "pisos": 'JSON-LD: {"url": "https://www.pisos.com/comprar/piso-valencia_capital-100000501_100500/", '
             '"title": "Piso en Valencia", "price": 180000.0, "currency": "EUR", "area_m2": 70.0, "rooms": 2, '
             '"address": "Calle Ejemplo, Valencia", "property_type": "apartment", "deal": "sale", '
             '"advertiser": "Inmobiliaria Ejemplo"}',
}
# With the flag on: the row's own type and deal (None: the row gives none) and the auction mark.
ON = {
    "habitaclia": ("apartment", "sale", True),
    "fotocasa": ("vivienda", "sale", None),  # code 2: «vivienda», flat or house: the portal says no more
    "idealista": ("house", "sale", None),  # the row is a chalet, whatever the task asked for
    "milanuncios": ("apartment", "sale", True),
    "yaencontre": ("apartment", "sale", None),
    "pisos": (None, "sale", None),  # no propertyType in the row: no type, never the task's
}


def adapter(key, on: bool):
    return CASES[key][0]("secret-token", **({"facts_from_row": True} if on else {}))


def text_of(listing) -> str:
    return WebSearchWorker._source_text(SimpleNamespace(config=WebSearchConfig()), listing)


def first_line(listing) -> str:
    return text_of(listing).split("\n", 1)[0]


def facts(listing) -> dict:
    return json.loads(first_line(listing).removeprefix("JSON-LD: "))


@pytest.mark.parametrize("key", CASES)
def test_flag_off_the_line_is_unchanged(key):
    listing = adapter(key, False)._normalize(CASES[key][1](), FLAT, None)
    assert listing is not None and listing.auction is None
    assert first_line(listing) == OFF[key]


@pytest.mark.parametrize("key", CASES)
def test_flag_on_type_and_deal_come_from_the_row(key):
    listing = adapter(key, True)._normalize(CASES[key][1](), FLAT, None)
    kind, deal, auction = ON[key]
    assert (listing.property_type, listing.deal, listing.auction) == (kind, deal, auction)
    line = facts(listing)
    assert line.get("property_type") == kind and line.get("deal") == deal and line.get("auction") == auction
    assert ("property_type" in line) is (kind is not None)
    # everything else on the line is as before
    before = json.loads(OFF[key].removeprefix("JSON-LD: "))
    rest = {k: v for k, v in line.items() if k not in ("property_type", "deal", "auction")}
    assert rest == {k: v for k, v in before.items() if k not in ("property_type", "deal")}


def test_flag_on_a_row_without_type_and_deal_has_neither_field():
    listing = adapter("habitaclia", True)._normalize(BARE, FLAT, None)
    line = facts(listing)
    assert "property_type" not in line and "deal" not in line and "auction" not in line
    off = facts(adapter("habitaclia", False)._normalize(BARE, FLAT, None))
    assert off["property_type"] == "apartment" and off["deal"] == "sale"  # the echo of the task the flag removes


def test_flag_on_habitaclia_land_subtype_and_fotocasa_land_code():
    land = habitaclia_row(detailUrl="https://www.habitaclia.com/comprar-terreno-madrid-i500009990003.htm",
                          title="Terreno en Madrid", city="Madrid Capital", province="Madrid", location="Barajas",
                          propertyType=None, propertySubtype="residentialland", areaSqm=None,
                          description="Parcela de 3.200 m2.", shortDescription="Parcela de 3.200 m2.")
    listing = adapter("habitaclia", True)._normalize(land, LAND, None)
    assert listing.property_type == "landparcel" and listing.plot_m2 == 3200 and listing.area_m2 is None
    plot = fotocasa_row(url="https://www.fotocasa.es/es/comprar/terreno/madrid-capital/rejas/100000102/d",
                        propertyType="4", propertySubtype="58", rooms=None, surface=2500,
                        location={"level2Name": "Madrid", "level5Name": "Madrid", "level8Name": "Rejas",
                                  "latitude": "40.44", "longitude": "-3.58"}, description="Parcela urbana.")
    listing = adapter("fotocasa", True)._normalize(plot, LAND, None)
    assert listing.property_type == "landparcel" and listing.plot_m2 == 2500
    unknown = adapter("fotocasa", True)._normalize(fotocasa_row(propertyType="7"), FLAT, None)
    assert unknown.property_type is None  # a code not seen in the recorded output is not translated


def test_the_existing_row_filters_stay():
    rent = adapter("habitaclia", True)._normalize(habitaclia_row(transactionType="rent"), FLAT, None)
    assert rent is None
    assert adapter("fotocasa", True)._normalize(fotocasa_row(transaction={"type": "RENT", "price": 900}),
                                                 FLAT, None) is None


@pytest.mark.parametrize(("text", "hit"), [
    ("PROPIEDAD EN PROCESO DE SUBASTA", True),
    ("Piso en subasta judicial", True),
    ("Subastas de inmuebles", True),
    ("Vivienda procedente de adjudicación bancaria", False),  # an ordinary bank sale
    ("Adjudicación de obras de reforma", False),
    ("Cesión de remate disponible", True),
    ("cesion del remate", True),
    ("Puja mínima 50.000 €", True),
    ("Piso de banco, no es subasta", False),
    ("No es una subasta: venta directa", False),
    ("Venta directa sin subasta", False),
    ("Vivienda que no procede de subasta", False),
    ("No es subasta. Hay otra en subasta en el mismo edificio", True),  # one auction not negated is enough
    ("Piso reformado, subastado no", False),  # «subastado» is another word
    ("Piso luminoso en Ruzafa", False),
    ("", False),
])
def test_auction_words(text, hit):
    assert auction_named(text) is hit
    # one list of words with CAMPAIGN_AUCTION_DEVIATION (``deal_kind_of``)
    assert (deal_kind_of(text) == "аукцион") is hit


def test_flag_on_land_words_of_pisos_and_habitaclia_are_a_plot():
    pisos = pisos_row(detailUrl="https://www.pisos.com/comprar/terreno-madrid_capital-100000502_100500/",
                      city="Madrid Capital", province="Madrid", propertyType="ruralEstate", areaSqm=3000,
                      shortDescription="Finca rústica de 3.000 m2.")
    listing = adapter("pisos", True)._normalize(pisos, LAND, None)
    assert listing.property_type == "landparcel" and listing.plot_m2 == 3000
    assert adapter("pisos", True)._normalize(pisos_row(propertyType="ruralEstate"), FLAT, None).property_type == "ruralestate"
    for subtype in ("Terreno urbano", "Terreno rústico", "Solar"):
        row = habitaclia_row(detailUrl="https://www.habitaclia.com/comprar-terreno-madrid-i500009990004.htm",
                             title="Terreno en Madrid", city="Madrid Capital", province="Madrid", location="Barajas",
                             propertyType=None, propertySubtype=subtype, areaSqm=1500,
                             description="Parcela de 1.500 m2.", shortDescription="Parcela de 1.500 m2.")
        assert adapter("habitaclia", True)._normalize(row, LAND, None).property_type == "landparcel"


@pytest.mark.parametrize("code", ["4", 4])
def test_flag_on_fotocasa_code_as_number_or_text(code):
    plot = fotocasa_row(url="https://www.fotocasa.es/es/comprar/terreno/madrid-capital/rejas/100000103/d",
                        propertyType=code, rooms=None, surface=2500,
                        location={"level2Name": "Madrid", "level5Name": "Madrid", "level8Name": "Rejas",
                                  "latitude": "40.44", "longitude": "-3.58"}, description="Parcela urbana.")
    assert adapter("fotocasa", True)._normalize(plot, LAND, None).property_type == "landparcel"
    assert adapter("fotocasa", True)._normalize(fotocasa_row(propertyType=2), FLAT, None).property_type == "vivienda"


def test_flag_off_by_default_everywhere(monkeypatch):
    monkeypatch.delenv("PORTAL_FACTS_FROM_ROW", raising=False)
    assert WebSearchSettings(_env_file=None).portal_facts_from_row is False
    assert facts_from_row_enabled() is False and Request().auction_guard is False
    assert all(adapter(key, False).facts_from_row is False for key in CASES)


@pytest.mark.asyncio
async def test_settings_pass_the_flag_to_every_portal():
    settings = WebSearchSettings(
        _env_file=None, APIFY_TOKEN="secret-token", PORTAL_FACTS_FROM_ROW=True, APIFY_IDEALISTA_ENABLED=True,
        APIFY_IDEALISTA_PROVIDER="igolaizola", APIFY_FOTOCASA_ENABLED=True, APIFY_YAENCONTRE_ENABLED=True,
        APIFY_PISOS_ENABLED=True, APIFY_HABITACLIA_ENABLED=True, APIFY_MILANUNCIOS_ENABLED=True)
    sources = settings.sources()
    try:
        assert len(sources) == 6 and all(source.facts_from_row is True for source in sources)
    finally:
        for source in sources:
            await source.aclose()


ANSWER = {
    "relevant": True, "confidence": 0.9, "summary": "Piso", "location": "Valencia", "price_signals": ["89250"],
    "related_links": [], "category": "real_estate", "reason": "offer", "summary_ru": "Квартира",
    "source_language": "es", "price_amount": 89250, "price_currency": "EUR", "deal_type": "sale",
    "property_type": "apartment", "rooms": 2, "who": None, "listing_kind": "offer", "country": "ES", "area_m2": 65,
    "evidence": {"price": "89250", "area": "65", "rooms": "2", "location": None}, "district": None,
    "address": None, "floor": None, "features": [], "condition": None, "listing_date": None,
}


async def payload(monkeypatch, listing) -> dict:
    """The finding payload the analysis writes for the post the web worker built from ``listing``."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ANSWER)}}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    evidence = Evidence(post_id="p", source_id="s", canonical_url=listing.url, title=listing.title,
                        text=text_of(listing), platform="website")
    result = await OpenRouterAnalyzer("key", "m").analyze(evidence, "real_estate")
    return finding_payload(result, evidence)


def valencia(monkeypatch, flag: bool, deal: str = "sale") -> Request:
    monkeypatch.setenv("PORTAL_FACTS_FROM_ROW", "true" if flag else "false")
    return request_for({"deal": deal}, location="Valencia", vertical="real_estate", country="ES")


@pytest.mark.asyncio
async def test_flag_off_the_auction_flat_stays_exact(monkeypatch):
    monkeypatch.delenv("CAMPAIGN_AUCTION_DEVIATION", raising=False)
    listing = adapter("habitaclia", False)._normalize(habitaclia_row(), FLAT, None)
    found = await payload(monkeypatch, listing)
    assert "auction" not in found and source_line_facts(text_of(listing)).get("auction") is None
    assert classify(found, valencia(monkeypatch, False)).bucket == "exact"


@pytest.mark.asyncio
async def test_flag_on_the_auction_flat_goes_to_similar_for_a_purchase(monkeypatch):
    listing = adapter("habitaclia", True)._normalize(habitaclia_row(), FLAT, None)
    found = await payload(monkeypatch, listing)
    assert found["auction"] is True
    match = classify(found, valencia(monkeypatch, True))
    assert (match.bucket, match.why, match.note) == ("similar", "deal_kind", AUCTION_NOTE)
    # the mark alone does nothing with the flag off in the runner (a payload written before a rollback)
    assert classify(found, valencia(monkeypatch, False)).bucket == "exact"
    # a request without a purchase does not read the mark
    assert classify(found, valencia(monkeypatch, True, deal="")).bucket == "exact"
    # an ordinary flat stays exact with the flag on
    plain = await payload(monkeypatch, adapter("habitaclia", True)._normalize(BARE, FLAT, None))
    assert "auction" not in plain and classify(plain, valencia(monkeypatch, True)).bucket == "exact"


def test_auction_keeps_a_worse_bucket(monkeypatch):
    request = replace(valencia(monkeypatch, True), amount=100000)
    dear = {"price_amount": 200000, "price_currency": "EUR", "auction": True, "location": "Valencia"}
    assert (classify(dear, request).bucket, classify(dear, request).why) == ("other", "price")  # never better
