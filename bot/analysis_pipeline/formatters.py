from __future__ import annotations

import json
from typing import Any

from .cards import CardTask, card_extra_fields_enabled, render_card
from .models import AnalysisResult, Evidence

PAYLOAD_VERSION = "analysis-v6"


def source_line_facts(text: str) -> dict[str, Any]:
    """Facts of the post's first line «JSON-LD: {...}» (written from a portal's SourceListing); {} if absent or broken."""
    first = (text or "").lstrip().split("\n", 1)[0]
    if not first.startswith("JSON-LD:"):
        return {}
    try:
        data = json.loads(first.removeprefix("JSON-LD:"))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("plot_m2", "area_m2"):
        v = data.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            out[key] = v
    for key in ("phone", "advertiser"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            out[key] = v.strip()
    return out


def finding_payload(result: AnalysisResult, evidence: Evidence) -> dict[str, Any]:
    """What ``findings.structured_payload`` stores (besides ``formatted``); the card is rebuilt from it."""
    payload = _base_payload(result, evidence)
    if card_extra_fields_enabled():
        facts = source_line_facts(evidence.text)
        for key in ("plot_m2", "phone", "advertiser"):
            if key in facts:
                payload[key] = facts[key]
        if payload.get("area_m2") is None and "area_m2" in facts:
            payload["area_m2"] = facts["area_m2"]
            payload["area_source"] = "listing"
    return payload


def _base_payload(result: AnalysisResult, evidence: Evidence) -> dict[str, Any]:
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
        "listing_kind": result.listing_kind,
        "country": result.country,
        "area_m2": result.area_m2,
        "evidence": result.evidence,
        "district": result.district,
        "address": result.address,
        "floor": result.floor,
        "features": result.features,
        "condition": result.condition,
        "listing_date": result.listing_date,
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
        extra_fields=card_extra_fields_enabled(),
    )


def real_estate(result: AnalysisResult, evidence: Evidence, language: str | None = None) -> str:
    return _card(result, evidence, "real_estate", language)


def investors(result: AnalysisResult, evidence: Evidence, language: str | None = None) -> str:
    return _card(result, evidence, "investors", language)


def digest(vertical: str, entries: list[str]) -> str:
    if not entries:
        return "Новых находок нет."
    return "\n\n".join(entries)
