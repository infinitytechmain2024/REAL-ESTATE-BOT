"""Deterministic facts extracted from listing text for degraded responses."""

from __future__ import annotations

import re

_PRICE = re.compile(
    r"(?P<symbol>€|\$|£)\s?(?P<amount>\d[\d\s.,]*)|"
    r"(?P<amount2>\d[\d\s.,]*)\s?(?P<currency>EUR|USD|GBP|€|\$|£)", re.I
)
_AREA = re.compile(r"\b(\d[\d\s.,]*)\s*(m²|m2|sqm|sq\.?\s*m|м²|м2|кв\.?\s*м)\b", re.I)
_CONTACT = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})")


def extract_listing_facts(text: str) -> dict[str, object]:
    """Return only values explicitly present in *text*; never infer missing facts."""
    facts: dict[str, object] = {}
    price = _PRICE.search(text)
    if price:
        raw = (price.group("amount") or price.group("amount2") or "").strip()
        currency = price.group("symbol") or price.group("currency")
        facts["price"] = f"{raw} {currency}".strip()
        facts["price_currency"] = {"€": "EUR", "$": "USD", "£": "GBP"}.get(
            currency.upper(), currency.upper()
        )
        number = re.sub(r"[^\d]", "", raw)
        if number:
            facts["price_value"] = float(number)
    area = _AREA.search(text)
    if area:
        facts["area"] = area.group(0).strip()
    contacts = list(dict.fromkeys(match.strip() for match in _CONTACT.findall(text)))
    if contacts:
        facts["contacts"] = contacts
    return facts
