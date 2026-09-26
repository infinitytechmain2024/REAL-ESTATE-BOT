"""How close a finding is to the campaign's request: exact, similar or other.

One policy, pure functions, no I/O. The runner files every finding of a
campaign in exactly one bucket (``campaign_findings.bucket``, migration 019):

* ``exact`` -- matches the request; its card is sent at once, as always.
* ``similar`` -- a little over budget; held until the requester answers
  «Одобрить» to one question, never sent on «Нет».
* ``other`` -- farther away (much more expensive, unknown price when a budget
  was given, another city, the other deal type); held for an optional second
  question once the search has finished.

Budget
------
The plan's ``max_price`` is the requested amount (intake and the architect
turn «до 1200 €» and a bare «1200 €» into it; the currency is always EUR).

* ``BUDGET_TOLERANCE`` (10 %): the exact band is the amount ± 10 %. A budget
  given as a maximum («до 50 000») has no lower edge: anything cheaper than the
  budget is exactly what was asked for, so exact means price ≤ 55 000.
* ``SIMILAR_CEILING`` (25 %): above the exact band up to +25 % of the amount
  (55 000 < price ≤ 62 500 for 50 000) is similar. For a target amount (not a
  maximum) the same distances apply below it too.
* Anything farther is other. A listing without a usable price is exact when no
  budget was given (price does not matter then) and other when one was.
* A price in another currency cannot be compared, so it is other. A listing
  that does not name a currency is taken to be in the requested one.

Other constraints
-----------------
* Deal type: when both the request and the listing name one (rent/sale) and
  they differ, the listing is other.
* Location: the listing's location mentions a known city (the architect's
  gazetteer) and not the requested one -> other. An unknown or unrecognised
  location never downgrades a listing.
* Rooms are shown on the card but do not change the bucket.
* Findings of the investors vertical carry no prices; they are always exact.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Literal

from .architect import find_places

BUDGET_TOLERANCE = 0.10
SIMILAR_CEILING = 0.25
DEFAULT_CURRENCY = "EUR"

Bucket = Literal["exact", "similar", "other"]
BUCKETS: tuple[Bucket, ...] = ("exact", "similar", "other")
HeldBucket = Literal["similar", "other"]
HELD_BUCKETS: tuple[HeldBucket, ...] = ("similar", "other")

_CURRENCY_ALIASES = {
    "€": "EUR", "EURO": "EUR", "EUROS": "EUR", "ЕВРО": "EUR", "ЄВРО": "EUR",
    "$": "USD", "US$": "USD", "£": "GBP", "₽": "RUB", "РУБ": "RUB", "₴": "UAH", "ГРН": "UAH",
}
_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "RUB": "₽", "UAH": "₴"}


@dataclass(frozen=True, slots=True)
class Request:
    """What the campaign asked for, as far as bucketing is concerned."""

    amount: int | None = None
    is_max: bool = True  # «до N»: anything cheaper is exact
    currency: str = DEFAULT_CURRENCY
    deal: str | None = None
    location: str | None = None  # a gazetteer canonical name


@dataclass(frozen=True, slots=True)
class Match:
    bucket: Bucket
    # Relative distance from the requested amount (0.2 = 20 % away); inf when unknown.
    distance: float = 0.0


def request_for(constraints: dict[str, Any], *, location: str | None = None, vertical: str | None = None) -> Request:
    """The bucketing request of a campaign plan (``plan.constraints``, ``plan.location``)."""
    if vertical == "investors":
        return Request(location=location)
    amount = constraints.get("max_price")
    deal = constraints.get("deal")
    return Request(
        amount=amount if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0 else None,
        deal=deal if deal in ("rent", "sale") else None,
        location=location,
    )


def currency_code(value: object) -> str | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    return _CURRENCY_ALIASES.get(text, text)


def price_of(payload: dict[str, Any] | None) -> float | None:
    amount = (payload or {}).get("price_amount")
    if isinstance(amount, int | float) and not isinstance(amount, bool) and math.isfinite(amount) and amount > 0:
        return float(amount)
    return None


def classify(payload: dict[str, Any] | None, request: Request, *, vertical: str | None = None) -> Match:
    """Put one finding in exactly one bucket (see the module notes for the rules)."""
    if vertical == "investors":
        return Match("exact")
    payload = payload or {}
    deal = payload.get("deal_type")
    if request.deal and deal in ("rent", "sale") and deal != request.deal:
        return Match("other", math.inf)
    if request.location and isinstance(payload.get("location"), str):
        places = find_places(payload["location"])
        if places and request.location not in places:
            return Match("other", math.inf)
    if request.amount is None:
        return Match("exact")
    price = price_of(payload)
    currency = currency_code(payload.get("price_currency")) or request.currency
    if price is None or currency != request.currency:
        return Match("other", math.inf)
    return budget_match(price, request.amount, is_max=request.is_max)


def budget_match(price: float, amount: int, *, is_max: bool = True) -> Match:
    """The budget rule alone: ±``BUDGET_TOLERANCE`` exact, up to ``SIMILAR_CEILING`` similar."""
    offset = (price - amount) / amount
    distance = abs(offset)
    if is_max and offset <= 0:
        return Match("exact", 0.0)
    if distance <= BUDGET_TOLERANCE + 1e-9:
        return Match("exact", distance)
    if distance <= SIMILAR_CEILING + 1e-9:
        return Match("similar", distance)
    return Match("other", distance)


def money(amount: float, currency: str = DEFAULT_CURRENCY) -> str:
    """«~60 000 €»: a rounded amount for the question text."""
    whole = round(amount)
    return f"~{whole:,}".replace(",", " ") + f" {_SYMBOLS.get(currency, currency)}"
