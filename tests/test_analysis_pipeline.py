from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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
    assert (
        filter_evidence(
            evidence(
                text="Apartment for rent at 20 EUR with a modern kitchen and bright rooms for long stays"
            ),
            "real_estate",
        ).reason
        == "missing_location_signal"
    )


class DummyAnalyzer:
    calls = 0

    async def analyze(self, _e, vertical):
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
    assert accepted.accepted and a.calls == 1 and "Real Estate proposition" in accepted.formatted


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
    assert "Investor lead" in investors(r, evidence(comments=["Interested investor"]))
    assert digest("real_estate", []) == "No new real estate findings."
    assert "Real Estate proposition" in real_estate(
        r.model_copy(update={"category": "real_estate"}), evidence()
    )


def test_idempotency_key_is_stable():
    from bot.analysis_pipeline.pipeline import finding_key

    assert finding_key(evidence(), "real_estate") == finding_key(evidence(), "real_estate")
    assert finding_key(evidence(), "real_estate") != finding_key(evidence(), "investors")


def test_a_long_digest_is_split_into_telegram_sized_messages_not_truncated():
    from bot.analysis_pipeline.main import MAX_MESSAGE_CHARS, split_digest

    entries = [(f"f{n}", "x" * 1500) for n in range(7)]
    chunks = split_digest(entries)
    assert [fid for chunk in chunks for fid, _ in chunk] == [f"f{n}" for n in range(7)]
    assert all(sum(len(t) for _, t in chunk) + 2 * (len(chunk) - 1) <= MAX_MESSAGE_CHARS for chunk in chunks)
    assert len(chunks) == 4


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
    no_place = evidence(text="Сдаю комнату в районе Usera, 400 евро, для одной девушки, без животных", title="")
    assert filter_evidence(no_place, "real_estate").reason == "missing_location_signal"


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
