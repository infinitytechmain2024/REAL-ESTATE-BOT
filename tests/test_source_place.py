"""Q4 (ANALYSIS_LOCATION_FROM_TEXT): a portal's JSON-LD place is not trusted when the listing's own text names another.

Mocks only: the live habitaclia rows of 2026-10-16 (``tests/fixtures/portals_live``) and ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
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


@pytest.mark.asyncio
async def test_with_flag_the_mijas_plot_loses_the_madrid_place(monkeypatch):
    result, data, sent = await analyze(monkeypatch, MIJAS, flag=True)
    assert LOCATION_FROM_TEXT_RULE in sent["messages"][1]["content"]
    assert "Barrio de Salamanca" not in data["text"].split("\n", 1)[0] and "Mijas" in data["text"]
    assert result.location == "Costa del Sol" and result.evidence["location"] == "Costa del Sol"
    assert result.district is None and result.address is None
    assert "source location 'Barrio de Salamanca, Madrid Capital' contradicts the text: 'Costa del Sol'" \
        in result.reason
    assert (result.price_amount, result.area_m2) == (1800000, 4140)  # the numbers of the source stay
    # a model that already took the place from the text keeps it
    good, _, _ = await analyze(monkeypatch, MIJAS, flag=True,
                               answer={**ANSWER, "location": "Mijas, Málaga", "district": None, "address": None})
    assert good.location == "Mijas, Málaga" and "contradicts the text" in good.reason


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

