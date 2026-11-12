"""Q20 PORTAL_RENTAL_TERM: the portal row's own rental term and a temporary let of a long-term rent.

The rows are synthetic in the shape of the recorded actor output (Barcelona flat rent, 2026-11:
``igolaizola/fotocasa-scraper`` ``rentalType`` LONG_TERM / TEMPORARY / null, ``igolaizola/idealista-scraper`` label
``seasonalRental``); ids, streets, phones and agency names are made up. Mocks only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from bot.analysis_pipeline.formatters import finding_payload, source_line_facts
from bot.analysis_pipeline.models import Evidence
from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer
from bot.campaign.tolerance import (
    TEMPORARY_NOTE,
    Request,
    classify,
    rental_term_enabled,
    request_for,
)
from bot.utils.listing_text import task_rental_term
from bot.web_search.queries import QueryTask
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources.portals import (
    ApifyFotocasaSource,
    ApifyHabitacliaSource,
    ApifyIdealistaPortalSource,
    ApifyMilanunciosSource,
)
from bot.web_search.worker import WebSearchConfig, WebSearchWorker, query_task

_REAL_CLIENT = httpx.AsyncClient
TERM_QUESTION = "На какой срок нужна аренда? Например: на год и дольше, от 6 месяцев, на 1–3 месяца."
YEAR_SPEC = {"context": {"answers": [{"question": TERM_QUESTION, "answer": "на год"}]}}
SOURCE_TEXT = "аренда до 1500 € Тип: квартира. Задача: снять piso в Барселоне до 1500 в месяц"
RENT = QueryTask("piso", SOURCE_TEXT, "Barcelona", {}, "real_estate", constraints={"deal": "rent"},
                 country_code="ES")
RENT_YEAR = QueryTask("piso", SOURCE_TEXT, "Barcelona", {}, "real_estate", constraints={"deal": "rent"},
                      country_code="ES", rental_term="long")
SALE = QueryTask("piso", "piso", "Barcelona", {}, "real_estate", constraints={"deal": "sale"}, country_code="ES")


def fotocasa_row(**kwargs):
    row = {"url": "https://www.fotocasa.es/es/alquiler/vivienda/barcelona-capital/amueblado/100000901/d",
           "propertyType": "2", "transaction": {"type": "RENT", "price": 1346, "priceDrop": None}, "surface": 49,
           "rooms": 1, "phone": "+34900000901", "street": "Calle Ejemplo", "agency": {"name": "Agencia Ejemplo"},
           "location": {"level2Name": "Barcelona", "level5Name": "Barcelona", "level8Name": "El Clot",
                        "latitude": "41.40", "longitude": "2.19"},
           "description": "Ático exterior de 49 m² con terraza.", "rentalType": "TEMPORARY"}
    return {**row, **kwargs}


def idealista_row(**kwargs):
    row = {"url": "https://www.idealista.com/inmueble/100000902/", "operation": "rent", "propertyType": "flat",
           "country": "es", "municipality": "Barcelona", "province": "Barcelona", "address": "Piso en Les Corts",
           "price": 1200, "size": 55, "rooms": 2, "latitude": 41.38, "longitude": 2.13,
           "contactInfo": {"commercialName": "Agencia Dos", "phone1": {"phoneNumberForMobileDialing": "+34900000902"}},
           "suggestedTexts": {"title": "Piso en Les Corts"},
           "labels": [{"name": "seasonalRental", "text": "Alquiler de temporada"}]}
    return {**row, **kwargs}


def text_of(listing) -> str:
    return WebSearchWorker._source_text(SimpleNamespace(config=WebSearchConfig()), listing)


def facts(listing) -> dict:
    return json.loads(text_of(listing).split("\n", 1)[0].removeprefix("JSON-LD: "))


# --- the row's term ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("source", "row"), [(ApifyFotocasaSource, fotocasa_row), (ApifyIdealistaPortalSource,
                                                                                    idealista_row)])
def test_flag_off_no_term_on_the_line(source, row):
    listing = source("secret-token")._normalize(row(), RENT, None)
    assert listing.rental_term is None and "rental_term" not in facts(listing)


@pytest.mark.parametrize(("value", "term"), [("TEMPORARY", "temporary"), ("LONG_TERM", "long"), (None, None),
                                             ("VACATIONAL", None)])  # a value not seen in the output: not translated
def test_flag_on_fotocasa_rental_type(value, term):
    listing = ApifyFotocasaSource("secret-token", rental_term=True)._normalize(fotocasa_row(rentalType=value), RENT,
                                                                               None)
    assert listing.rental_term == term and facts(listing).get("rental_term") == term
    off = ApifyFotocasaSource("secret-token")._normalize(fotocasa_row(rentalType=value), RENT, None)
    rest = {k: v for k, v in facts(listing).items() if k != "rental_term"}
    assert rest == facts(off)  # everything else on the line is as before


def test_flag_on_idealista_seasonal_label_only():
    source = ApifyIdealistaPortalSource("secret-token", rental_term=True)
    assert source._normalize(idealista_row(), RENT, None).rental_term == "temporary"
    other = idealista_row(labels=[{"name": "seaViewsType", "text": "Vistas al mar"}])
    assert source._normalize(other, RENT, None).rental_term is None  # no mark is no long term
    assert source._normalize(idealista_row(labels=None), RENT, None).rental_term is None


def test_flag_on_habitaclia_and_milanuncios_have_no_term():
    habitaclia = {"detailUrl": "https://www.habitaclia.com/alquiler-piso-barcelona-i500009990901.htm",
                  "title": "Piso en alquiler", "price": 1100, "priceCurrency": "EUR", "areaSqm": 60, "rooms": 2,
                  "city": "Barcelona", "province": "Barcelona", "location": "Gràcia, Barcelona",
                  "transactionType": "rent", "description": "Alquiler de temporada."}
    listing = ApifyHabitacliaSource("secret-token", rental_term=True)._normalize(habitaclia, RENT, None)
    assert listing is not None and listing.rental_term is None
    milanuncios = {"url": "https://www.milanuncios.com/alquiler-de-pisos-en-barcelona-barcelona/x-100000903.htm",
                   "title": "Piso", "price": 1000, "description": "Alquiler temporal.", "cityName": "Barcelona",
                   "provinceName": "Barcelona", "categorySlug": "alquiler-de-pisos", "sellType": "supply",
                   "tag_sqm": 50, "tag_bedrooms": 1}
    listing = ApifyMilanunciosSource("secret-token", rental_term=True)._normalize(milanuncios, RENT, None)
    assert listing is not None and listing.rental_term is None  # the text is never read as the term


# --- the task's term ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "term"), [
    ("на год", "long"), ("Аренда на год", "long"), ("долгосрочно", "long"), ("long term rent", "long"),
    ("на 2 года", "long"), ("год и дольше", "long"), ("на лето", "short"), ("на 1–3 месяца", "short"),
    ("посуточно", "short"), ("от 6 месяцев", None), ("не на год", None), (SOURCE_TEXT, None),
    ("на год, но можно и на лето", None),
    # review: «длительн» only as «длительный срок»; a negation among the 3 words before a term
    ("длительность не важна", None), ("на длительный срок", "long"),
    ("не обязательно долгосрочно", None), ("не обязательно на год", None), ("без долгосрочного контракта", None),
    ("not long term", None), ("срок не важен, можно и долгосрочно и временно", None),
    ("не обязательно на лето", None), ("не на лето, а на год", "long"),
    ("на 12 месяцев", "long"), ("на 1-2 года", "long"), ("минимум год", "long"), ("на 11 месяцев", "short"),
])
def test_task_rental_term(text, term):
    assert task_rental_term(None, text) == term


def test_task_term_from_the_answers_not_the_questions():
    # the bot's question names one long term only: were it read, «на год» would make the task long
    one_example = {"context": {"answers": [{"question": "На какой срок нужна аренда? Например: на год.",
                                            "answer": "без разницы"}]}}
    assert task_rental_term(one_example, SOURCE_TEXT) is None
    assert task_rental_term(YEAR_SPEC, SOURCE_TEXT) == "long"
    asked = {"context": {"answers": [{"question": TERM_QUESTION, "answer": "без разницы"}]}}
    assert task_rental_term(asked, SOURCE_TEXT) is None


def test_query_task_carries_the_term_of_a_rent():
    plan = SimpleNamespace(goal="g", location="Barcelona", location_aliases={}, vertical="real_estate",
                           constraints={"deal": "rent"}, languages=("es",), country="ES", search_plan=None)
    campaign = SimpleNamespace(plan=plan, spec=YEAR_SPEC, source_text=SOURCE_TEXT)
    assert query_task(campaign).rental_term == "long"
    campaign.plan = SimpleNamespace(**{**vars(plan), "constraints": {"deal": "sale"}})
    assert query_task(campaign).rental_term is None


# --- the Idealista input -----------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idealista_input_rental_types():
    on = ApifyIdealistaPortalSource("secret-token", rental_term=True)
    off = ApifyIdealistaPortalSource("secret-token")
    try:
        _, actor_input, _ = await on._plan(RENT_YEAR, 5, None)
        assert actor_input["rentalTypes"] == ["longTerm"]
        for source, task in ((off, RENT_YEAR), (on, RENT), (on, SALE)):
            _, actor_input, _ = await source._plan(task, 5, None)
            assert "rentalTypes" not in actor_input
    finally:
        await on.aclose()
        await off.aclose()


# --- tolerance ---------------------------------------------------------------------------------------------------

ANSWER = {
    "relevant": True, "confidence": 0.9, "summary": "Piso", "location": "Barcelona", "price_signals": ["1346"],
    "related_links": [], "category": "real_estate", "reason": "offer", "summary_ru": "Квартира",
    "source_language": "es", "price_amount": 1346, "price_currency": "EUR", "deal_type": "rent",
    "property_type": "apartment", "rooms": 1, "who": None, "listing_kind": "offer", "country": "ES", "area_m2": 49,
    "evidence": {"price": "1346", "area": "49", "rooms": "1", "location": None}, "district": None,
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


def barcelona(monkeypatch, flag: bool, spec=YEAR_SPEC, deal: str = "rent") -> Request:
    monkeypatch.setenv("PORTAL_RENTAL_TERM", "true" if flag else "false")
    return request_for({"deal": deal, "max_price": 1500}, location="Barcelona", vertical="real_estate",
                       text=SOURCE_TEXT, country="ES", spec=spec)


@pytest.mark.asyncio
async def test_flag_off_temporary_stays_exact(monkeypatch):
    listing = ApifyFotocasaSource("secret-token")._normalize(fotocasa_row(), RENT, None)
    found = await payload(monkeypatch, listing)
    assert "rental_term" not in found and source_line_facts(text_of(listing)).get("rental_term") is None
    request = barcelona(monkeypatch, False)
    assert request.rental_term is None and classify(found, request).bucket == "exact"
    # a payload written with the flag on is ignored after the rollback
    assert classify({**found, "rental_term": "temporary"}, request).bucket == "exact"


@pytest.mark.asyncio
async def test_flag_on_temporary_for_a_year_is_similar_long_term_is_exact(monkeypatch):
    source = ApifyFotocasaSource("secret-token", rental_term=True)
    temporary = await payload(monkeypatch, source._normalize(fotocasa_row(), RENT_YEAR, None))
    assert temporary["rental_term"] == "temporary"
    request = barcelona(monkeypatch, True)
    assert request.rental_term == "long"
    match = classify(temporary, request)
    assert (match.bucket, match.why, match.note) == ("similar", "deal_kind", TEMPORARY_NOTE)
    long = await payload(monkeypatch, source._normalize(fotocasa_row(rentalType="LONG_TERM"), RENT_YEAR, None))
    assert long["rental_term"] == "long" and classify(long, request).bucket == "exact"


@pytest.mark.asyncio
async def test_flag_on_without_a_term_in_the_task_nothing_changes(monkeypatch):
    found = await payload(monkeypatch, ApifyFotocasaSource("secret-token", rental_term=True)._normalize(
        fotocasa_row(), RENT, None))
    assert found["rental_term"] == "temporary"
    for spec in (None, {"context": {"answers": [{"question": TERM_QUESTION, "answer": "на лето"}]}}):
        request = barcelona(monkeypatch, True, spec=spec)
        assert classify(found, request).bucket == "exact"
    assert barcelona(monkeypatch, True, deal="sale").rental_term is None


def test_temporary_keeps_a_worse_bucket(monkeypatch):
    request = barcelona(monkeypatch, True)
    dear = {"price_amount": 2500, "price_currency": "EUR", "rental_term": "temporary", "location": "Barcelona",
            "deal_type": "rent"}
    assert (classify(dear, request).bucket, classify(dear, request).why) == ("other", "price")


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("PORTAL_RENTAL_TERM", raising=False)
    assert WebSearchSettings(_env_file=None).portal_rental_term is False
    assert rental_term_enabled() is False and Request().rental_term is None
    assert ApifyFotocasaSource("secret-token").rental_term is False


@pytest.mark.asyncio
async def test_settings_pass_the_flag_to_fotocasa_and_idealista():
    settings = WebSearchSettings(
        _env_file=None, APIFY_TOKEN="secret-token", PORTAL_RENTAL_TERM=True, APIFY_IDEALISTA_ENABLED=True,
        APIFY_IDEALISTA_PROVIDER="igolaizola", APIFY_FOTOCASA_ENABLED=True)
    sources = settings.sources()
    try:
        assert len(sources) == 2 and all(source.rental_term is True for source in sources)
    finally:
        for source in sources:
            await source.aclose()
