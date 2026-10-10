"""Cheap, deterministic reading of a listing's area and deal from Spanish (and English/Russian) text.

Used before any model sees a page (``bot.analysis_pipeline.prefilter``; it lives in ``bot/utils`` so the analysis
image has it). Spanish writes thousands with a dot and decimals with a comma: «2.000 m²» is two thousand square
metres, «2,5 ha» two and a half hectares. A number is an area only next to a unit (m², m2, metros cuadrados, ha,
hectáreas, сотки); a bare «500 m» or «200 metros» is a distance. The words just before a number say whose area it is:
the plot (parcela, terreno, solar, finca) or the building (construida, útil, vivienda).

``deal_of`` reads the deal only from strong signals (an offer phrase or a monthly price), and says nothing when a
text shows both deals (a site menu «Venta · Alquiler»).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

AreaKind = Literal["plot", "built"]

_NUMBER = r"\d{1,3}(?:[.   ]\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?"
_UNIT = (r"m²|m2|mts?2|mts?\.?\s*cuadrados|metros?\s+cuadrados|sq\.?\s*m|sqm"
         r"|hect[aá]reas?|hectares?|ha\b|has\b|га\b|гектар\w*|сот\w*|м²|м2|кв\.?\s*м")
_AREA = re.compile(rf"(?<![\d.,])({_NUMBER})\s*({_UNIT})", re.IGNORECASE)
_PLOT_WORDS = re.compile(r"(?<!\w)(?:parcela|terreno|solar|finca|suelo|plot|land|участ|земл|ділянк)\w*", re.IGNORECASE)
_BUILT_WORDS = re.compile(r"(?<!\w)(?:construid|[uú]til|vivienda|edificad|habitable|built|living|дом|жил)\w*",
                          re.IGNORECASE)
WINDOW = 40  # characters before a number searched for whose area it is; the nearest word wins


@dataclass(frozen=True, slots=True)
class Area:
    m2: float
    kind: AreaKind | None = None  # None: the text does not say


def number(raw: str) -> float | None:
    """A Spanish-written number: «2.000» 2000, «2.000,5» 2000.5, «2,5» 2.5, «1.5» 1.5, «2 000» 2000."""
    text = raw.replace(" ", " ").replace(" ", " ").strip()
    if re.fullmatch(r"\d{1,3}(?:[. ]\d{3})+(?:,\d+)?", text):
        whole, _, frac = text.partition(",")
        text = re.sub(r"[. ]", "", whole) + (f".{frac}" if frac else "")
    elif re.fullmatch(r"\d+,\d+", text):
        text = text.replace(",", ".")
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value > 0 else None


def _to_m2(value: float, unit: str) -> float:
    unit = _fold(unit)
    if unit.startswith(("hect", "гект")) or unit in ("ha", "has", "га"):
        return value * 10_000
    if unit.startswith("сот"):
        return value * 100
    return value


def areas(text: str) -> list[Area]:
    """Every area the text states, in m², with whose area it is when the words before it say so."""
    out: list[Area] = []
    for found in _AREA.finditer(text or ""):
        value = number(found.group(1))
        if value is None:
            continue
        m2 = _to_m2(value, found.group(2))
        if not 1 <= m2 <= 100_000_000:
            continue
        before = re.split(r"[.;·|\n]\s", text[max(0, found.start() - WINDOW):found.start()])[-1]  # this sentence only
        plot = max((m.end() for m in _PLOT_WORDS.finditer(before)), default=-1)
        built = max((m.end() for m in _BUILT_WORDS.finditer(before)), default=-1)
        kind: AreaKind | None = None if plot == built == -1 else "plot" if plot > built else "built"
        out.append(Area(round(m2, 1), kind))
    return out


def plot_area(text: str) -> float | None:
    """The largest plot area the text states (parcela/terreno ...), else None."""
    plots = [a.m2 for a in areas(text) if a.kind == "plot"]
    return max(plots) if plots else None


def largest_area(text: str) -> float | None:
    """The largest area of any kind (a plot is never smaller than the house on it), else None."""
    found = [a.m2 for a in areas(text)]
    return max(found) if found else None


_RENT = re.compile(r"(?<!\w)(?:se\s+alquila|en\s+alquiler|alquilo|alquiler(?:es)?|arrendamiento|for\s+rent|to\s+let"
                   r"|сда[её]тся|сдам|аренда|оренда)(?!\w)|€\s*/\s*mes\b|€\s*al\s+mes\b|eur(?:os)?\s*/\s*mes\b"
                   r"|/\s*month\b|per\s+month\b|al\s+mes\b", re.IGNORECASE)
_SALE = re.compile(r"(?<!\w)(?:se\s+vende|en\s+venta|vendo|venta|for\s+sale|продаж\w*|продам|продаю)(?!\w)", re.IGNORECASE)


def deal_of(text: str) -> Literal["rent", "sale"] | None:
    """``rent`` / ``sale`` when the text shows only that deal, else None (neither, or both)."""
    rent, sale = bool(_RENT.search(text or "")), bool(_SALE.search(text or ""))
    return "rent" if rent and not sale else "sale" if sale and not rent else None


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))
