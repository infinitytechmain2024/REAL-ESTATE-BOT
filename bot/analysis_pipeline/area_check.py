"""The area of a post's «JSON-LD: {...}» line against the areas its own text states (ANALYSIS_AREA_TEXT_CHECK, Q13).

A portal row may carry a wrong area: JSON-LD ``surface`` 32088 for a listing whose description says «parcela … de
3.208mts». The prompt makes the JSON-LD authoritative, so the card showed 32 088 m². ``conflict`` compares the
line's areas (``area_m2``, ``plot_m2``, ``surface``) with every area the title and description state
(``bot.utils.listing_text.areas``, plus the Spanish «mts»/«mtrs»): when no pair agrees within a factor of two, the
area is not confirmed and ``apply`` sets ``area_m2`` (and its quote) to None. Neither number is taken: rule 9 of
CLAUDE.md, a fact we cannot tell is not guessed.

A built area and a plot are different facts: a house of 150 m² on a plot of 1 000 m² agrees with itself. So a pair
also agrees when one side is the plot and the other the building and the plot is the larger, and a JSON-LD area of
an unknown kind agrees with a larger plot of the text (it may be the house) or a smaller building (it may be the
plot). A text with no area, or a line with none, is never a conflict.

Deterministic and after the model's answer, on purpose: the prompt, its version and the request stay byte for byte
as before (no new model behaviour to re-measure on the golden set), the check reads only the evidence, never the
model's words, and an answer outside the schema is still an error before it runs. Pure functions, no I/O.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from bot.utils.listing_text import areas, number

from .models import AnalysisResult

PREFIX = "JSON-LD:"
LINK_PREFIX = "Ссылка:"
AREA_KEYS = (("area_m2", None), ("surface", None), ("plot_m2", "plot"))
RATIO = 2.0  # more than twice apart: not the same area
# «3.208mts», «1.500 mtrs»: the Spanish abbreviation listing_text does not read (a bare «mts» after «a» is a distance).
_MTS = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:,\d+)?)\s*(?:mts|mtrs|mt)\.?(?![\w²2])", re.IGNORECASE)
_DISTANCE_BEFORE = re.compile(r"(?<!\w)a\s+(?:unos\s+|menos\s+de\s+|mas\s+de\s+|más\s+de\s+)?$", re.IGNORECASE)
_DISTANCE_AFTER = re.compile(r"^\s*(?:de|del|al|hasta|hacia)(?!\w)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AreaConflict:
    source: tuple[float, ...]  # the JSON-LD areas
    text: tuple[float, ...]  # the text's areas


def _source(text: str) -> tuple[list[tuple[float, str | None]], str] | None:
    first, _, rest = (text or "").lstrip().partition("\n")
    if not first.startswith(PREFIX):
        return None
    try:
        data: Any = json.loads(first[len(PREFIX):])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    values = [(float(data[k]), kind) for k, kind in AREA_KEYS
              if isinstance(data.get(k), int | float) and not isinstance(data.get(k), bool) and data[k] > 0]
    return values, rest


def _mts(text: str) -> str:
    """«3.208mts» written as «3.208 m2» for ``listing_text.areas``, unless it is a distance («a 200 mts de»)."""
    def one(m: re.Match[str]) -> str:
        if _DISTANCE_BEFORE.search(text[max(0, m.start() - 16):m.start()]) or _DISTANCE_AFTER.match(text[m.end():]):
            return m.group(0)
        return f"{m.group(1)} m2" if number(m.group(1)) else m.group(0)
    return _MTS.sub(one, text)


def _agree(source: float, source_kind: str | None, text: float, text_kind: str | None) -> bool:
    if max(source, text) <= RATIO * min(source, text):
        return True
    if source_kind == "plot":
        return text_kind == "built" and text < source
    if text_kind == "plot":
        return source < text  # an area of unknown kind smaller than the plot: the house on it
    if text_kind == "built":
        return source > text  # an area of unknown kind larger than the house: its plot
    return False


def conflict(text: str, title: str = "") -> AreaConflict | None:
    """The JSON-LD and text areas when no pair of them agrees (see the module notes); else None."""
    parsed = _source(text)
    if parsed is None:
        return None
    source, rest = parsed
    if not source:
        return None
    body = "\n".join(line for line in rest.split("\n") if not line.startswith(LINK_PREFIX))
    if title and title not in body:
        body = f"{title}\n{body}"
    stated = areas(_mts(body))
    if not stated:
        return None
    if any(_agree(s, sk, t.m2, t.kind) for s, sk in source for t in stated):
        return None
    return AreaConflict(tuple(s for s, _ in source), tuple(dict.fromkeys(t.m2 for t in stated)))


def _m2(value: float) -> str:
    return f"{value:g}"


def note(found: AreaConflict) -> str:
    return (f"area not confirmed: JSON-LD {'/'.join(_m2(v) for v in found.source)} m2 vs text "
            f"{'/'.join(_m2(v) for v in found.text[:3])} m2")


def apply(result: AnalysisResult, found: AreaConflict) -> AnalysisResult:
    """No area on the card: ``area_m2`` and its quote become None, ``reason`` and ``area_unconfirmed`` say why."""
    reason = note(found)
    update: dict[str, Any] = {"area_m2": None, "evidence": {**result.evidence, "area": None},
                              "area_unconfirmed": reason[:120]}
    if reason not in result.reason:
        update["reason"] = f"{result.reason}; {reason}".strip("; ")[:300]
    return result.model_copy(update=update)
