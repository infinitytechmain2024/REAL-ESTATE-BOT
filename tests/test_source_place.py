"""Q4 (ANALYSIS_LOCATION_FROM_TEXT): a portal's JSON-LD place is not trusted when the listing's own text names another.

Mocks only: the live habitaclia rows of 2026-10-16 (``tests/fixtures/portals_live``) and ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from bot.analysis_pipeline import source_place
from bot.analysis_pipeline.models import Evidence
from bot.analysis_pipeline.openrouter import (
    INSTRUCTIONS,
    JSONLD_RULE,
    LOCATION_FROM_TEXT_RULE,
    OpenRouterAnalyzer,
    instructions,
)
from bot.analysis_pipeline.settings import AnalysisSettings
from bot.web_search.queries import QueryTask
from bot.web_search.sources.portals import ApifyHabitacliaSource
from bot.web_search.worker import WebSearchConfig, WebSearchWorker

LIVE = Path(__file__).parent / "fixtures" / "portals_live"
MIJAS = "https://www.habitaclia.com/i54590000000240.htm"
_REAL_CLIENT = httpx.AsyncClient


def habitaclia_texts() -> dict[str, tuple[str, str]]:
    """url -> (title, post text) exactly as the web worker writes a habitaclia listing for the analysis."""
    rows = json.loads((LIVE / "habitaclia_items.json").read_text(encoding="utf-8"))
    task = QueryTask("terreno", "terreno", "Madrid", {}, "real_estate",
                     constraints={"deal": "sale", "min_area": "2000"}, country_code="ES")
    adapter = ApifyHabitacliaSource("secret-token")
    worker = SimpleNamespace(config=WebSearchConfig())
    found = {}
    for row in rows:
        listing = adapter._normalize(row, task, None)
        found[listing.url] = (listing.title, WebSearchWorker._source_text(worker, listing))
    return found


def post(text: str, title: str = "") -> str:
    return text if text.startswith("JSON-LD") else f'JSON-LD: {{"address": "Madrid Capital, Madrid"}}\n{title}\n{text}'


# -- the prompt --


def test_prompt_without_flag_is_the_old_one_and_with_flag_has_the_new_rule():
    old = instructions()
    assert old == instructions(False) == INSTRUCTIONS and JSONLD_RULE in old and LOCATION_FROM_TEXT_RULE not in old
    assert "copy price, currency, area, rooms, address and location from it" in old
    new = instructions(True)
    assert LOCATION_FROM_TEXT_RULE in new and JSONLD_RULE not in new
    assert "only its price, currency, area_m2/plot_m2 and rooms are authoritative" in new
    assert "explicitly names another municipality or province" in new and "never from the JSON-LD" in new
    assert "source location X contradicts the text: Y" in new
    assert new.replace(LOCATION_FROM_TEXT_RULE, JSONLD_RULE) == old  # nothing else of the prompt changes


def test_flag_is_off_by_default_and_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("ANALYSIS_LOCATION_FROM_TEXT", raising=False)
    assert AnalysisSettings(_env_file=None).location_from_text is False
    monkeypatch.setenv("ANALYSIS_LOCATION_FROM_TEXT", "true")
    assert AnalysisSettings(_env_file=None).location_from_text is True


# -- the deterministic check on the live rows --


def test_mijas_plot_filed_under_madrid_is_a_conflict():
    title, text = habitaclia_texts()[MIJAS]
    assert '"address": "Barrio de Salamanca, Madrid Capital"' in text and "Mijas" in text
    found = source_place.conflict(text, title)
    assert found is not None
    assert found.source == "Barrio de Salamanca, Madrid Capital" and found.source_areas == {"Madrid"}
    assert (found.phrase, found.area) == ("Costa del Sol", "Andalucía")
    # what the model reads: the numbers stay, the wrong place is gone
    shown = source_place.without_place(text)
    first = json.loads(shown.split("\n", 1)[0].removeprefix("JSON-LD: "))
    assert "address" not in first and first["price"] == 1800000 and first["plot_m2"] == 4140
    assert shown.split("\n", 1)[1] == text.split("\n", 1)[1]


def test_ordinary_madrid_plots_keep_their_place():
    texts = habitaclia_texts()
    assert len(texts) == 3
    for url, (title, text) in texts.items():
        if url != MIJAS:
            assert source_place.conflict(text, title) is None, url


@pytest.mark.parametrize("text", [
    "Piso exterior en calle Valencia, 3 dormitorios.",            # a street named after a city
    "Ático junto a la Glorieta de Bilbao con vistas a la Puerta de Toledo.",
    "Chalet a 30 min de Málaga capital, ideal familias.",          # a distance
    "Bien comunicado con la Costa del Sol por la A-7.",
    "Parcela en Boadilla del Monte, cerca del Real Madrid.",       # the source's own area is named
    "Terreno urbano en Mijas, junto al pueblo.",                   # a place the list does not know: the prompt decides
    "Piso reformado, 3 habitaciones, ascensor.",                   # no place at all
])
def test_no_conflict_without_a_clear_signal(text):
    assert source_place.conflict(post(text)) is None


def portal_post(address: str, title: str, description: str) -> str:
    """The layout of web_search/worker.py ``_source_text``: JSON-LD, title, «Ссылка:», description."""
    line = json.dumps({"price": 300000, "address": address}, ensure_ascii=False)
    return f"JSON-LD: {line}\n{title}\nСсылка: https://x.example/1\n{description}"


# The review cases of Q4: a city in a street, an agency, a trip, a style or someone else's place is not the object's.
@pytest.mark.parametrize(("address", "title", "description"), [
    ("Calle Mayor, Madrid", "Piso en venta", "Piso luminoso, a 10 min de Valencia"),
    ("Centro, Madrid Capital", "Piso", "a 10 minutos en coche de la ciudad de Valencia"),
    ("Chamberí, Madrid", "Piso en Chamberí", "Inmobiliaria Barcelona Homes, oficina en Barcelona. Piso reformado."),
    ("Salamanca, Madrid", "Piso exterior", "Costa del Sol Properties le ofrece este piso"),
    ("Barrio de Salamanca, Madrid Capital", "Ático", "Costa del Sol Realty. Ático con terraza"),
    ("Chamberí, Madrid", "Piso", "en calle de Valencia 12"),
    ("Retiro, Madrid", "Piso", "junto al estadio Santiago Bernabéu, Real Madrid"),
    ("Getafe, Madrid", "Nave", "ideal empresa logística con clientes en Barcelona y Valencia"),
    ("Alcobendas, Madrid", "Chalet", "Propietario se traslada a Málaga, vende urgente"),
    ("Valencia", "Piso en Gandía", "playa de Gandía"),
    ("Salamanca, Madrid", "Flat for sale", "Our Barcelona office handles the sale"),
    ("Malasaña, Madrid", "Loft", "estilo Barcelona, diseño catalán"),
    ("Atocha, Madrid", "Piso", "a 2 pasos del AVE a Sevilla y Barcelona"),
    ("Centro, Madrid", "Piso", "propietario valenciano"),
    ("Retiro, Madrid", "Piso", "Avda. Ciudad de Barcelona 30"),
    ("Sant Antoni, Barcelona", "Pis", "Carrer de Madrid 5"),
    ("Sant Antoni, Barcelona", "Pis a l'Eixample", "Pis en Carrer de Madrid 5, Avinguda de Sevilla"),
    ("Ruzafa, Valencia", "Piso", "C/ Sevilla 3"),
    ("Málaga", "Piso", "Calle Madrid, Málaga"),
    ("Málaga", "Piso", "Avenida de Andalucía"),
    ("Ciudad Universitaria, Madrid", "Casa en Girona", "Masía en Girona"),
    ("Ciudad Universitaria, Madrid", "Piso en Móstoles", "Piso en Móstoles centro"),
    # the same words in a portal's own title
    ("Chamberí, Madrid", "Piso a 10 min de la ciudad de Valencia", "Piso reformado"),
    ("Chamberí, Madrid", "Piso en Carrer de Barcelona", "Piso reformado"),
    ("Chamberí, Madrid", "Piso en passeig Barcelona", "Piso reformado"),
    ("Chamberí, Madrid", "Piso en plaça Catalunya", "Piso reformado"),
    ("Chamberí, Madrid", "Piso junto al AVE a Sevilla", "Piso reformado"),
    ("Chamberí, Madrid", "Piso cerca del tren a Valencia", "Piso reformado"),
    ("Chamberí, Madrid", "Inmobiliaria Barcelona Homes vende piso", "Piso reformado"),
    ("Chamberí, Madrid", "Oficina en Barcelona vende", "Piso reformado"),
    ("Chamberí, Madrid", "Agencia Málaga Living", "Piso reformado"),
    ("Chamberí, Madrid", "Loft estilo Barcelona", "Piso reformado"),
    ("Chamberí, Madrid", "Nave con clientes en Barcelona", "Piso reformado"),
    ("Chamberí, Madrid", "Propietario se traslada a Málaga", "Piso reformado"),
    # a title we build ourselves from the actor's place (portals.py: Fotocasa, Habitaclia) is not the portal's
    ("Chamberí, Madrid", "Piso en venta, Barcelona", "Piso reformado"),
    ("Chamberí, Madrid", "Vivienda en alquiler, Málaga", "Piso reformado"),
    ("Chamberí, Madrid", "Terreno en venta", "Parcela urbana"),
    # the description counts only right after «en», «ubicado en», «situado en», «se encuentra en»
    ("Chamberí, Madrid", "Piso", "Barcelona y Valencia, a la vista desde la terraza"),
])
def test_review_cases_are_no_conflict(address, title, description):
    assert source_place.conflict(portal_post(address, title, description), title) is None


@pytest.mark.parametrize(("title", "description", "phrase", "city"), [
    ("Parcela en Mijas, Costa del Sol", "Terreno con vistas", "Costa del Sol", None),
    ("Piso", "Se encuentra en Marbella, junto al mar", "Marbella", "Marbella"),
    ("Piso", "Situado en la Costa del Sol", "Costa del Sol", None),
    ("Piso", "Terreno ubicado en Mijas, Málaga", "Málaga", "Málaga"),
    ("Casa en Girona", "Masía en Girona, Catalunya", "Catalunya", None),
    ("Parcela… ½ hectárea en Marbella №5", "", "Marbella", "Marbella"),  # «…», «½», «№» keep the positions
])
def test_a_place_named_by_the_text_is_a_conflict(title, description, phrase, city):
    found = source_place.conflict(portal_post("Barrio de Salamanca, Madrid Capital", title, description), title)
    assert found is not None and (found.phrase, found.city) == (phrase, city) and found.source_areas == {"Madrid"}


def test_fold_keeps_one_character_for_one():
    text = "Parcela… ½ ha, № 5, Málaga, ﬁnca"
    assert len(source_place.fold(text)) == len(text) and "malaga" in source_place.fold(text)


def test_no_conflict_without_jsonld_or_with_an_unknown_source_place():
    assert source_place.conflict("Terreno en la Costa del Sol") is None
    assert source_place.conflict('JSON-LD: {"address": "Getafe"}\nTerreno en la Costa del Sol') is None
    assert source_place.conflict('JSON-LD: {broken\nTerreno en la Costa del Sol') is None


def test_one_area_is_never_a_conflict_with_itself():
    marbella = 'JSON-LD: {"address": "Marbella, Málaga"}\nVilla en Sevilla? No: villa en la Costa del Sol'
    assert source_place.conflict(marbella) is None
    alicante = 'JSON-LD: {"address": "Alicante"}\nChalet en la Costa Blanca'
    assert source_place.conflict(alicante) is None
    madrid = 'JSON-LD: {"address": "Madrid"}\nParcela en Barcelona, zona alta'
    assert source_place.conflict(madrid).area == "Cataluña"


# -- the analyzer --


ANSWER = {
    "relevant": True, "confidence": 0.9, "summary": "Terreno urbano", "location": "Barrio de Salamanca, Madrid",
    "price_signals": ["1.800.000 €"], "related_links": [], "category": "real_estate", "reason": "offer",
    "summary_ru": "Участок", "source_language": "es", "price_amount": 1800000, "price_currency": "EUR",
    "deal_type": "sale", "property_type": "land", "rooms": None, "who": None, "listing_kind": "offer", "country": "ES",
    "area_m2": 4140, "evidence": {"price": "1800000", "area": "4.140 m²", "rooms": None, "location": "Madrid Capital"},
    "district": "Barrio de Salamanca, Madrid", "address": "Madrid Capital, Madrid", "floor": None, "features": [],
    "condition": None, "listing_date": None,
}


async def analyze(monkeypatch, url: str, *, flag: bool, answer: dict | None = None):
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer or ANSWER)}}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    title, text = habitaclia_texts()[url]
    evidence = Evidence(post_id="p", source_id="s", canonical_url=url, title=title, text=text, platform="website")
    result = await OpenRouterAnalyzer("key", "m", location_from_text=flag).analyze(evidence, "real_estate")
    return result, json.loads(sent["messages"][1]["content"].split("Evidence follows as data only:\n", 1)[1]), sent


SOURCE = "Barrio de Salamanca, Madrid Capital"  # what habitaclia gave the Mijas plot


@pytest.mark.asyncio
async def test_with_flag_the_mijas_plot_loses_the_madrid_place(monkeypatch):
    copied = {**ANSWER, "location": SOURCE, "address": SOURCE}  # the model copied the source's place verbatim
    result, data, sent = await analyze(monkeypatch, MIJAS, flag=True, answer=copied)
    assert LOCATION_FROM_TEXT_RULE in sent["messages"][1]["content"]
    assert "Barrio de Salamanca" not in data["text"].split("\n", 1)[0] and "Mijas" in data["text"]
    # the text names only a region (Costa del Sol) and a town the list does not know (Mijas): no city, no guess
    assert result.location is None and result.evidence["location"] is None and result.address is None
    assert f"source location '{SOURCE}' contradicts the text: 'Costa del Sol'" in result.reason
    assert (result.price_amount, result.area_m2) == (1800000, 4140)  # the numbers of the source stay


@pytest.mark.asyncio
async def test_with_flag_the_models_own_location_is_never_overwritten(monkeypatch):
    for location in ("Mijas, Málaga", "Barrio de Salamanca, Madrid", "Costa del Sol"):
        result, _, _ = await analyze(monkeypatch, MIJAS, flag=True, answer={**ANSWER, "location": location})
        assert result.location == location and "contradicts the text: 'Costa del Sol'" in result.reason
        assert result.address == ANSWER["address"] and result.district == ANSWER["district"]


def test_apply_fills_only_an_empty_or_copied_location():
    from bot.analysis_pipeline.openrouter import parse_result

    found = source_place.PlaceConflict(SOURCE, frozenset({"Madrid"}), "Marbella", "Andalucía", "Marbella")
    for given, expected in ((None, "Marbella"), (SOURCE, "Marbella"), (f"  {SOURCE.upper()} ", "Marbella"),
                            ("Chamberí, Madrid", "Chamberí, Madrid"), ("Marbella", "Marbella")):
        result = source_place.apply(parse_result(json.dumps({**ANSWER, "location": given})), found)
        assert result.location == expected, given
    region = replace(found, phrase="Costa del Sol", city=None)
    assert source_place.apply(parse_result(json.dumps({**ANSWER, "location": None})), region).location is None


def test_a_barcelona_agency_does_not_move_a_madrid_flat():
    title = "Piso en Chamberí"
    text = portal_post("Chamberí, Madrid", title, "Inmobiliaria Barcelona Homes. Piso reformado de 90 m².")
    assert source_place.conflict(text, title) is None


@pytest.mark.asyncio
async def test_mijas_is_excluded_by_place_in_a_madrid_campaign(monkeypatch):
    from bot.analysis_pipeline.formatters import finding_payload
    from bot.campaign.tolerance import classify, request_for

    request = request_for({"deal": "sale", "min_area": 2000, "max_price": 3000000}, location="Madrid",
                          vertical="real_estate", country="ES")
    title, text = habitaclia_texts()[MIJAS]
    evidence = Evidence(post_id="p", source_id="s", canonical_url=MIJAS, title=title, text=text, platform="website")
    # the new rule: the model takes the place from the text
    result, _, _ = await analyze(monkeypatch, MIJAS, flag=True,
                                 answer={**ANSWER, "location": "Mijas, Málaga", "district": None, "address": None})
    assert classify(finding_payload(result, evidence), request).why == "location"
    # the old rule: the model copies the JSON-LD place, and the Mijas plot passes as a Madrid one
    old, _, _ = await analyze(monkeypatch, MIJAS, flag=False, answer={**ANSWER, "location": SOURCE})
    assert classify(finding_payload(old, evidence), request).why != "location"
    # an ordinary Madrid plot keeps its place
    url = next(u for u in habitaclia_texts() if u != MIJAS)
    madrid, _, _ = await analyze(monkeypatch, url, flag=True, answer={**ANSWER, "location": "Moncloa - Aravaca, Madrid"})
    title, text = habitaclia_texts()[url]
    own = Evidence(post_id="p", source_id="s", canonical_url=url, title=title, text=text, platform="website")
    assert classify(finding_payload(madrid, own), request).why != "location"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["not json {", "", "[]"])
async def test_with_flag_an_answer_outside_the_schema_is_an_error_and_apply_is_not_called(monkeypatch, content):
    from bot.analysis_pipeline.openrouter import INVALID

    def boom(*args, **kwargs):
        raise AssertionError("apply must not run on an invalid answer")

    monkeypatch.setattr(source_place, "apply", boom)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))
    title, text = habitaclia_texts()[MIJAS]
    evidence = Evidence(post_id="p", source_id="s", canonical_url=MIJAS, title=title, text=text, platform="website")
    with pytest.raises(ValueError, match=INVALID):
        await OpenRouterAnalyzer("key", "m", location_from_text=True).analyze(evidence, "real_estate")


@pytest.mark.asyncio
async def test_prompt_version_is_marked_only_with_the_flag():
    from bot.analysis_pipeline.openrouter import PROMPT_VERSION, prompt_version
    from bot.analysis_pipeline.pipeline import AnalysisPipeline, metadata

    assert metadata("es", "m") == {"prompt_version": PROMPT_VERSION, "model": "m", "language": "es"}
    assert PROMPT_VERSION == "analysis-v6" and prompt_version() == "analysis-v6"
    assert metadata("es", "m", location_from_text=True) == {"prompt_version": "analysis-v6-loc", "model": "m",
                                                            "language": "es"}
    from bot.analysis_pipeline.openrouter import parse_result

    class Fake:
        def __init__(self, flag):
            self.location_from_text = flag

        async def analyze(self, evidence, vertical):
            return parse_result(json.dumps({**ANSWER, "relevant": True}))

    title, text = habitaclia_texts()[MIJAS]
    evidence = Evidence(post_id="p", source_id="s", canonical_url=MIJAS, title=title, text=text, platform="website")
    for flag in (True, False):
        outcome = await AnalysisPipeline(Fake(flag)).process(evidence, "real_estate")
        assert outcome.accepted and outcome.location_from_text is flag


@pytest.mark.asyncio
async def test_without_flag_nothing_changes(monkeypatch):
    result, data, sent = await analyze(monkeypatch, MIJAS, flag=False)
    assert JSONLD_RULE in sent["messages"][1]["content"] and LOCATION_FROM_TEXT_RULE not in sent["messages"][1]["content"]
    assert "Barrio de Salamanca" in data["text"].split("\n", 1)[0]
    assert result.location == ANSWER["location"] and result.reason == "offer"


@pytest.mark.asyncio
async def test_with_flag_a_madrid_plot_keeps_the_answer(monkeypatch):
    url = next(u for u in habitaclia_texts() if u != MIJAS)
    answer = {**ANSWER, "location": "Moncloa - Aravaca, Madrid", "price_amount": None, "area_m2": None}
    result, data, _ = await analyze(monkeypatch, url, flag=True, answer=answer)
    assert '"address": ' in data["text"].split("\n", 1)[0]
    assert result.location == "Moncloa - Aravaca, Madrid" and result.reason == "offer"

