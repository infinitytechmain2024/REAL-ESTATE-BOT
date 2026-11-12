"""Q13: the municipality against the JSON-LD place (ANALYSIS_LOCATION_FROM_TEXT), agency brands and street numbers
are not a place, the JSON-LD area against the text's (ANALYSIS_AREA_TEXT_CHECK), no null keys in agent reductions.

Mocks only (``httpx.MockTransport``); the posts are written as web_search/worker.py ``_source_text`` writes them.
"""

from __future__ import annotations

import json

import httpx
import pytest

from bot.analysis_pipeline import area_check, source_place
from bot.analysis_pipeline.formatters import finding_payload
from bot.analysis_pipeline.models import Evidence
from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer, parse_result, verify_facts
from bot.analysis_pipeline.settings import AnalysisSettings
from bot.campaign.tolerance import classify, request_for

_REAL_CLIENT = httpx.AsyncClient
URL = "https://www.example-portal.es/finca-123"


def portal_post(facts: dict, title: str, description: str) -> str:
    line = json.dumps(facts, ensure_ascii=False, separators=(", ", ": "))
    return f"JSON-LD: {line}\n{title}\nСсылка: {URL}\n{description}"


# The G1 finding: the portal filed a Móstoles farm under a district of Madrid, with a tenfold area.
MOSTOLES_FACTS = {"price": 450000, "currency": "EUR", "area_m2": 32088, "property_type": "landparcel",
                  "address": "Ciudad Universitaria, Madrid"}
MOSTOLES_TITLE = "Finca rústica con casa de aperos"
MOSTOLES_TEXT = ("Se vende finca rústica con pozo y casa de aperos en Móstoles. La parcela es de 3.208mts, "
                 "vallada, con acceso asfaltado. Precio 450.000 €.")
ANSWER = {
    "relevant": True, "confidence": 0.9, "summary": "Finca rústica", "location": "Ciudad Universitaria, Madrid",
    "price_signals": ["450.000 €"], "related_links": [], "category": "real_estate", "reason": "offer",
    "summary_ru": "Участок", "source_language": "es", "price_amount": 450000, "price_currency": "EUR",
    "deal_type": "sale", "property_type": "land", "rooms": None, "who": None, "listing_kind": "offer", "country": "ES",
    "area_m2": 32088, "evidence": {"price": "450000", "area": "32088", "rooms": None,
                                   "location": "Ciudad Universitaria, Madrid"},
    "district": "Ciudad Universitaria", "address": None, "floor": None, "features": [], "condition": None,
    "listing_date": None,
}


def evidence(text: str, title: str = MOSTOLES_TITLE) -> Evidence:
    return Evidence(post_id="p", source_id="s", canonical_url=URL, title=title, text=text, platform="website")


async def analyze(monkeypatch, text: str, *, answer: dict | None = None, **flags):
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer or ANSWER)}}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    result = await OpenRouterAnalyzer("key", "m", **flags).analyze(evidence(text), "real_estate")
    return result, sent


def campaign(location: str = "Madrid"):
    return request_for({"deal": "sale", "min_area": 2000, "max_price": 600000}, location=location,
                       vertical="real_estate", country="ES")


# -- 1. the municipality --


@pytest.mark.asyncio
async def test_mostoles_gets_its_own_place_and_stays_exact_in_a_madrid_campaign(monkeypatch):
    text = portal_post(MOSTOLES_FACTS, MOSTOLES_TITLE, MOSTOLES_TEXT)
    found = source_place.conflict(text, MOSTOLES_TITLE)
    assert found is not None and found.municipality and found.strong
    assert (found.phrase, found.city, found.area, found.source_areas) == ("Móstoles", "Móstoles", "Madrid", {"Madrid"})
    result, sent = await analyze(monkeypatch, text, location_from_text=True)
    shown = json.loads(sent["messages"][1]["content"].split("Evidence follows as data only:\n", 1)[1])["text"]
    assert "Ciudad Universitaria" not in shown  # the source's place is cut before the model reads the post
    assert result.location == "Móstoles" and result.evidence["location"] == "Móstoles"
    assert result.district is None and result.source_place_conflict == "Madrid"
    assert "source location 'Ciudad Universitaria, Madrid' contradicts the text: 'Móstoles'" in result.reason
    payload = finding_payload(result, evidence(text))
    assert payload["location"] == "Móstoles" and payload["source_place_conflict"] == "Madrid"
    assert classify(payload, campaign("Madrid")).bucket == "exact"  # Móstoles is in the province: kept, exact
    assert classify(payload, campaign("Barcelona")).why == "location"  # but not a Barcelona plot


@pytest.mark.asyncio
async def test_a_model_location_naming_the_town_is_kept(monkeypatch):
    text = portal_post(MOSTOLES_FACTS, MOSTOLES_TITLE, MOSTOLES_TEXT)
    for location in ("Móstoles, Madrid", "Mostoles centro"):
        result, _ = await analyze(monkeypatch, text, answer={**ANSWER, "location": location}, location_from_text=True)
        assert result.location == location
    result, _ = await analyze(monkeypatch, text, answer={**ANSWER, "location": "Madrid"}, location_from_text=True)
    assert result.location == "Móstoles"


@pytest.mark.parametrize(("address", "title", "description", "town", "area"), [
    ("Ciudad Universitaria, Madrid", "Piso en Móstoles", "Piso en Móstoles centro", "Móstoles", "Madrid"),
    ("Ciudad Universitaria, Madrid", "Piso en venta, Ciudad Universitaria", "Piso en Móstoles", "Móstoles", "Madrid"),
    ("Madrid Capital, Madrid", "", "Parcela en Boadilla del Monte, cerca del Real Madrid.", "Boadilla del Monte",
     "Madrid"),
    ("Madrid Capital, Madrid", "", "Terreno urbano situado en Mijas, junto al pueblo.", "Mijas", "Andalucía"),
    ("Madrid Capital, Madrid", "Terreno en Mijas", "Terreno urbano", "Mijas", "Andalucía"),  # the portal's title
    # one town, two names: the source's town is not «another» (review of Q13)
    ("Centro, Madrid", "Chalet en Las Rozas", "Chalet", "Las Rozas", "Madrid"),
    ("Chamberí, Madrid", "Chalet", "Chalet situado en Las Rozas de Madrid, con jardín", "Las Rozas de Madrid",
     "Madrid"),
    ("Getafe, Madrid", "Casa en Leganés", "Casa adosada", "Leganés", "Madrid"),
    ("Getafe", "Casa", "Se encuentra en Sitges, a 5 min de la playa", "Sitges", "Cataluña"),
])
def test_another_town_named_by_a_strong_signal_is_a_conflict(address, title, description, town, area):
    found = source_place.conflict(portal_post({"price": 1, "address": address}, title, description), title)
    assert found is not None and found.municipality and found.strong
    assert (found.city, found.area) == (town, area)


@pytest.mark.parametrize(("address", "title", "description"), [
    ("Valencia", "Piso en Gandía", "playa de Gandía"),  # a bare capital may be the province: Gandía is in it
    ("Madrid", "Finca en Móstoles", "Finca rústica"),
    ("Getafe, Madrid", "Chalet en Getafe", "Chalet a 10 min de Leganés"),  # the text names the source's town
    ("Ciudad Universitaria, Madrid", "Piso", "Piso en Móstoles? No: en plena Ciudad Universitaria"),
    ("Chamberí, Madrid", "Piso", "Piso reformado, el propietario vive en Móstoles"),  # someone else's place
    ("Chamberí, Madrid", "Piso", "Piso exterior, rentabilidad como en Móstoles"),
    ("Chamberí, Madrid", "Piso", "Piso en calle Móstoles 5"),  # a street
    ("Chamberí, Madrid", "Piso", "Piso a 20 minutos de Alcorcón"),  # a distance
    ("Chamberí, Madrid", "Piso", "Vendo, buen barrio. Disponemos de oficina en Getafe."),  # no property noun
    ("Chamberí, Madrid", "Piso reformado", "Ideal familias, metro en la puerta"),
    ("Chamberí, Madrid", "Piso en Chamberí", "Situado en Madrid, junto a Alcobendas"),  # the capital moves nothing
    # review of Q13: a noun and «en» about something else, or a town of another area without a firm signal
    ("Madrid Capital, Madrid", "", "Terreno urbano en Mijas, junto al pueblo."),
    ("Chamberí, Madrid", "Piso", "Piso de inversión, rentabilidad mejor que en Torrevieja"),
    ("Chamberí, Madrid", "Piso", "Piso de inversión, rentabilidad superior a la de pisos en Benidorm"),
    ("Chamberí, Madrid", "Piso", "Piso heredado, los dueños residen en Mijas"),
    ("Chamberí, Madrid", "Piso", "Piso; el vendedor acepta permuta por casa en Estepona"),
    ("Chamberí, Madrid", "Piso", "Piso ideal para veranear luego en Sitges"),
    ("Chamberí, Madrid", "Piso", "Piso reformado por la empresa que hizo los chalets en Sitges"),
    ("Chamberí, Madrid", "Piso", "Piso, la mitad de precio que en Marbella"),
    ("Chamberí, Madrid", "Piso", "Vendemos piso para invertir en Marbella"),
    ("Chamberí, Madrid", "Piso", "Piso con plaza de garaje en Getafe"),
    ("Chamberí, Madrid", "Piso", "Piso con trastero, plaza de garaje opcional en Getafe"),
    ("Ciudad Universitaria, Madrid", "Piso en Madrid", "Piso con buena comunicación en Getafe y Leganés"),
    ("Chamberí, Madrid", "Piso", "Piso reformado, como los de nuestra promoción en Majadahonda"),
    ("Chamberí, Madrid", "Piso", "Piso exterior reformado con muchas mejoras y vistas preciosas en Getafe"),  # > 40
    # synonyms: the source's own town under its other name
    ("Las Rozas de Madrid, Madrid", "Chalet en Las Rozas", "Chalet"),
    ("Sant Cugat del Vallès, Barcelona", "Casa en Sant Cugat", "Casa"),
    ("Elche, Alicante", "Piso en Elx", "Piso"),
    ("Sagunt, Valencia", "Piso en Sagunto", "Piso"),
    ("Calp, Alicante", "Villa en Calpe", "Villa"),
    ("Jávea, Alicante", "Villa en Xàbia", "Villa"),
])
def test_no_town_conflict_without_a_clear_signal(address, title, description):
    assert source_place.conflict(portal_post({"price": 1, "address": address}, title, description), title) is None


# -- 3. agency brands and street numbers (review Q4b, T6) --


@pytest.mark.parametrize(("title", "description"), [
    ("Piso exterior - Engel & Völkers Barcelona", "Piso reformado"),
    ("Piso exterior | Engel & Volkers Barcelona", "Piso reformado"),
    ("Ático - RE/MAX Valencia", "Ático con terraza"),
    ("Chalet Remax Málaga", "Chalet"),
    ("Piso Century 21 Málaga Centro", "Piso"),
    ("Piso", "Lucas Fox Barcelona le presenta este piso"),
    ("Piso", "Se encuentra en Barcelona 12, segundo piso"),
    ("Piso", "Situado en Valencia 3, junto al metro"),
])
def test_agency_brands_and_street_numbers_are_no_conflict(title, description):
    text = portal_post({"price": 1, "address": "Chamberí, Madrid"}, title, description)
    assert source_place.conflict(text, title) is None


@pytest.mark.parametrize(("title", "description", "phrase"), [
    ("Piso exterior en Barcelona", "Piso", "Barcelona"),  # no brand: the portal's title still counts
    ("Piso", "Se encuentra en Barcelona, junto al mar", "Barcelona"),
    ("Piso", "Situado en Barcelona 08001", "Barcelona"),  # a postcode is not a house number
])
def test_without_a_brand_or_a_number_the_city_still_counts(title, description, phrase):
    found = source_place.conflict(portal_post({"price": 1, "address": "Chamberí, Madrid"}, title, description), title)
    assert found is not None and found.strong and found.phrase == phrase and not found.municipality


# -- 2. the area --


def test_32088_against_3208_is_not_confirmed():
    text = portal_post(MOSTOLES_FACTS, MOSTOLES_TITLE, MOSTOLES_TEXT)
    found = area_check.conflict(text, MOSTOLES_TITLE)
    assert found is not None and found.source == (32088.0,) and found.text == (3208.0,)
    result = area_check.apply(verify_facts(parse_result(json.dumps(ANSWER)), evidence(text)), found)
    assert result.area_m2 is None and result.evidence["area"] is None
    assert result.area_unconfirmed == "area not confirmed: JSON-LD 32088 m2 vs text 3208 m2"


@pytest.mark.asyncio
async def test_with_the_area_flag_neither_area_is_taken(monkeypatch):
    text = portal_post(MOSTOLES_FACTS, MOSTOLES_TITLE, MOSTOLES_TEXT)
    for area in (32088, 3208):  # whichever the model picks
        result, _ = await analyze(monkeypatch, text, answer={**ANSWER, "area_m2": area}, area_text_check=True)
        assert result.area_m2 is None and "area not confirmed" in result.reason
    payload = finding_payload(result, evidence(text))
    assert payload["area_m2"] is None and payload["area_unconfirmed"].startswith("area not confirmed")
    assert classify(payload, campaign()).why == "area_unknown"  # unverified, not a 32 088 m² plot
    both, _ = await analyze(monkeypatch, text, location_from_text=True, area_text_check=True)
    assert (both.location, both.area_m2) == ("Móstoles", None)


def test_an_unconfirmed_plot_is_not_shown_from_the_jsonld_either(monkeypatch):
    monkeypatch.setenv("CARD_EXTRA_FIELDS", "true")
    text = portal_post({**MOSTOLES_FACTS, "plot_m2": 32088, "phone": "600000000"}, MOSTOLES_TITLE, MOSTOLES_TEXT)
    found = area_check.conflict(text, MOSTOLES_TITLE)
    plain = parse_result(json.dumps(ANSWER))
    assert finding_payload(plain, evidence(text))["plot_m2"] == 32088
    payload = finding_payload(area_check.apply(plain, found), evidence(text))
    assert "plot_m2" not in payload and payload["phone"] == "600000000"


@pytest.mark.parametrize(("facts", "description"), [
    ({"area_m2": 3208}, "Parcela de 3.208 m²"),
    ({"area_m2": 3500}, "Parcela de 3.208mts"),  # within twice
    ({"area_m2": 150}, "Casa en parcela de 1.000 m²"),  # the house on a larger plot
    ({"area_m2": 1000}, "Vivienda de 150 m² construidos"),  # the plot of a smaller house
    ({"plot_m2": 1000, "area_m2": 150}, "Chalet de 150 m2 en parcela de 1.000 m2"),
    ({"plot_m2": 1000}, "Casa de 150 m² construidos"),  # no plot in the text: nothing to compare with
    ({"area_m2": 32088}, "Finca a 200 mts de la playa"),  # a distance, not an area
    ({"area_m2": 32088}, "Finca rústica, consulte superficie"),  # no area in the text
    ({"price": 1}, "Parcela de 3.208 m²"),  # no area in the line
    ({"area_m2": 32088}, "Finca de 3,2 ha"),  # hectares
    # the review of Q13: parts of the object, its surroundings, the buildable area, distances
    ({"area_m2": 90}, "Piso con terraza de 15 m²"),
    ({"area_m2": 90}, "Piso de 90 m², terraza de 15 m²"),
    ({"area_m2": 85}, "Piso con balcón de 4 m²"),
    ({"area_m2": 85}, "Piso de 3 dormitorios, salón de 25 m²"),
    ({"area_m2": 90}, "Piso con trastero de 6 m² y garaje 12 m2"),
    ({"area_m2": 300}, "Local, salón de 40 m², cocina de 12 m²"),
    ({"area_m2": 250}, "Chalet con 1.500 m² de parcela"),
    ({"area_m2": 250}, "Chalet con 1.500 m² de jardín"),
    ({"area_m2": 250}, "Chalet con jardín de 600 m²"),
    ({"area_m2": 5000}, "Nave con oficinas de 300 m²"),
    ({"area_m2": 80}, "Piso, urbanización de 20.000 m² con piscina"),
    ({"area_m2": 80}, "Piso en urbanización con zonas comunes de 5.000 m2"),
    ({"plot_m2": 3650}, "Solar, edificabilidad de 12.000 m² de techo"),
    ({"area_m2": 3650}, "Solar urbano de 3.650 m2, edificabilidad 4.232 m² techo"),
    ({"area_m2": 85}, "Piso, metro a 100 mts, colegio 300 mts"),
    ({"area_m2": 32088}, "Finca, playa 200 mts"),
    ({"area_m2": 32088}, "Finca con 500 mts de fachada"),
    ({"area_m2": 32088, "plot_m2": 3208}, "Parcela de 3.208 m²"),  # the JSON-LD plot itself agrees
])
def test_areas_that_agree_or_are_missing_are_no_conflict(facts, description):
    assert area_check.conflict(portal_post(facts, "Finca", description), "Finca") is None


@pytest.mark.parametrize(("facts", "description"), [
    ({"surface": 32088}, "parcela de 3.208mts"),
    ({"area_m2": 32088, "property_type": "landparcel"}, "La parcela es de 3.208mts"),
    ({"plot_m2": 32088}, "Parcela de 3.208 m²"),
    ({"area_m2": 32088}, "Finca de 3.208 m², casa de 120 m²"),  # neither the plot nor the house
    ({"area_m2": 90}, "Piso de 300 m² útiles"),
    ({"area_m2": 5000}, "Terreno de 1.200 m2"),
    ({"area_m2": 3}, "Piso de 85 m²"),
])
def test_areas_more_than_twice_apart_are_a_conflict(facts, description):
    assert area_check.conflict(portal_post(facts, "Finca", description), "Finca") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(("area", "description"), [
    (90, "Piso exterior con terraza de 15 m²"),
    (85, "Piso con balcón de 4 m²"),
    (250, "Chalet con 1.500 m² de parcela"),
    (5000, "Nave industrial con oficinas de 300 m²"),
    (80, "Piso en urbanización de 20.000 m² con piscina"),
])
async def test_review_cases_keep_the_area_with_the_flag(monkeypatch, area, description):
    text = portal_post({"price": 300000, "area_m2": area, "address": "Chamberí, Madrid"}, "Anuncio", description)
    answer = {**ANSWER, "area_m2": area, "evidence": {**ANSWER["evidence"], "area": str(area)}}
    result, _ = await analyze(monkeypatch, text, answer=answer, area_text_check=True)
    assert result.area_m2 == area and result.area_unconfirmed is None and "area not confirmed" not in result.reason


def test_area_flag_is_off_by_default_and_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("ANALYSIS_AREA_TEXT_CHECK", raising=False)
    assert AnalysisSettings(_env_file=None).area_text_check is False
    monkeypatch.setenv("ANALYSIS_AREA_TEXT_CHECK", "true")
    assert AnalysisSettings(_env_file=None).area_text_check is True


# -- without the flags: as before, line for line --


@pytest.mark.asyncio
async def test_without_flags_the_request_and_the_answer_are_as_before(monkeypatch):
    text = portal_post(MOSTOLES_FACTS, MOSTOLES_TITLE, MOSTOLES_TEXT)
    before, sent_before = await analyze(monkeypatch, text)
    after, sent_after = await analyze(monkeypatch, text, location_from_text=False, area_text_check=False)
    assert sent_after == sent_before and after == before
    assert after == verify_facts(parse_result(json.dumps(ANSWER)), evidence(text))
    assert (after.location, after.district, after.area_m2) == ("Ciudad Universitaria, Madrid", "Ciudad Universitaria",
                                                                32088)
    assert after.source_place_conflict is None and after.area_unconfirmed is None and after.reason == "offer"
    payload = finding_payload(after, evidence(text))
    assert "source_place_conflict" not in payload and "area_unconfirmed" not in payload


# -- 4. agent reductions (review Q4b, T2) --


def test_agent_reduction_has_no_null_keys_of_ours():
    from bot.agents.extraction import parse_extraction

    data = parse_extraction(json.dumps({**ANSWER, "red_flags": [], "contact_present": True,
                                        "extraction_confidence": 0.8}))
    assert "source_place_conflict" not in data and "area_unconfirmed" not in data
    assert data["location"] == ANSWER["location"] and data["area_m2"] == 32088
