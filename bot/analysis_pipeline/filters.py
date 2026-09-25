from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from .models import Evidence, FilterDecision

RELEVANCE = {
    "real_estate": (
        # en
        "rent",
        "apartment",
        "property",
        "house",
        "flat",
        "room",
        "for sale",
        # es
        "alquiler",
        "alquilo",
        "piso",
        "vivienda",
        "habitaci",
        "estudio",
        "venta",
        "vendo",
        # ru / uk (stems: сдаю, сдаётся, сниму, продаю, продаётся, комната, квартира ...)
        "аренд",
        "квартир",
        "недвижим",
        "комнат",
        "сда",
        "сним",
        "снять",
        "прода",
        "жиль",
        "студи",
        "апартамент",
        "оренд",
        "кімнат",
        "житл",
    ),
    "investors": (
        "invest",
        "funding",
        "investor",
        "startup",
        "capital",
        "инвест",
        "стартап",
        "invers",
        "інвест",
    ),
}
SPAM = ("guaranteed profit", "click here", "free crypto", "http://bit.ly")
LOCATIONS = (
    "madrid",
    "barcelona",
    "valencia",
    "españa",
    "espana",
    "spain",
    "kyiv",
    "kiev",
    "мадрид",
    "барселон",
    "валенси",
    "испани",
    "іспані",
    "киев",
    "київ",
    "украин",
    "україн",
)


def detect_language(text: str) -> str:
    lower = text.lower()
    if re.search(r"[а-яіїєґ]", lower):
        return "ru"
    if any(x in lower for x in (" el ", " la ", " piso", " alquiler", " en ")):
        return "es"
    if re.search(r"[a-z]", lower):
        return "en"
    return "unknown"


def filter_evidence(
    evidence: Evidence, vertical: str, *, now: datetime | None = None, max_age_days: int = 90
) -> FilterDecision:
    text = evidence.text.strip()
    language = detect_language(text)
    if len(text) < 30:
        return FilterDecision(accepted=False, reason="insufficient_content", language=language)
    if evidence.published_at and evidence.published_at < (now or datetime.now(UTC)) - timedelta(
        days=max_age_days
    ):
        return FilterDecision(accepted=False, reason="stale", language=language)
    low = text.lower()
    if any(marker in low for marker in SPAM):
        return FilterDecision(accepted=False, reason="spam_signal", language=language)
    if not any(keyword in low for keyword in RELEVANCE[vertical]):
        return FilterDecision(accepted=False, reason="irrelevant_keywords", language=language)
    # A post in a city's group rarely repeats the city; the group title (Evidence.title) counts.
    place_text = f"{low} {evidence.title.lower()}"
    if vertical == "real_estate" and not any(place in place_text for place in LOCATIONS):
        return FilterDecision(accepted=False, reason="missing_location_signal", language=language)
    return FilterDecision(accepted=True, reason="accepted", language=language)
