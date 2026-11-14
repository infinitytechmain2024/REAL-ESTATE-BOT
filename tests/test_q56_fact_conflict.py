"""Q56 (ANALYSIS_FACT_CONFLICT_NULL): a JSON-LD number the title or description contradict is unknown, not shown.

Sim runs b4-g2-s5 and b5-g2-s6: fotocasa 191378001 (Santiago Rusiñol) and its habitaclia copy carry JSON-LD
``rooms: 1``, the habitaclia title says «Piso 1 habitación 1 hab. auxiliar», the description «Piso de 3
habitaciones»; the card said «Комнаты: 3» and noted nothing. With the flag the analysis prompt (``-fc``) makes such a
field null with both numbers in ``reason`` and no evidence, so the reviewer sees no rooms count. Off: byte for byte.
"""

from __future__ import annotations

import hashlib
import itertools
import json

import httpx
import pytest

from bot.agents import reviewer as rv
from bot.analysis_pipeline import openrouter
from bot.analysis_pipeline.formatters import finding_payload
from bot.analysis_pipeline.models import Evidence
from bot.analysis_pipeline.openrouter import (
    OpenRouterAnalyzer,
    instructions,
    parse_result,
    prompt_version,
    verify_facts,
)
from bot.analysis_pipeline.pipeline import AnalysisPipeline, metadata
from bot.analysis_pipeline.settings import AnalysisSettings
from bot.campaign.relevance import finding_data

# sha256 of instructions(location_from_text, agency_rule, investor_rule) on origin/main before Q56.
OLD_PROMPTS = {
    (False, False, False): "c7549ca4cdae5da892a7828c3bb33ac0e5a9064f227fd87369fe4b02643281a8",
    (False, False, True): "3d574728b9122bc8f648a61ed024e12d85c404e6eaa0faf4a9520413f4d625ce",
    (False, True, False): "c7549ca4cdae5da892a7828c3bb33ac0e5a9064f227fd87369fe4b02643281a8",
    (False, True, True): "3d574728b9122bc8f648a61ed024e12d85c404e6eaa0faf4a9520413f4d625ce",
    (True, False, False): "c6b235c351ab608818c9b711f1292c509427d48613d442d345a641cc701a6a3b",
    (True, False, True): "15b60fd66a2f7a6d6d02b6c6cc600d015a17dfe55d5c2c59689cbc850c16d11c",
    (True, True, False): "35240a7ca141250d3e384cded37b047e80fabfbce5a94bf2daf14d12ccbf28d1",
    (True, True, True): "ad3dafb9004064ca784cc6f6727e4caf0387525a6e576831fc04a130acba08d3",
}
OLD_VERSIONS = {(False, False): "analysis-v6", (False, True): "analysis-v6", (True, False): "analysis-v6-loc",
                (True, True): "analysis-v6-loc-agency"}
TEXT = ('JSON-LD: {"price": 320000, "currency": "EUR", "rooms": 1, "area_m2": 85, "property_type": "flat"}\n'
        "Piso de 3 habitaciones en la calle Santiago Rusiñol, Madrid. Exterior, ascensor, 85 m2 construidos.")
TITLE = "Piso 1 habitación 1 hab. auxiliar en venta en Santiago Rusiñol"
REASON = "JSON-LD rooms 1 vs text 3 habitaciones"


def ev() -> Evidence:
    return Evidence(post_id="p1", source_id="s1", canonical_url="https://www.fotocasa.es/es/comprar/vivienda/madrid/191378001/d",
                    title=TITLE, text=TEXT)


def answer(rooms: int | None, reason: str) -> dict:
    return {"relevant": True, "confidence": 0.9, "summary": "Piso en venta", "location": "Madrid", "price_signals": ["320.000 €"],
            "related_links": [], "category": "real_estate", "reason": reason, "summary_ru": "Квартира в Мадриде",
            "source_language": "es", "price_amount": 320000, "price_currency": "EUR", "deal_type": "sale",
            "property_type": "apartment", "rooms": rooms, "who": None, "listing_kind": "offer", "country": "ES",
            "area_m2": 85,
            "evidence": {"price": '"price": 320000', "area": '"area_m2": 85',
                         "rooms": None if rooms is None else '"rooms": 1', "location": None},
            "district": None, "address": "calle Santiago Rusiñol", "floor": None, "features": ["exterior", "ascensor"],
            "condition": None, "listing_date": None}


def mock_model(monkeypatch) -> list[dict]:
    """A stand-in model: follows the Q56 rule only when the prompt carries it, else copies the JSON-LD as before."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        rule = openrouter.FACT_CONFLICT_RULE in body["messages"][1]["content"]
        content = answer(None, REASON) if rule else answer(1, "flat for sale")
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content)}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return sent


def test_flag_off_keeps_every_prompt_and_version_byte_for_byte() -> None:
    assert AnalysisSettings(DATABASE_URL="postgresql://x").fact_conflict_null is False
    for flags, digest in OLD_PROMPTS.items():
        assert hashlib.sha256(instructions(*flags).encode()).hexdigest() == digest
        assert instructions(*flags, fact_conflict=False) == instructions(*flags)
    for (loc, agency), version in OLD_VERSIONS.items():
        assert prompt_version(loc, agency) == prompt_version(loc, agency, fact_conflict=False) == version
    assert instructions() is openrouter.INSTRUCTIONS


def test_flag_on_adds_the_rule_to_every_variant_and_a_new_version() -> None:
    for loc, agency, investor in itertools.product((False, True), repeat=3):
        old = instructions(loc, agency, investor)
        new = instructions(loc, agency, investor, fact_conflict=True)
        assert openrouter.FACT_CONFLICT_RULE not in old and new.count(openrouter.FACT_CONFLICT_RULE) == 1
        assert new.replace(openrouter.FACT_CONFLICT_RULE, "") == old
        # after the JSON-LD rule of the variant, before the evidence-quote sentence
        assert new.index(openrouter.FACT_CONFLICT_RULE) > new.index("'JSON-LD: {...}'")
        old_version = prompt_version(loc, agency, investor)
        version = prompt_version(loc, agency, investor, fact_conflict=True)
        assert version != old_version and "-fc" in version and len(version) <= 40
    assert "JSON-LD rooms 1 vs text 3 habitaciones" in openrouter.FACT_CONFLICT_RULE
    assert prompt_version(fact_conflict=True) == "analysis-v6-fc"
    assert prompt_version(True, True, True, True) == "analysis-v6-loc-agency-fc-isrc"
    assert prompt_version(True, False, False, True) == "analysis-v6-loc-fc"


def test_an_answer_with_null_rooms_and_the_reason_parses_and_stays_null() -> None:
    result = verify_facts(parse_result(json.dumps(answer(None, REASON))), ev())
    assert result.rooms is None and result.evidence["rooms"] is None and result.reason == REASON
    assert (result.price_amount, result.area_m2) == (320000, 85)  # the JSON-LD numbers the text agrees with stay


@pytest.mark.asyncio
async def test_flag_on_the_contested_rooms_count_reaches_the_reviewer_as_unknown(monkeypatch) -> None:
    sent = mock_model(monkeypatch)
    outcome = await AnalysisPipeline(OpenRouterAnalyzer("k", "m", fact_conflict_null=True)).process(ev(), "real_estate")
    assert outcome.accepted and outcome.fact_conflict and outcome.result.rooms is None
    assert openrouter.FACT_CONFLICT_RULE in sent[0]["messages"][1]["content"]
    assert metadata("es", "m", fact_conflict=outcome.fact_conflict)["prompt_version"] == "analysis-v6-fc"
    payload = finding_payload(outcome.result, ev())
    finding = finding_data(payload, original=TEXT, review=True, vertical="real_estate")
    assert finding["rooms"] is None and "rooms" not in finding["evidence"]
    # A rooms criterion the model leaves out (or fails with no quote) is unknown, and a match becomes near.
    review = rv.parse_review(json.dumps({"criteria": [{"name": "place", "verdict": "pass", "quote": "Madrid"},
                                                      {"name": "rooms", "verdict": "fail", "quote": None}],
                                         "overall": "match"}), expected=["place", "rooms"],
                             haystack=rv.haystack_of(finding))
    assert [c.verdict for c in review.criteria] == ["pass", "unknown"] and review.overall == "near"


@pytest.mark.asyncio
async def test_flag_off_the_old_prompt_and_the_json_ld_count_as_before(monkeypatch) -> None:
    sent = mock_model(monkeypatch)
    outcome = await AnalysisPipeline(OpenRouterAnalyzer("k", "m")).process(ev(), "real_estate")
    assert outcome.accepted and not outcome.fact_conflict and outcome.result.rooms == 1
    assert sent[0]["messages"][1]["content"].startswith(openrouter.INSTRUCTIONS)
    assert openrouter.FACT_CONFLICT_RULE not in sent[0]["messages"][1]["content"]
    assert metadata("es", "m")["prompt_version"] == "analysis-v6"
