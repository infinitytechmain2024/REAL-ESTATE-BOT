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
  they differ, the listing is excluded: filed, never offered, never sent (a
  buyer never wants rentals, even among the "farther" variants).
* Location: the listing's location mentions a known city (the architect's
  gazetteer) and not the requested one -> other. An unknown or unrecognised
  location never downgrades a listing.
* Another country (``geo``) -> excluded: the payload's ``country`` is not the
  requested city's, its location names a place clearly in another country
  (Москва, Минск ...), its link is under another country's TLD (``.ru``,
  ``.ua`` ...), or its price is in another currency than the country's (a
  Spanish campaign is in EUR).
* Not one concrete offer -> excluded: ``listing_kind`` (analysis-v4) is
  ``catalog`` (search results, a list of ads, average prices), ``wanted`` or
  ``other``; a summary that reads like a catalog («43 объявления», «средняя
  стоимость») too. A payload without ``listing_kind`` is an offer.
* Area: a minimum area in the task («от 2000 м²», ``min_area_of``) with a
  known ``area_m2``: from 90 % of it (the same ±10 % tolerance as the budget)
  it is exact, from 75 % similar, below that excluded. An unknown area never
  downgrades a listing.
* Rooms are shown on the card but do not change the bucket.
* Findings of the investors vertical carry no prices; they are always exact.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Literal

from . import geo
from .architect import find_places

BUDGET_TOLERANCE = 0.10
SIMILAR_CEILING = 0.25
DEFAULT_CURRENCY = "EUR"

AREA_TOLERANCE = 0.10  # within 10 % below the minimum area is still exact
AREA_SIMILAR_FLOOR = 0.75  # from 75 % of the minimum area: similar; below: excluded

Bucket = Literal["exact", "similar", "other", "excluded"]
_RANK = {"exact": 0, "similar": 1, "other": 2, "excluded": 3}
# Why a finding is not exact: price, area, location, deal, kind (not one offer), foreign, currency.
Why = Literal["price", "area", "location", "deal", "kind", "foreign", "currency", "ai"]
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
    min_area: float | None = None  # «от 2000 м²» in the task, square metres
    country: str | None = None  # ISO-2; derived from ``location`` when not given

    @property
    def country_code(self) -> str | None:
        return self.country or geo.country_of(self.location)


@dataclass(frozen=True, slots=True)
class Match:
    bucket: Bucket
    # Relative distance from the requested amount (0.2 = 20 % away); inf when unknown.
    distance: float = 0.0
    why: str | None = None  # the main deviation (``Why``) when not exact
    area: float | None = None  # the listing's area when it is the deviation


def worse(a: Match, b: Match) -> Match:
    """The farther of two verdicts on one listing (exact < similar < other < excluded)."""
    if _RANK[b.bucket] > _RANK[a.bucket]:
        return b
    if _RANK[b.bucket] == _RANK[a.bucket] and b.distance > a.distance:
        return b
    return a


_AREA = re.compile(
    r"(?<!\w)(?:от|від|не менее|не менше|минимум|мінімум|min(?:imum)?|from|at least|desde|m[aá]s de|>=|≥|>)\s*"
    r"(\d{1,3}(?:[ .,]\d{3})+|\d+(?:[.,]\d+)?)\s*(тыс\w*\.?|тис\w*\.?|k)?\s*"
    r"(м²|м2|кв\.?\s*м\w*|m²|m2|sq\.?\s*m|metros?(?:\s+cuadrados)?|квадрат\w*|сот\w*|га\b|ha\b|hect\w*|гект\w*)",
    re.IGNORECASE,
)


def min_area_of(text: str | None) -> float | None:
    """The minimum area a task asks for, in m²: «от 2000 м²», «≥ 1 500 m2», «від 20 соток», «от 1 га»."""
    for found in _AREA.finditer(text or ""):
        raw = found.group(1).replace(" ", "")
        if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
            number = float(re.sub(r"[.,]", "", raw))
        else:
            number = float(raw.replace(",", "."))
        if found.group(2):
            number *= 1000
        unit = found.group(3).casefold()
        if unit.startswith("сот"):
            number *= 100
        elif unit in ("га", "ha") or unit.startswith(("hect", "гект")):
            number *= 10_000
        if 1 <= number <= 100_000_000:
            return number
    return None


_CATALOG = re.compile(
    r"\b[1-9]\d+\s+(?:объявлени|предложени|оголошен|пропозиц|anuncios|listings|ads\b|results|resultados)|"
    r"средн\w+ (?:стоимост|цен)|середн\w+ (?:варт|цін)|"
    r"average price|precio medio",
    re.IGNORECASE,
)


def not_an_offer(payload: dict[str, Any], *, vertical: str | None = None) -> bool:
    """A catalog page, a «wanted» post or anything but one concrete offer (analysis-v4, or the summary's words)."""
    kind = payload.get("listing_kind")
    if kind == "catalog" or (vertical != "investors" and kind in ("wanted", "other")):
        return True
    text = " ".join(str(payload.get(k) or "") for k in ("summary_ru", "summary"))
    return bool(_CATALOG.search(text))


def foreign(payload: dict[str, Any], request: Request) -> Match | None:
    """Excluded when the listing is clearly in another country than the requested city (see the notes)."""
    country = request.country_code
    if not country:
        return None
    stated = payload.get("country")
    if isinstance(stated, str) and len(stated) == 2 and stated.upper() != country:
        return Match("excluded", math.inf, "foreign")
    location = payload.get("location")
    if isinstance(location, str):
        places = geo.place_countries(location)
        if places and country not in places:
            return Match("excluded", math.inf, "foreign")
    if geo.foreign_tld(geo.host_of_link(payload.get("original_post_link") or payload.get("url")), country):
        return Match("excluded", math.inf, "foreign")
    expected = geo.CURRENCY.get(country)
    currency = currency_code(payload.get("price_currency"))
    if expected and currency and currency != expected:
        return Match("excluded", math.inf, "currency")
    return None


def area_match(area: float, minimum: float) -> Match:
    """From 90 % of the minimum area exact, from 75 % similar, below that excluded."""
    ratio = area / minimum
    distance = max(0.0, 1 - ratio)
    if ratio >= 1 - AREA_TOLERANCE - 1e-9:
        return Match("exact", distance)
    if ratio >= AREA_SIMILAR_FLOOR - 1e-9:
        return Match("similar", distance, "area", area)
    return Match("excluded", distance, "area", area)


def request_for(constraints: dict[str, Any], *, location: str | None = None, vertical: str | None = None,
                text: str | None = None) -> Request:
    """The bucketing request of a campaign plan (``plan.constraints``, ``plan.location``; ``text``: the task)."""
    if vertical == "investors":
        return Request(location=location)
    amount = constraints.get("max_price")
    deal = constraints.get("deal")
    return Request(
        amount=amount if isinstance(amount, int) and not isinstance(amount, bool) and amount > 0 else None,
        deal=deal if deal in ("rent", "sale") else None,
        location=location,
        min_area=min_area_of(text),
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
    payload = payload or {}
    if not_an_offer(payload, vertical=vertical):
        return Match("excluded", math.inf, "kind")
    if vertical == "investors":
        return Match("exact")
    deal = payload.get("deal_type")
    if request.deal and deal in ("rent", "sale") and deal != request.deal:
        return Match("excluded", math.inf, "deal")
    elsewhere = foreign(payload, request)
    if elsewhere is not None:
        return elsewhere
    result = Match("exact")
    area = payload.get("area_m2")
    if request.min_area and isinstance(area, int | float) and not isinstance(area, bool) and math.isfinite(area) and area > 0:
        result = area_match(float(area), request.min_area)
        if result.bucket == "excluded":
            return result
    if request.location and isinstance(payload.get("location"), str):
        places = find_places(payload["location"])
        if places and request.location not in places:
            return worse(result, Match("other", math.inf, "location"))
    if request.amount is None:
        return result
    price = price_of(payload)
    currency = currency_code(payload.get("price_currency")) or request.currency
    if price is None or currency != request.currency:
        return worse(result, Match("other", math.inf, "price"))
    return worse(result, budget_match(price, request.amount, is_max=request.is_max))


def budget_match(price: float, amount: int, *, is_max: bool = True) -> Match:
    """The budget rule alone: ±``BUDGET_TOLERANCE`` exact, up to ``SIMILAR_CEILING`` similar."""
    offset = (price - amount) / amount
    distance = abs(offset)
    if is_max and offset <= 0:
        return Match("exact", 0.0)
    if distance <= BUDGET_TOLERANCE + 1e-9:
        return Match("exact", distance)
    if distance <= SIMILAR_CEILING + 1e-9:
        return Match("similar", distance, "price")
    return Match("other", distance, "price")


def area_text(area: float) -> str:
    """«~1 600 м²»."""
    return f"~{round(area):,}".replace(",", " ") + " м²"


def money(amount: float, currency: str = DEFAULT_CURRENCY) -> str:
    """«~60 000 €»: a rounded amount for the question text."""
    whole = round(amount)
    return f"~{whole:,}".replace(",", " ") + f" {_SYMBOLS.get(currency, currency)}"
