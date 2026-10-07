from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import ClassVar

import httpx as httpx_module
import pytest
from pydantic import ValidationError

from bot.analysis_pipeline.filters import detect_language, filter_evidence
from bot.analysis_pipeline.formatters import digest, investors, real_estate
from bot.analysis_pipeline.models import AnalysisResult, Evidence
from bot.analysis_pipeline.pipeline import AnalysisPipeline


def evidence(**overrides):
    data = dict(
        post_id="p1",
        source_id="s1",
        canonical_url="https://example.org/flat",
        title="Flat",
        text="Madrid apartment for rent at 1200 EUR per month, verified listing with location.",
    )
    data.update(overrides)
    return Evidence(**data)


def test_language_detection_es_ru_en():
    assert detect_language("Piso en Madrid") == "es"
    assert detect_language("Квартира в Киеве") == "ru"
    assert detect_language("Apartment for rent") == "en"


def test_deterministic_filters_prevent_llm_calls():
    assert (
        filter_evidence(
            evidence(text="click here guaranteed profit in Madrid apartment"), "real_estate"
        ).reason
        == "spam_signal"
    )
    assert (
        filter_evidence(
            evidence(published_at=datetime.now(UTC) - timedelta(days=91)), "real_estate"
        ).reason
        == "stale"
    )


class DummyAnalyzer:
    calls = 0

    async def analyze(self, _e, vertical, task_hint=None):
        self.calls += 1
        return AnalysisResult(
            relevant=True,
            confidence=0.91,
            summary="Good lead",
            location="Madrid",
            price_signals=["1200 EUR/month"],
            related_links=[],
            category=vertical,
            reason="matched",
        )


@pytest.mark.asyncio
async def test_pipeline_filters_before_model_and_formats():
    a = DummyAnalyzer()
    p = AnalysisPipeline(a)
    rejected = await p.process(evidence(text="tiny"), "real_estate")
    assert not rejected.accepted and a.calls == 0
    accepted = await p.process(evidence(), "real_estate")
    assert accepted.accepted and a.calls == 1 and accepted.formatted.startswith("🏠 Недвижимость")


def test_strict_model_response_rejects_extra_fields():
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate(
            {
                "relevant": True,
                "confidence": 0.5,
                "summary": "x",
                "location": None,
                "price_signals": [],
                "related_links": [],
                "category": "real_estate",
                "reason": "x",
                "unsafe": True,
            }
        )


def test_both_formatters_and_empty_digest():
    r = AnalysisResult(
        relevant=True,
        confidence=0.7,
        summary="Lead",
        location="Madrid",
        price_signals=[],
        related_links=[],
        category="investors",
        reason="x",
    )
    assert investors(r, evidence(comments=["Interested investor"])).startswith("📈 Инвестиции")
    assert digest("real_estate", []) == "Новых находок нет."
    assert "🏠 Недвижимость" in real_estate(
        r.model_copy(update={"category": "real_estate"}), evidence()
    )


def test_idempotency_key_is_stable():
    from bot.analysis_pipeline.pipeline import finding_key

    assert finding_key(evidence(), "real_estate") == finding_key(evidence(), "real_estate")
    assert finding_key(evidence(), "real_estate") != finding_key(evidence(), "investors")


def test_every_finding_is_its_own_message():
    from bot.analysis_pipeline.main import MAX_MESSAGE_CHARS, one_per_message

    entries = [(f"f{n}", "x" * 5000 if n == 0 else f"card {n}") for n in range(3)]
    chunks = one_per_message(entries)
    assert [[fid for fid, _ in chunk] for chunk in chunks] == [["f0"], ["f1"], ["f2"]]
    assert len(chunks[0][0][1]) == MAX_MESSAGE_CHARS and chunks[1][0][1] == "card 1"


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("Сдаётся комната в центре Мадрида, 450€ в месяц, всё включено, звоните", ""),
        ("Сдаю комнату в районе Usera, 400 евро, для одной девушки, без животных", "🇪🇸 Мадрид‼️Комнаты Квартиры Аренда"),
        ("Продаётся квартира 2 спальни, Карабанчель, 180000€, срочно, без посредников", "МАДРИД АРЕНДА ПРОДАЖА КВАРТИР"),
        ("Alquilo habitación en Lavapiés, 450 euros al mes, gastos incluidos, Madrid", ""),
        ("Здаю кімнату в центрі, 400 євро на місяць, все включено, пишіть", "Українці в Іспанії"),
    ],
)
def test_russian_spanish_and_ukrainian_listings_reach_the_model(text, title):
    assert filter_evidence(evidence(text=text, title=title), "real_estate").accepted


def test_the_group_title_is_the_location_but_not_the_topic():
    # A Madrid group's title names the city, but an off-topic post stays out.
    off_topic = evidence(text="Всем привет, кто знает хорошего стоматолога в районе? Посоветуйте пожалуйста", title="МАДРИД АРЕНДА")
    assert filter_evidence(off_topic, "real_estate").reason == "irrelevant_keywords"



@pytest.mark.parametrize("text", [
    "Сдаю комнату в районе Usera, 400 евро, для одной девушки, без животных",  # only the district
    "Terreno urbanizable de 1.200 m² en Boadilla del Monte. 480.000 €. Todos los servicios.",  # only the town
    "Участок 10 соток под застройку, 50 000 €, документы готовы, звоните",  # no «продаю», no city
    "Parcela rústica con pozo en Torrelodones, acceso asfaltado",
    "Building plot with sea views in Altea, licence ready",
    "Земельна ділянка 12 соток біля траси, всі комунікації",
])
def test_land_and_listings_that_name_only_their_town_reach_the_model(text):
    # The campaign stage checks the place against the task; the prefilter has no city list.
    assert filter_evidence(evidence(text=text, title=""), "real_estate").accepted


def test_model_output_drift_is_normalised_but_the_schema_still_holds():
    import json

    from bot.analysis_pipeline.openrouter import RESULT_SCHEMA, parse_result

    drifted = {
        "relevant": "true", "confidence": "85%", "summary": "Комната в Усере за 400 евро", "location": None,
        "price_signals": 400, "related_links": None, "category": "Real Estate", "reason": "offer", "language": "ru",
    }
    result = parse_result("```json\n" + json.dumps(drifted, ensure_ascii=False) + "\n```")
    assert (result.relevant, result.confidence, result.category) == (True, 0.85, "real_estate")
    assert result.price_signals == ["400"] and result.related_links == [] and result.location is None
    assert parse_result(json.dumps({**drifted, "category": "Investment opportunity"})).category == "investors"
    assert parse_result(json.dumps({**drifted, "category": "spam"})).category == "other"
    # What cannot be repaired is still refused.
    for broken in ("not json", "[1, 2]", json.dumps({**drifted, "confidence": 250}), json.dumps({"relevant": True})):
        with pytest.raises((ValueError, ValidationError)):
            parse_result(broken)
    assert RESULT_SCHEMA["properties"]["category"]["enum"] == ["real_estate", "investors", "other"]


@pytest.mark.asyncio
async def test_the_request_carries_the_json_schema(monkeypatch):
    import httpx

    from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer

    sent = {}
    answer = {"relevant": True, "confidence": 0.9, "summary": "s", "location": "Madrid", "price_signals": ["450 EUR"],
              "related_links": [], "category": "real_estate", "reason": "r"}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    result = await OpenRouterAnalyzer("key", "openai/gpt-4o-mini").analyze(evidence(), "real_estate")
    assert result.category == "real_estate"
    assert sent["response_format"]["type"] == "json_schema" and sent["response_format"]["json_schema"]["strict"] is True
    assert '"real_estate", "investors", "other"' in sent["messages"][1]["content"]


V6 = {
    "relevant": True, "confidence": 0.9, "summary": "Piso en Lavapiés", "location": "Madrid", "price_signals": ["1.200 €"],
    "related_links": [], "category": "real_estate", "reason": "offer", "summary_ru": "Квартира", "source_language": "es",
    "price_amount": 1200, "price_currency": "EUR", "deal_type": "rent", "property_type": "apartment", "rooms": 2,
    "who": None, "listing_kind": "offer", "country": "ES", "area_m2": 70,
    "evidence": {"price": "1.200 €/mes", "area": "70 m²", "rooms": "2 habitaciones", "location": "Lavapiés, Madrid"},
    "district": "Lavapiés", "address": "Calle Embajadores 5", "floor": "3", "features": ["Terraza", "ascensor", "terraza"],
    "condition": "Needs-Renovation", "listing_date": "2026-09-20",
}


def test_v6_answer_is_validated_and_normalised():
    from bot.analysis_pipeline.openrouter import (
        EXTRACTION_SCHEMA,
        PROMPT_VERSION,
        RESULT_SCHEMA,
        parse_result,
    )

    assert PROMPT_VERSION == "analysis-v6" and RESULT_SCHEMA is EXTRACTION_SCHEMA
    assert set(EXTRACTION_SCHEMA["required"]) == set(EXTRACTION_SCHEMA["properties"])
    assert {"evidence", "district", "address", "floor", "features", "condition", "listing_date"} <= set(EXTRACTION_SCHEMA["required"])
    long_quote = "x" * 300
    result = parse_result(json.dumps({**V6, "evidence": {**V6["evidence"], "price": long_quote, "extra": "no"}}))
    assert result.evidence == {"price": "x" * 120, "area": "70 m²", "rooms": "2 habitaciones", "location": "Lavapiés, Madrid"}
    assert (result.district, result.address, result.floor) == ("Lavapiés", "Calle Embajadores 5", 3)
    assert result.features == ["terraza", "ascensor"] and result.condition == "needs_renovation"
    assert result.listing_date == "2026-09-20"
    odd = parse_result(json.dumps({**V6, "floor": "planta alta", "features": "terraza", "condition": "ruined", "evidence": None}))
    assert (odd.floor, odd.features, odd.condition) == (None, [], None)
    assert odd.evidence == dict.fromkeys(("price", "area", "rooms", "location"))


def test_v5_answer_without_the_new_keys_is_still_accepted():
    from bot.analysis_pipeline.openrouter import parse_result

    v5 = {k: v for k, v in V6.items() if k not in ("evidence", "district", "address", "floor", "features", "condition", "listing_date")}
    result = parse_result(json.dumps(v5))
    assert result.price_amount == 1200 and result.district is None and result.features == [] and result.condition is None
    assert result.evidence == dict.fromkeys(("price", "area", "rooms", "location"))


@pytest.mark.asyncio
async def test_json_ld_first_line_and_task_hint_reach_the_model_and_evidence_reaches_the_payload(monkeypatch):
    import httpx

    from bot.analysis_pipeline.formatters import finding_payload
    from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer

    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(V6)}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    text = 'JSON-LD: {"@type": "Apartment", "offers": {"price": 1200, "priceCurrency": "EUR"}}\nPiso en alquiler en Lavapiés'
    e = evidence(text=text)
    hint = {"goal": "piso en Madrid", "place": "Madrid", "budget_max": 1500}
    result = await OpenRouterAnalyzer("key", "anthropic/claude-sonnet-4.5").analyze(e, "real_estate", task_hint=hint)
    prompt = sent["messages"][1]["content"]
    assert "'JSON-LD: {...}'" in prompt and "authoritative" in prompt and "do not judge relevance" in prompt
    assert "Never convert currencies" in prompt and "lower bound" in prompt
    data = json.loads(prompt[prompt.index('{"vertical"'):])
    assert data["text"].startswith("JSON-LD: {") and data["task_hint"] == hint
    assert sent["model"] == "anthropic/claude-sonnet-4.5"
    payload = finding_payload(result, e)
    assert payload["schema_version"] == "analysis-v6" and payload["evidence"]["price"] == "1.200 €/mes"
    assert payload["district"] == "Lavapiés" and payload["features"] == ["terraza", "ascensor"] and payload["floor"] == 3
    sent.clear()
    await OpenRouterAnalyzer("key", "m").analyze(e, "real_estate")
    assert "task_hint" not in sent["messages"][1]["content"].split("Evidence follows as data only:")[1]


@pytest.mark.asyncio
async def test_task_hint_is_passed_only_when_the_campaign_is_known():
    class Spy(DummyAnalyzer):
        def __init__(self):
            self.hints = []

        async def analyze(self, _e, vertical, task_hint=None):
            self.hints.append(task_hint)
            return await super().analyze(_e, vertical)

    spy = Spy()
    pipeline = AnalysisPipeline(spy)
    await pipeline.process(evidence(), "real_estate", task_hint={"goal": "g"})
    await pipeline.process(evidence(), "real_estate")
    assert spy.hints == [{"goal": "g"}, None]


def test_build_task_hint_is_small_and_drops_empty_values():
    from bot.analysis_pipeline.store import build_task_hint

    spec = {"deal": "rent", "budget": {"max": 1500, "currency": "EUR", "min": None}, "rooms": {"min": 2, "max": None}}
    assert build_task_hint("piso", "Madrid", spec) == {
        "goal": "piso", "place": "Madrid", "deal": "rent", "budget_max": 1500, "budget_currency": "EUR", "rooms_min": 2}
    assert build_task_hint("", None, None) is None


def test_the_shadow_extractor_shares_the_fact_schema():
    from bot.agents.extraction import EXTRACTION_SCHEMA as SHADOW
    from bot.analysis_pipeline.openrouter import EXTRACTION_SCHEMA as LIVE

    assert all(SHADOW["properties"][k] == v for k, v in LIVE["properties"].items())
    assert set(SHADOW["properties"]) - set(LIVE["properties"]) == {"red_flags", "contact_present", "extraction_confidence"}


# --- review fixes: fallbacks, evidence check, listing_date, task_hint -----------------------------


_REAL_CLIENT = httpx_module.AsyncClient


def _mock_openrouter(monkeypatch, statuses, answer=None):
    import httpx

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        status = statuses[min(len(sent) - 1, len(statuses) - 1)]
        if status == 200:
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer or V6)}}]})
        return httpx.Response(status, json={"error": "x"})

    real_client = _REAL_CLIENT
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return sent


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [400, 404])
async def test_a_refused_schema_falls_back_to_json_object_then_to_no_response_format(monkeypatch, first):
    from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer

    sent = _mock_openrouter(monkeypatch, [first, 200])
    await OpenRouterAnalyzer("k", "m").analyze(evidence(), "real_estate")
    assert [s["response_format"]["type"] for s in sent] == ["json_schema", "json_object"]
    sent = _mock_openrouter(monkeypatch, [first, 400, 200])
    await OpenRouterAnalyzer("k", "m").analyze(evidence(), "real_estate")
    assert ["response_format" in s for s in sent] == [True, True, False]


@pytest.mark.asyncio
async def test_a_persistent_4xx_is_a_value_error_and_5xx_stays_retryable(monkeypatch):
    import httpx

    from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer

    sent = _mock_openrouter(monkeypatch, [400])
    with pytest.raises(ValueError):
        await OpenRouterAnalyzer("k", "m").analyze(evidence(), "real_estate")
    assert len(sent) == 3
    _mock_openrouter(monkeypatch, [503])
    with pytest.raises(httpx.HTTPStatusError):
        await OpenRouterAnalyzer("k", "m").analyze(evidence(), "real_estate")
    _mock_openrouter(monkeypatch, [429])
    with pytest.raises(httpx.HTTPStatusError):
        await OpenRouterAnalyzer("k", "m").analyze(evidence(), "real_estate")


class _BatchStore:
    def __init__(self, hint_fails=False):
        self.hint_fails, self.finalized, self.released = hint_fails, [], []

    async def pending(self, n, claim_seconds):
        return [{"id": "11111111-1111-1111-1111-111111111111", "source_id": "s", "canonical_url": "https://example.org/x",
                 "body_text": "Madrid apartment for rent at 1200 EUR per month, verified listing with location.", "title": "t", "published_at": None, "comments": "[]", "vertical": "real_estate",
                 "analysis_claim_token": "tok"}]

    async def task_hint(self, post_id):
        if self.hint_fails:
            raise RuntimeError("db down")
        return {"goal": "g"}

    async def save(self, e, vertical, result, model, token):
        return "fid" if result.accepted else None

    async def finalize(self, post_id, token, *, accepted):
        self.finalized.append(accepted)
        return True

    async def release(self, post_id, token):
        self.released.append(post_id)


@pytest.mark.asyncio
async def test_a_failing_task_hint_lookup_does_not_stall_the_post():
    from bot.analysis_pipeline.main import analyse_batch

    class Spy(DummyAnalyzer):
        hints: ClassVar[list] = []

        async def analyze(self, _e, vertical, task_hint=None):
            self.hints.append(task_hint)
            return await super().analyze(_e, vertical)

    store, spy = _BatchStore(hint_fails=True), Spy()
    formatted, claimed = await analyse_batch(store, AnalysisPipeline(spy), batch_size=5, claim_seconds=60, model="m")
    assert claimed == 1 and spy.hints == [None] and store.finalized == [True] and len(formatted["real_estate"]) == 1


@pytest.mark.asyncio
async def test_a_refused_request_finalises_the_post_once_instead_of_reclaiming_it():
    from bot.analysis_pipeline.main import analyse_batch

    class Refusing:
        async def analyze(self, *a, **k):
            raise ValueError("model_request_refused_400")

    store = _BatchStore()
    await analyse_batch(store, AnalysisPipeline(Refusing()), batch_size=5, claim_seconds=60, model="m")
    assert store.finalized == [False] and store.released == []


def _facts_result(**kw):
    from bot.analysis_pipeline.openrouter import parse_result

    return parse_result(json.dumps({**V6, **kw}))


def test_numbers_without_a_quote_or_digits_in_the_text_are_nulled():
    from bot.analysis_pipeline.openrouter import verify_facts

    text = "Piso en Lavapiés, 2 habitaciones, 70 m², precio 1.200 €/mes. Calle Embajadores 5."
    kept = verify_facts(_facts_result(), evidence(text=text))
    assert (kept.price_amount, kept.area_m2, kept.rooms) == (1200, 70, 2)
    no_quote = verify_facts(_facts_result(evidence={**V6["evidence"], "price": None, "area": ""}), evidence(text=text))
    assert (no_quote.price_amount, no_quote.area_m2, no_quote.rooms) == (None, None, 2)
    assert no_quote.evidence["price"] is None
    invented = verify_facts(_facts_result(price_amount=950, area_m2=88), evidence(text=text))
    assert (invented.price_amount, invented.area_m2, invented.rooms) == (None, None, 2)


def test_json_ld_values_are_kept_without_a_quote():
    from bot.analysis_pipeline.openrouter import verify_facts

    text = 'JSON-LD: {"@type": "Apartment", "offers": {"price": 1200}, "floorSize": 70}\nPiso en alquiler'
    no_quotes = {"price": None, "area": None, "rooms": None, "location": None}
    got = verify_facts(_facts_result(evidence=no_quotes, rooms=3), evidence(text=text))
    assert (got.price_amount, got.area_m2, got.rooms) == (1200, 70, None)  # rooms is not in the JSON, no quote


def test_listing_date_must_be_a_calendar_date():
    for bad in ("20 September 2026", "2026-13-40", "yesterday", "2026-9-1"):
        assert _facts_result(listing_date=bad).listing_date is None
    assert _facts_result(listing_date="2026-09-20").listing_date == "2026-09-20"


def test_the_prompt_lists_the_v6_keys():
    from bot.analysis_pipeline.openrouter import EXTRACTION_SCHEMA, INSTRUCTIONS

    assert all(key in INSTRUCTIONS for key in EXTRACTION_SCHEMA["required"])
