from __future__ import annotations

from .models import AnalysisResult, Evidence


def real_estate(result: AnalysisResult, evidence: Evidence) -> str:
    links = "\n".join(f"• {x}" for x in [evidence.canonical_url, *result.related_links][:6])
    prices = ", ".join(result.price_signals) or "not stated"
    return f"🏠 Real Estate proposition\nLocation: {result.location or 'not stated'}\nPrice signals: {prices}\nConfidence: {result.confidence:.0%}\n\n{result.summary}\n\nLinks:\n{links}"


def investors(result: AnalysisResult, evidence: Evidence) -> str:
    comments = (
        "\n".join(f"• {x[:300]}" for x in evidence.comments[:5])
        or "• No relevant comments collected"
    )
    profile = (
        f"\nPublic profile context: {evidence.profile_extract}" if evidence.profile_extract else ""
    )
    return f"📈 Investor lead\nConfidence: {result.confidence:.0%}\n\n{result.summary}\n\nOriginal post: {evidence.canonical_url}\nRelevant comments:\n{comments}{profile}"


def digest(vertical: str, entries: list[str]) -> str:
    if not entries:
        return f"No new {vertical.replace('_', ' ')} findings."
    return "\n\n".join(entries)
