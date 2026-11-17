"""Q74: comarcas as the task's districts (PLACE_COMARCA_MEMBERSHIP, off by default). Sim b7-g4-s8 («участок в
провинции Аликанте, районы: Вега-Баха, Марина-Альта»): finds in Aspe, Villena, Finestrat... were «Не подтверждено:
место» 31 times and went to similar. Mocks only, no network.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from bot.analysis_pipeline.formatters import source_line_facts
from bot.campaign import comarcas
from bot.campaign.relevance import review_match
from bot.campaign.tolerance import Request, classify, comarca_membership_enabled, request_for
from bot.web_search.queries import QueryTask
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources.portals import ApifyFotocasaSource
from bot.web_search.worker import WebSearchConfig, WebSearchWorker
from tests.test_reviewer import settings

LAND = {"deal_type": "sale", "property_type": "land", "price_amount": 99000, "price_currency": "EUR",
        "area_m2": 11907.0, "listing_kind": "offer", "country": "ES"}
G4 = {"deal": "sale", "currency": "EUR", "min_area": 5000, "districts": "Vega Baja, Marina Alta", "max_price": 300000,
      "property_type": "land"}
TEXT = "покупка до 300000 € Тип: участок. Задача: учаток в провинции Аликанте, от 5000 кв.м, купить."


def g4(monkeypatch, flag: bool = True, districts: str = "Vega Baja, Marina Alta", nearby: list[str] | None = None,
       location: str = "Alicante") -> Request:
    monkeypatch.setenv("PLACE_COMARCA_MEMBERSHIP", "true" if flag else "false")
    return request_for({**G4, "districts": districts}, location=location, vertical="real_estate", country="ES",
                       text=TEXT, deviations={"nearby_areas": nearby} if nearby else None)


def finding(location: str, **extra) -> dict:
    return {**LAND, "location": location, **extra}


def review(place: str) -> dict:
    return {"overall": {"pass": "match", "unknown": "near", "fail": "reject"}[place],
            "criteria": [{"name": "place", "verdict": place}, {"name": "type", "verdict": "pass"}]}


# --- the G4 scenario --------------------------------------------------------------------------------------------

def test_sim_orihuela_is_the_place_and_the_reviewer_unknown_does_not_hold_it(monkeypatch):
    request = g4(monkeypatch)
    assert request.comarcas == ("Vega Baja", "Marina Alta")
    match = classify(finding("Molins - Campaneta - San Bartolomé, Orihuela, Alicante"), request)
    assert (match.bucket, match.confirmed) == ("exact", ("place",))
    near = review_match(match, review("unknown"))  # was «Не подтверждено: место»
    assert (near.bucket, near.why, near.note) == ("similar", "ai", None)  # the reviewer's near stays near
    assert review_match(match, review("pass")).bucket == "exact"


@pytest.mark.parametrize("location", ["Pedanías Este, Aspe, Alicante", "Finestrat Pueblo, Finestrat, Alicante",
                                      "Las Tiesas, Villena, Alicante", "Las Bayas, Elche / Elx, Alicante",
                                      "Ronesa, Tibi, Alicante", "El Pinós / Pinoso, Alicante", "Relleu, Alicante",
                                      "Barrio del Centro, Alicante / Alacant, Alicante"])
def test_sim_a_town_of_another_comarca_is_another_place(monkeypatch, location):
    match = classify(finding(location), g4(monkeypatch))
    assert (match.bucket, match.why) == ("other", "location")
    assert review_match(match, review("unknown")).bucket == "other"


@pytest.mark.parametrize("location", ["Partida Los Llanos, Alicante", "Alicante", "Benimagrell", None])
def test_an_unknown_town_is_unchanged(monkeypatch, location):
    """A town not in the table (or only the province's name): the old rule, the reviewer's unknown still holds it."""
    match = classify(finding(location), g4(monkeypatch))
    assert (match.bucket, match.confirmed) == ("exact", ())
    held = review_match(match, review("unknown"))
    assert (held.bucket, held.note) == ("similar", "Не подтверждено: место")


def test_fotocasa_comarca_is_a_direct_answer(monkeypatch):
    request = g4(monkeypatch)
    inside = classify(finding("Partida Los Llanos, Alicante", comarca="Vega Baja"), request)
    assert (inside.bucket, inside.confirmed) == ("exact", ("place",))
    outside = classify(finding("Partida Los Llanos, Alicante", comarca="Vinalopó Mitjà"), request)
    assert (outside.bucket, outside.why) == ("other", "location")
    # «Valencia, Zona de» (Fotocasa's level3Name of the capital) is no comarca: the town decides
    assert classify(finding("Orihuela, Alicante", comarca="Valencia, Zona de"), request).confirmed == ("place",)


def test_towns_of_two_comarcas_or_a_shared_town_are_not_guessed(monkeypatch):
    request = g4(monkeypatch)
    assert classify(finding("Torrevieja, Benidorm"), request).confirmed == ()  # two comarcas: unknown
    assert classify(finding("Torrevieja, Benidorm"), request).bucket == "exact"
    sax = g4(monkeypatch, districts="Vinalopó Mitjà")
    assert classify(finding("Sax, Alicante"), sax).confirmed == ("place",)
    assert classify(finding("Sax, Alicante"), g4(monkeypatch, districts="Alt Vinalopó")).confirmed == ("place",)


def test_the_address_names_the_town_when_the_location_does_not(monkeypatch):
    match = classify(finding("Alicante", address="Calle Mayor 3, Dénia"), g4(monkeypatch))
    assert match.confirmed == ("place",)


# --- neighbouring areas -----------------------------------------------------------------------------------------

def test_an_approved_neighbouring_comarca_still_passes(monkeypatch):
    request = g4(monkeypatch, nearby=["Марина-Баха"])
    match = classify(finding("Finestrat Pueblo, Finestrat, Alicante"), request)
    assert (match.bucket, match.why, match.note) == ("exact", "approved_deviation", "район Марина-Баха (соседний)")
    assert match.covers == ("place",)
    assert review_match(match, review("fail")).bucket == "exact"  # «not in Vega Baja / Marina Alta»: approved
    assert review_match(match, review("unknown")).why == "ai"  # the place is not unknown, the near stays
    assert classify(finding("Pedanías Este, Aspe, Alicante"), request).bucket == "other"  # not the approved one


def test_an_approved_neighbouring_town_still_passes(monkeypatch):
    match = classify(finding("Las Bayas, Elche / Elx, Alicante"), g4(monkeypatch, nearby=["Elche"]))
    assert (match.bucket, match.why, match.note) == ("exact", "approved_deviation", "район Elche (соседний)")


# --- when it applies --------------------------------------------------------------------------------------------

def test_a_town_among_the_districts_keeps_the_old_rule(monkeypatch):
    request = g4(monkeypatch, districts="Vega Baja, Torrevieja")
    assert request.comarcas == ()
    assert classify(finding("Pedanías Este, Aspe, Alicante"), request).bucket == "exact"


def test_another_province_keeps_the_old_rule(monkeypatch):
    request = g4(monkeypatch, location="Madrid", districts="Marina Alta")
    assert request.comarcas == ()


@pytest.mark.parametrize(("name", "key"), [
    ("Вега-Баха", "Vega Baja"), ("Вега Баха", "Vega Baja"), ("Vega Baja del Segura", "Vega Baja"),
    ("Baix Segura", "Vega Baja"), ("la Vega Baja", "Vega Baja"), ("Comarca de la Vega Baja", "Vega Baja"),
    ("Марина-Альта", "Marina Alta"), ("La Marina Alta", "Marina Alta"), ("Marina Baja", "Marina Baixa"),
    ("Марина-Баха", "Marina Baixa"), ("L'Alacantí", "Alacantí"), ("Alto Vinalopó", "Alt Vinalopó"),
    ("Vinalopó Medio", "Vinalopó Mitjà"), ("Bajo Vinalopó", "Baix Vinalopó"), ("El Comtat", "Comtat"),
    ("Hoya de Alcoy", "Alcoià"), ("Madrid", None), ("Marina", None), ("", None),
])
def test_comarca_synonyms(name, key):
    assert comarcas.comarca_of(name) == key


def test_synonyms_in_the_task(monkeypatch):
    request = g4(monkeypatch, districts="Вега-Баха, Марина-Альта")
    assert request.comarcas == ("Vega Baja", "Marina Alta")
    assert classify(finding("Jávea / Xàbia, Alicante"), request).confirmed == ("place",)
    assert classify(finding("Oriola"), request).confirmed == ("place",)  # the Valencian name
    assert classify(finding("Finestrat"), request).bucket == "other"


def test_the_table_has_the_nine_comarcas_and_agrees_with_fotocasa():
    assert len(comarcas.MUNICIPALITIES) == 9 and set(comarcas.MUNICIPALITIES) == set(comarcas.COMARCA_NAMES)
    # level3Name / level5Name pairs of the recorded Fotocasa output (sim b7, province of Alicante)
    for comarca, town in (("Alacantí", "Agost"), ("Alacantí", "Alicante / Alacant"),
                          ("Alacantí", "San Vicente del Raspeig / Sant Vicent del Raspeig"), ("Alcoià", "Alcoy / Alcoi"),
                          ("Alt Vinalopó", "Sax"), ("Alt Vinalopó", "Villena"), ("Baix Vinalopó", "Elche / Elx"),
                          ("Marina Alta", "Jávea / Xàbia"), ("Marina Alta", "Pego"), ("Marina Baixa", "Finestrat"),
                          ("Marina Baixa", "Orxeta"), ("Vega Baja", "Daya Nueva"), ("Vega Baja", "Orihuela"),
                          ("Vinalopó Mitjà", "Aspe"), ("Vinalopó Mitjà", "El Pinós / Pinoso")):
        assert comarca in comarcas.comarcas_of_place(town), town


# --- flag off ---------------------------------------------------------------------------------------------------

def test_flag_off_as_before(monkeypatch):
    request = g4(monkeypatch, flag=False)
    assert request.comarcas == ()
    for location in ("Pedanías Este, Aspe, Alicante", "Orihuela, Alicante"):
        match = classify(finding(location, comarca="Vega Baja"), request)
        assert (match.bucket, match.confirmed) == ("exact", ())
        assert review_match(match, review("unknown")).note == "Не подтверждено: место"


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("PLACE_COMARCA_MEMBERSHIP", raising=False)
    assert comarca_membership_enabled() is False and Request().comarcas == ()
    assert settings().place_comarca_membership is False
    assert settings(PLACE_COMARCA_MEMBERSHIP="true").place_comarca_membership is True
    assert WebSearchSettings(_env_file=None).place_comarca_membership is False
    assert ApifyFotocasaSource("secret-token").comarca is False


# --- the Fotocasa row's comarca on the facts line ---------------------------------------------------------------

LAND_TASK = QueryTask("terreno", "terreno", "Alicante", {}, "real_estate", constraints={"deal": "sale",
                      "property_type": "land"}, country_code="ES", place_level="province")


def fotocasa_row(**location):
    return {"url": "https://www.fotocasa.es/es/comprar/terreno/orihuela/molins/100000074/d", "propertyType": "7",
            "transaction": {"type": "SALE", "price": 99000}, "surface": 11907, "agency": {"name": "Agencia"},
            "location": {"level2Name": "Alicante", "level3Name": "Vega Baja", "level5Name": "Orihuela",
                         "latitude": "38.08", "longitude": "-0.94", **location}, "description": "Parcela rústica."}


def line_facts(listing) -> dict:
    text = WebSearchWorker._source_text(SimpleNamespace(config=WebSearchConfig()), listing)
    return json.loads(text.split("\n", 1)[0].removeprefix("JSON-LD: ")), text


def test_flag_on_fotocasa_comarca_on_the_facts_line():
    listing = ApifyFotocasaSource("secret-token", comarca=True)._normalize(fotocasa_row(), LAND_TASK, None)
    facts, text = line_facts(listing)
    assert listing.comarca == "Vega Baja" and facts["comarca"] == "Vega Baja"
    assert source_line_facts(text)["comarca"] == "Vega Baja"


def test_flag_off_no_comarca_on_the_facts_line():
    listing = ApifyFotocasaSource("secret-token")._normalize(fotocasa_row(), LAND_TASK, None)
    facts, text = line_facts(listing)
    assert listing.comarca is None and "comarca" not in facts and "comarca" not in source_line_facts(text)
    empty = ApifyFotocasaSource("secret-token", comarca=True)._normalize(fotocasa_row(level3Name=None), LAND_TASK, None)
    assert empty.comarca is None


@pytest.mark.asyncio
async def test_settings_pass_the_flag_to_fotocasa():
    sources = WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token", APIFY_FOTOCASA_ENABLED=True,
                                PLACE_COMARCA_MEMBERSHIP=True).sources()
    try:
        assert [s.comarca for s in sources if s.name == "apify_fotocasa"] == [True]
    finally:
        for source in sources:
            await source.aclose()


def test_a_confirmed_place_leaves_the_other_unknowns_and_a_wish_near(monkeypatch):
    match = classify(finding("Orihuela, Alicante"), g4(monkeypatch))
    water = {"overall": "near", "criteria": [{"name": "place", "verdict": "unknown"},
                                             {"name": "must_have:вода", "verdict": "unknown"}]}
    held = review_match(match, water)
    assert (held.bucket, held.note) == ("similar", "Не подтверждено: обязательно: вода")
    wish = {**review("unknown"), "near_reason": "wish"}
    assert review_match(match, wish).bucket == "similar"


@pytest.mark.parametrize("near_reason", ["other", None])
def test_an_adosado_for_a_detached_house_stays_near(monkeypatch, near_reason):
    """REVIEW_TOWNHOUSE_IS_HOUSE: «adosado» for «отдельный дом» is type pass, overall near («таунхаус»); the place
    the comarca confirmed does not make it a match."""
    request = g4(monkeypatch)
    match = classify({**finding("Orihuela, Alicante"), "property_type": "house"},
                     replace(request, property_type="house"))
    assert match.confirmed == ("place",)
    adosado = {"overall": "near", "deviation_ru": "таунхаус", "near_reason": near_reason,
               "criteria": [{"name": "place", "verdict": "unknown"}, {"name": "type", "verdict": "pass"}]}
    held = review_match(match, adosado)
    assert (held.bucket, held.why) == ("similar", "ai")


def test_fotocasa_comarca_against_the_table_is_unknown(monkeypatch):
    """Fotocasa's level3Name that the table does not give the town (Sax, a portal's typo): not «another place»."""
    request = g4(monkeypatch)
    for location, comarca in (("Aspe, Alicante", "Vega Baja"), ("Orihuela, Alicante", "Marina Baixa"),
                              ("Sax, Alicante", "Baix Vinalopó")):
        match = classify(finding(location, comarca=comarca), request)
        assert (match.bucket, match.confirmed) == ("exact", ()), location
    sax = g4(monkeypatch, districts="Vinalopó Mitjà")
    assert classify(finding("Sax, Alicante", comarca="Alt Vinalopó"), sax).confirmed == ("place",)  # both are Sax's


def test_torremanzanas_is_in_alacanti():
    assert comarcas.comarcas_of_place("La Torre de les Maçanes, Alicante") == {"Alacantí"}
    assert comarcas.comarcas_of_place("Torremanzanas / La Torre de les Maçanes") == {"Alacantí"}
    assert sum(len(set(towns) - {"Orihuela Costa", "Ciudad Quesada", "Moraira", "Teulada-Moraira"})
               for towns in comarcas.MUNICIPALITIES.values()) == 142  # 141 municipalities, Sax in two comarcas


# --- dedup: the comarca of one portal is no difference -----------------------------------------------------------

DESC = ("Parcela rústica en Orihuela con agua de riego, pozo propio, casa de aperos, acceso asfaltado, a diez "
        "minutos del pueblo, ideal para cultivo de cítricos. ") * 3
FOTOCASA_URL = "https://www.fotocasa.es/es/comprar/terreno/orihuela/molins/100000074/d"
IDEALISTA_URL = "https://www.idealista.com/inmueble/100000075/"


def _post(url: str, comarca: str | None) -> str:
    line = {"url": url, "title": "Terreno en venta", "price": 99000.0, "currency": "EUR", "plot_m2": 11907.0,
            "address": "Molins, Orihuela, Alicante"}
    if comarca:
        line["comarca"] = comarca
    return (f"JSON-LD: {json.dumps(line, ensure_ascii=False, separators=(', ', ': '))}\nTerreno en venta\n"
            f"Ссылка: {url}\n{DESC}")


def test_dedup_ignores_the_comarca_of_the_facts_line():
    from bot.campaign.dedup import listing_of

    payload = {"price_amount": 99000, "plot_m2": 11907, "property_type": "land", "deal_type": "sale",
               "location": "Orihuela"}
    on = listing_of(payload, url=FOTOCASA_URL, body=_post(FOTOCASA_URL, "Vega Baja"))
    off = listing_of(payload, url=FOTOCASA_URL, body=_post(FOTOCASA_URL, None))
    assert on.shingles == off.shingles and on.words == off.words and on.numbers == off.numbers
    assert on.description == off.description
    other = listing_of(payload, url=IDEALISTA_URL, body=_post(IDEALISTA_URL, None))
    assert on.shingles == other.shingles  # the Jaccard of the two copies is as without the flag
