"""Q73 IDEALISTA_FLOOR_FACT: the portal row's own floor and lift reach the facts line, the payload and the reviewer.

Sim b7-g2-s8 («квартира в Валенсии, con ascensor, no bajo»): the Idealista rows had ``floor`` "4", "2", "1" and
``hasLift`` true, but the payload's floor was None and «исключить: первый этаж (bajo)» stayed unconfirmed. The rows
here are synthetic in the shape of the recorded output (igolaizola idealista ``floor``/``hasLift``, trev0n habitaclia
``floor``/``hasElevator``); ids, streets, phones and agency names are made up. Mocks only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from bot.analysis_pipeline.formatters import finding_payload, source_line_facts
from bot.analysis_pipeline.models import Evidence
from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer
from bot.campaign.relevance import finding_data
from bot.web_search.queries import QueryTask
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources.portals import (
    ApifyFotocasaSource,
    ApifyHabitacliaSource,
    ApifyIdealistaPortalSource,
    _row_floor,
)
from bot.web_search.worker import WebSearchConfig, WebSearchWorker

_REAL_CLIENT = httpx.AsyncClient
SALE = QueryTask("piso", "piso", "Valencia", {}, "real_estate", constraints={"deal": "sale"}, country_code="ES")


def idealista_row(**kwargs):
    row = {"url": "https://www.idealista.com/inmueble/100000731/", "operation": "sale", "propertyType": "flat",
           "country": "es", "municipality": "València", "province": "Valencia", "address": "Piso en Calle Ejemplo",
           "price": 240000, "size": 77, "rooms": 2, "latitude": 39.49, "longitude": -0.37,
           "contactInfo": {"commercialName": "Agencia Ejemplo", "phone1": {"phoneNumberForMobileDialing": "+34900000731"}},
           "suggestedTexts": {"title": "Piso en Calle Ejemplo"}, "floor": "4", "hasLift": True}
    return {**row, **kwargs}


def habitaclia_row(**kwargs):
    row = {"detailUrl": "https://www.habitaclia.com/comprar-piso-valencia-i500000000732.htm", "title": "Piso",
           "price": 200000, "priceCurrency": "EUR", "areaSqm": 70, "rooms": 2, "city": "Valencia",
           "province": "Valencia", "location": "Benimaclet, Valencia", "transactionType": "sale",
           "description": "Piso luminoso.", "floor": "GROUND_FLOOR", "hasElevator": True}
    return {**row, **kwargs}


def fotocasa_row(**kwargs):
    row = {"url": "https://www.fotocasa.es/es/comprar/vivienda/valencia-capital/ascensor/100000733/d",
           "propertyType": "2", "transaction": {"type": "SALE", "price": 210000}, "surface": 70, "rooms": 2,
           "street": "Calle Ejemplo", "agency": {"name": "Agencia Tres"},
           "location": {"level2Name": "Valencia", "level5Name": "Valencia", "latitude": "39.47", "longitude": "-0.37"},
           "description": "Piso.", "floor": "3", "floorType": "FIRST_FLOOR"}
    return {**row, **kwargs}


def text_of(listing) -> str:
    return WebSearchWorker._source_text(SimpleNamespace(config=WebSearchConfig()), listing)


def facts(listing) -> dict:
    return json.loads(text_of(listing).split("\n", 1)[0].removeprefix("JSON-LD: "))


# --- the facts line ---------------------------------------------------------------------------------------------

def test_flag_off_no_floor_on_the_line():
    for source, row in ((ApifyIdealistaPortalSource, idealista_row), (ApifyHabitacliaSource, habitaclia_row)):
        listing = source("secret-token")._normalize(row(), SALE, None)
        assert listing.floor is None and listing.lift is None
        assert "floor" not in facts(listing) and "lift" not in facts(listing)


@pytest.mark.parametrize(("value", "floor"), [("4", 4), ("2", 2), ("1", 1), ("bj", 0), ("baja", 0), ("0", 0),
                                              ("en", "entreplanta"), ("entreplanta", "entreplanta"), ("st", -1),
                                              ("sótano", -1), (None, None), ("xx", None), ("", None), (True, None)])
def test_flag_on_idealista_floor(value, floor):
    on = ApifyIdealistaPortalSource("secret-token", floor_fact=True)._normalize(idealista_row(floor=value), SALE, None)
    off = ApifyIdealistaPortalSource("secret-token")._normalize(idealista_row(floor=value), SALE, None)
    assert on.floor == floor and facts(on).get("floor") == floor and facts(on)["lift"] is True
    rest = {k: v for k, v in facts(on).items() if k not in ("floor", "lift")}
    assert rest == facts(off)  # everything else on the line is as before


def test_flag_on_idealista_lift_only_when_the_row_says():
    source = ApifyIdealistaPortalSource("secret-token", floor_fact=True)
    assert source._normalize(idealista_row(hasLift=False), SALE, None).lift is False
    row = idealista_row()
    del row["hasLift"], row["floor"]
    listing = source._normalize(row, SALE, None)
    assert listing.lift is None and listing.floor is None and "lift" not in facts(listing)
    assert source._normalize(idealista_row(hasLift="yes"), SALE, None).lift is None  # not a boolean: not guessed


@pytest.mark.parametrize(("value", "floor"), [("GROUND_FLOOR", 0), ("FIRST", 1), ("FOURTH", 4), ("TENTH", 10),
                                              ("BASEMENT", -1), ("MEZZANINE", "entreplanta"), ("PENTHOUSE", "ático"),
                                              (None, None), ("ELEVENTH", None)])
def test_flag_on_habitaclia_floor(value, floor):
    listing = ApifyHabitacliaSource("secret-token", floor_fact=True)._normalize(habitaclia_row(floor=value), SALE, None)
    assert listing.floor == floor and listing.lift is True
    assert ApifyHabitacliaSource("secret-token", floor_fact=True)._normalize(
        habitaclia_row(hasElevator=None), SALE, None).lift is None


def test_fotocasa_floor_code_is_not_mapped():
    """Fotocasa ``floor`` is a code («3» with FIRST_FLOOR): its meaning is not in the output, nothing is written."""
    listing = ApifyFotocasaSource("secret-token", floor_fact=True)._normalize(fotocasa_row(), SALE, None)
    assert listing is not None and listing.floor is None and "floor" not in facts(listing)


def test_row_floor_range():
    assert _row_floor(250) is None and _row_floor("-1") == -1 and _row_floor(3) == 3 and _row_floor("999") is None


# --- the analysis payload and the reviewer -------------------------------------------------------------------------

ANSWER = {
    "relevant": True, "confidence": 0.9, "summary": "Piso", "location": "València", "price_signals": ["240000"],
    "related_links": [], "category": "real_estate", "reason": "offer", "summary_ru": "Квартира",
    "source_language": "es", "price_amount": 240000, "price_currency": "EUR", "deal_type": "sale",
    "property_type": "apartment", "rooms": 2, "who": None, "listing_kind": "offer", "country": "ES", "area_m2": 77,
    "evidence": {"price": "240000", "area": "77", "rooms": "2", "location": None}, "district": None,
    "address": None, "floor": None, "features": [], "condition": None, "listing_date": None,
}


async def payload(monkeypatch, listing, answer=None) -> dict:
    """The finding payload the analysis writes for the post the web worker built from ``listing``."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer or ANSWER)}}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    evidence = Evidence(post_id="p", source_id="s", canonical_url=listing.url, title=listing.title,
                        text=text_of(listing), platform="website")
    result = await OpenRouterAnalyzer("key", "m").analyze(evidence, "real_estate")
    return finding_payload(result, evidence)


@pytest.mark.asyncio
async def test_payload_gets_the_row_floor_when_the_model_gave_none(monkeypatch):
    listing = ApifyIdealistaPortalSource("secret-token", floor_fact=True)._normalize(idealista_row(), SALE, None)
    assert source_line_facts(text_of(listing)) == {"phone": "+34900000731", "advertiser": "Agencia Ejemplo",
                                                   "floor": 4, "lift": True}
    found = await payload(monkeypatch, listing)
    assert found["floor"] == 4 and found["lift"] is True
    review = finding_data(found, original=text_of(listing), review=True)
    assert review["floor"] == 4 and review["lift"] is True and '"floor": 4' in review["excerpt"]
    bajo = ApifyIdealistaPortalSource("secret-token", floor_fact=True)._normalize(idealista_row(floor="bj"), SALE, None)
    found = await payload(monkeypatch, bajo)
    assert found["floor"] == 0 and finding_data(found, review=True)["floor"] == 0  # bajo is a fact, not «empty»


@pytest.mark.asyncio
async def test_payload_keeps_the_model_floor_and_a_word_floor_stays_on_the_line(monkeypatch):
    listing = ApifyIdealistaPortalSource("secret-token", floor_fact=True)._normalize(idealista_row(), SALE, None)
    assert (await payload(monkeypatch, listing, {**ANSWER, "floor": 3}))["floor"] == 3
    entre = ApifyIdealistaPortalSource("secret-token", floor_fact=True)._normalize(idealista_row(floor="en"), SALE,
                                                                                  None)
    found = await payload(monkeypatch, entre)
    assert found["floor"] is None and facts(entre)["floor"] == "entreplanta"


@pytest.mark.asyncio
async def test_flag_off_payload_as_before(monkeypatch):
    listing = ApifyIdealistaPortalSource("secret-token")._normalize(idealista_row(), SALE, None)
    found = await payload(monkeypatch, listing)
    assert found["floor"] is None and "lift" not in found
    assert "lift" not in finding_data(found, review=True) and "floor" not in finding_data(found, review=True)


# --- settings ----------------------------------------------------------------------------------------------------

def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("IDEALISTA_FLOOR_FACT", raising=False)
    assert WebSearchSettings(_env_file=None).idealista_floor_fact is False
    assert ApifyIdealistaPortalSource("secret-token").floor_fact is False


@pytest.mark.asyncio
async def test_settings_pass_the_flag_to_idealista_and_habitaclia():
    settings = WebSearchSettings(
        _env_file=None, APIFY_TOKEN="secret-token", IDEALISTA_FLOOR_FACT=True, APIFY_IDEALISTA_ENABLED=True,
        APIFY_IDEALISTA_PROVIDER="igolaizola", APIFY_HABITACLIA_ENABLED=True, APIFY_FOTOCASA_ENABLED=True)
    sources = settings.sources()
    try:
        flags = {source.name: source.floor_fact for source in sources}
        assert flags == {"apify_idealista": True, "apify_fotocasa": False, "apify_habitaclia": True}
    finally:
        for source in sources:
            await source.aclose()
