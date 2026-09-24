from __future__ import annotations

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
