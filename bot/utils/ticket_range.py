"""An investor's ticket written as a range («Nivel de Inversión €10,000 - €100,000»), read without guessing.

INVESTOR_TICKET_RANGE (Q68, default off; sim b6-g7-s7). The analysis keeps one number per finding (``price_amount``),
the range's lower bound, so a business angel of «$10,000 - $100,000» showed «Сумма: 10 000 $» and one of
«€100 - €150,000» «Сумма: 100 €». With the flag the card shows the range (``ticket_text``) and a comparison with the
task's ticket uses its upper bound (``TicketRange.high``): an investor fits when the upper bound reaches the task's
minimum.

Reading a range (both bounds written with one currency: €, $, £, EUR, USD, GBP, euros):

* «10,000», «10.000», «10 000» are ten thousand; «10k», «10 mil», «10 тыс» too; «2M», «2 millones», «2 млн» two million;
  a decimal («1,5M») is not read (no range);
* the upper bound must be above the lower one;
* the lower bound is **ambiguous** when it is a bare number below 1 000 (no thousands separator, no k/mil/M) and read
  as thousands it would still not pass the upper bound: «€100 - €150,000» is 100 € or 100 000 € to 150 000 €, «€10-50k»
  is 10 € or 10 000 €. Nothing is guessed: ``low`` is None and the card shows the source's text as it is («€100 -
  €150,000»); the upper bound is certain and still compares. «$500 - $5,000» is not ambiguous (500 000 > 5 000).
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_CUR = r"[€$£]|eur(?:os?)?(?![^\W\d_])|usd(?![^\W\d_])|gbp(?![^\W\d_])"
_NUM = r"(?<![\d.,])(?:\d{1,3}(?:[.,   ]\d{3})+|\d+)(?![.,]?\d)"
_SUF = r"millones|millions?|mil|mln|млн|тыс|k|m(?![²2])"
_SEP = r"\s*[-–—]\s*|\s+(?:to|a|hasta|до)\s+"


def _side(i: int) -> str:
    return (rf"(?:(?P<pre{i}>{_CUR})\s?)?(?P<num{i}>{_NUM})(?:\s?(?P<suf{i}>{_SUF})(?![^\W\d_]))?"
            rf"(?:\s?(?P<post{i}>{_CUR}))?")


_RANGE = re.compile(rf"{_side(1)}(?:{_SEP}){_side(2)}", re.IGNORECASE)
_MULTIPLIER = {"k": 1e3, "mil": 1e3, "тыс": 1e3, "m": 1e6, "mln": 1e6, "млн": 1e6, "million": 1e6, "millions": 1e6,
               "millones": 1e6}
_CODES = {"€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR", "$": "USD", "usd": "USD", "£": "GBP", "gbp": "GBP"}
SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def investor_ticket_range_enabled() -> bool:
    """INVESTOR_TICKET_RANGE (default off): an investor's ticket range is shown and compared by its upper bound."""
    return os.environ.get("INVESTOR_TICKET_RANGE", "").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True, slots=True)
class TicketRange:
    low: float | None  # None: ambiguous (see the module notes)
    high: float
    currency: str  # EUR | USD | GBP
    text: str  # the range as the source wrote it

    def display(self) -> str:
        """«10 000 – 100 000 $»; an ambiguous lower bound: the source's text as it is."""
        if self.low is None:
            return self.text
        return f"{_number(self.low)} – {_number(self.high)} {SYMBOLS.get(self.currency, self.currency)}"


def _number(value: float) -> str:
    return f"{int(value):,}".replace(",", " ")


def _value(match: re.Match[str], i: int) -> tuple[float, bool]:
    """The bound's value and whether it is bare (no thousands separator, no multiplier)."""
    num, suffix = match.group(f"num{i}"), (match.group(f"suf{i}") or "").casefold()
    digits = re.sub(r"\D", "", num)
    return float(digits) * _MULTIPLIER.get(suffix, 1.0), digits == num and not suffix


def ticket_ranges(text: str) -> list[TicketRange]:
    """Every money range in ``text`` (both bounds, one currency, the upper above the lower)."""
    found: list[TicketRange] = []
    for match in _RANGE.finditer(text or ""):
        marks = {_CODES[m.casefold()] for k in ("pre1", "post1", "pre2", "post2") if (m := match.group(k))}
        if len(marks) != 1:
            continue
        low, bare = _value(match, 1)
        high, _ = _value(match, 2)
        if high <= low:
            continue
        ambiguous = bare and low < 1000 and low * 1000 <= high
        found.append(TicketRange(None if ambiguous else low, high, marks.pop(), match.group(0).strip()))
    return found


def ticket_range_of(payload: Mapping[str, Any] | None) -> TicketRange | None:
    """The first range among the finding's ``price_signals`` (the prices as written), None without one."""
    for signal in (payload or {}).get("price_signals") or []:
        if ranges := ticket_ranges(str(signal)):
            return ranges[0]
    return None
