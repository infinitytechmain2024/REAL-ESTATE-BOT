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
        "se vende",
        # land, houses, commercial (es / en)
        "terreno",
        "parcela",
        "solar",
        "finca",
        "casa",
        "chalet",
        "villa",
        "adosado",
        "nave",
        "local comercial",
        "inmueble",
        "land",
        "plot",
        "m2",
        "m²",
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
        # land, houses, commercial (ru / uk)
        "участ",
        "земл",
        "сотк",
        "соток",
        "дом",
        "вилл",
        "таунхаус",
        "коттедж",
        "офис",
        "склад",
        "помещени",
        "ділянк",
        "будин",
        "приміщен",
        "м²",
        "кв.м",
        "кв м",
    ),
    "investors": (
        "invest",
        "funding",
        "investor",
        "инвест",
        "стартап",
        "invers",
        "інвест",
    ),
}
SPAM = ("guaranteed profit", "click here", "free crypto", "http://bit.ly")


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
    # No place gate here: campaigns search any place in the world, and the campaign stage checks the
    # place against the task (``bot.campaign.tolerance`` and the AI relevance check). A fixed list of
    # cities dropped every listing that named only its town («Boadilla del Monte», «Usera»).
    return FilterDecision(accepted=True, reason="accepted", language=language)
