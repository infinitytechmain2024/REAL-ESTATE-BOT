from __future__ import annotations

from typing import Any

from .cards import CardTask, render_card
from .models import AnalysisResult, Evidence

PAYLOAD_VERSION = "analysis-v3"


def finding_payload(result: AnalysisResult, evidence: Evidence) -> dict[str, Any]:
    """What ``findings.structured_payload`` stores (besides ``formatted``); the card is rebuilt from it."""
    return {
        "schema_version": PAYLOAD_VERSION,
        "summary": result.summary,
        "summary_ru": result.summary_ru,
        "source_language": result.source_language,
        "location": result.location,
        "price_signals": result.price_signals,
        "price_amount": result.price_amount,
        "price_currency": result.price_currency,
        "deal_type": result.deal_type,
        "property_type": result.property_type,
        "rooms": result.rooms,
        "who": result.who,
        "original_post_link": evidence.canonical_url,
        "related_links": result.related_links,
    }


def _card(result: AnalysisResult, evidence: Evidence, vertical: str, language: str | None) -> str:
    return render_card(
        finding_payload(result, evidence),
        original=evidence.text,
        task=CardTask(vertical=vertical),
        vertical=vertical,
        language=language,
        confidence=result.confidence,
    )


def real_estate(result: AnalysisResult, evidence: Evidence, language: str | None = None) -> str:
    return _card(result, evidence, "real_estate", language)


def investors(result: AnalysisResult, evidence: Evidence, language: str | None = None) -> str:
    return _card(result, evidence, "investors", language)


def digest(vertical: str, entries: list[str]) -> str:
    if not entries:
        return "Новых находок нет."
    return "\n\n".join(entries)
