"""Comparing a result's price against the budget the user asked for.

Kept out of the LLM deliberately. The model reports the price it found on the
page as a number; the arithmetic and the "is this in range" decision happen
here, because the resulting figure is quoted back to the user as "дороже на
45 000 EUR" and has to be right.

The one thing this module refuses to do is guess across currencies. A budget in
EUR and a listing in USD are not comparable without a rate, and a stale rate
would put a wrong number in front of someone making a financial decision, so
the pair is reported as :attr:`BudgetFit.UNKNOWN` instead.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from bot.logging_conf import get_logger
from bot.models.enums import BudgetFit
from bot.models.query import ParsedQuery
from bot.models.result import StructuredResult

log = get_logger(__name__)


class BudgetMatch(BaseModel):
    """How one result relates to the requested budget."""

    model_config = ConfigDict(frozen=True)

    fit: BudgetFit = BudgetFit.UNKNOWN
    delta: float | None = None
    """Absolute distance from the nearest bound, in :attr:`currency`."""
    currency: str | None = None
    price: float | None = None

    @property
    def is_alternative(self) -> bool:
        return self.fit.is_alternative


def classify(result: StructuredResult, query: ParsedQuery) -> BudgetMatch:
    """Place *result* relative to the budget in *query*.

    Returns :attr:`BudgetFit.EXACT` when the user named no budget -- there is
    nothing to fall outside of, so every result is a match on price.
    """
    lower = query.budget_min
    upper = query.budget_max

    if lower is None and upper is None:
        return BudgetMatch(fit=BudgetFit.EXACT, price=result.price_value,
                           currency=result.price_currency)

    price = result.price_value
    if price is None or price <= 0:
        return BudgetMatch(fit=BudgetFit.UNKNOWN, currency=result.price_currency)

    currency = _comparable_currency(query, result)
    if currency is None:
        log.debug(
            "budget.currency_mismatch",
            url=result.url,
            wanted=query.currency,
            found=result.price_currency,
        )
        return BudgetMatch(fit=BudgetFit.UNKNOWN, price=price,
                           currency=result.price_currency)

    if upper is not None and price > upper:
        return BudgetMatch(
            fit=BudgetFit.OVER, delta=price - upper, currency=currency, price=price
        )
    if lower is not None and price < lower:
        return BudgetMatch(
            fit=BudgetFit.UNDER, delta=lower - price, currency=currency, price=price
        )
    return BudgetMatch(fit=BudgetFit.EXACT, currency=currency, price=price)


def _comparable_currency(query: ParsedQuery, result: StructuredResult) -> str | None:
    """The currency both figures are in, or ``None`` if they cannot be compared.

    When only one side names a currency we take it: the extraction prompt
    normalises the user's budget to the local currency, so a listing that omits
    the code is overwhelmingly in the same one. When both name a currency and
    they differ, we give up rather than convert.
    """
    wanted = (query.currency or "").upper() or None
    found = (result.price_currency or "").upper() or None

    if wanted and found:
        return wanted if wanted == found else None
    return wanted or found


def split_by_fit(
    results: list[StructuredResult], query: ParsedQuery
) -> tuple[list[tuple[StructuredResult, BudgetMatch]], list[tuple[StructuredResult, BudgetMatch]]]:
    """Partition *results* into (matches, alternatives), each keeping its match.

    Alternatives are ordered by how close they are to the budget rather than by
    relevance score: if the user cannot have what they asked for, the next most
    useful thing to see is the smallest step away from it.
    """
    matches: list[tuple[StructuredResult, BudgetMatch]] = []
    alternatives: list[tuple[StructuredResult, BudgetMatch]] = []

    for result in results:
        match = classify(result, query)
        (alternatives if match.is_alternative else matches).append((result, match))

    alternatives.sort(key=lambda pair: (pair[1].delta or float("inf")))
    return matches, alternatives
