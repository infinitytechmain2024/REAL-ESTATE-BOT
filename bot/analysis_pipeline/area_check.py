"""The area of a post's «JSON-LD: {...}» line against the areas its own text states (ANALYSIS_AREA_TEXT_CHECK, Q13).

A portal row may carry a wrong area: JSON-LD ``surface`` 32088 for a listing whose description says «parcela … de
3.208mts». The prompt makes the JSON-LD authoritative, so the card showed 32 088 m². ``conflict`` compares the
line's areas (``area_m2``, ``plot_m2``, ``surface``) with the areas of the same kind the title and description state;
when they are more than twice apart, the area is not confirmed and ``apply`` sets ``area_m2`` (and its quote) to
None. Neither number is taken: rule 9 of CLAUDE.md, a fact we cannot tell is not guessed.

Only a clear signal counts (review of Q13): a text area counts only when the word just before it or right after it
(«N m² de parcela») says whose it is -- the plot (parcela, terreno, solar, finca, suelo) or the building (piso, casa,
chalet, nave, vivienda, construidos, útiles ...). A part of the object or of its surroundings (terraza, balcón,
jardín, garaje, trastero, salón, cocina, dormitorio, oficinas, piscina, urbanización, zonas comunes ...) and the
buildable area (edificabilidad, m² de techo) never count. «mts» / «metros» are read only next to such a word and
never as a distance («a 200 mts de», «playa 200 mts»).

Kinds: ``plot_m2`` (and ``area_m2`` of a ``landparcel``) is the plot and meets only the text's plots. ``area_m2`` of
another listing is the building when the line also has ``plot_m2``, else of unknown kind: it may be the house (it
agrees with any larger plot) or the plot (with any smaller building). Nothing of a kind to compare with: the area
stays.

Deterministic and after the model's answer, on purpose: the prompt, its version and the request stay byte for byte
as before (no new model behaviour to re-measure on the golden set), the check reads only the evidence, never the
model's words, and an answer outside the schema is still an error before it runs. Pure functions, no I/O.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from bot.utils.listing_text import number

from .models import AnalysisResult

PREFIX = "JSON-LD:"
LINK_PREFIX = "Ссылка:"
RATIO = 2.0  # more than twice apart: not the same area
WINDOW = 40  # characters before a number searched for whose area it is (up to the clause's start)

_NUMBER = r"\d{1,3}(?:\.\d{3})+(?:,\d+)?|\d+(?:[.,]\d+)?"
_SQUARE = r"m²|m2|mts?2|mts?\.?\s*cuadrados|metros?\s+cuadrados|sq\.?\s*m|sqm"
_HECTARE = r"hectareas?|hectares?|ha|has"
_LOOSE = r"mts|mtrs|mt|metros"  # «3.208mts», «2000 metros»: an area only next to a plot or a building word
_AREA = re.compile(rf"(?<![\d.,])({_NUMBER})\s*({_SQUARE}|{_HECTARE}|{_LOOSE})\.?(?![\w²])")
_PLOT = r"parcelas?|terrenos?|solar(?:es)?|fincas?|suelos?|plot|land"
_BUILT = (r"construid\w*|utiles?|vivienda\w*|edificad[oa]s?|habitables?|superficie\s+construida|piso\w*|casas?|"
          r"chalets?|atico\w*|naves?|locales?|apartamentos?|duplex|villas?|adosados?|pareados?|estudios?|lofts?|"
          r"bungalows?|built|house|home")
_PART = (r"terrazas?|balcon\w*|jardin\w*|garaje\w*|parking|trasteros?|salon\w*|cocinas?|dormitorios?|"
         r"habitacion\w*|banos?|aseos?|patios?|porche\w*|solarium|piscinas?|oficinas?|almacen\w*|altillo\w*|"
         r"urbanizacion\w*|zonas?\s+comun\w*|comunes|edificabilidad|edificables?|techo|fachadas?|sotano\w*|"
         r"buhardilla\w*|vestidor\w*|lavadero\w*")
_WORD = re.compile(rf"(?<!\w)(?:({_PART})|({_PLOT})|({_BUILT}))(?!\w)")
_AFTER = re.compile(rf"^\s*(?:de\s+|del\s+)?(?:(?:la|el|los|las)\s+)?(?:({_PART})|({_PLOT})|({_BUILT}))(?!\w)")
_CLAUSE = re.compile(r"[.;:,·|\n(]\s")
_DISTANCE_BEFORE = re.compile(r"(?<!\w)a\s+(?:unos\s+|solo\s+|menos\s+de\s+|mas\s+de\s+)?$")
_DISTANCE_AFTER = re.compile(r"^\s*(?:de|del|al|hasta|hacia)(?!\w)")


@dataclass(frozen=True, slots=True)
class AreaConflict:
    source: tuple[float, ...]  # the JSON-LD areas
    text: tuple[float, ...]  # the text's areas of the same kind


def fold(text: str) -> str:
    """Lower case without accents, one character for one (positions stay)."""
    out = []
    for char in text:
        bare = "".join(c for c in unicodedata.normalize("NFKD", char) if not unicodedata.combining(c)).lower()
        out.append(bare if len(bare) == 1 else char.lower() if len(char.lower()) == 1 else char)
    return "".join(out)


def _kind(match: re.Match[str] | None) -> str | None:
    if match is None:
        return None
    return "part" if match.group(1) else "plot" if match.group(2) else "built"


def stated(text: str) -> list[tuple[float, str]]:
    """(m², plot | built) of every area the text gives a kind; parts, buildable areas and distances are left out."""
    folded = fold(text)
    out = []
    for m in _AREA.finditer(folded):
        value = number(m.group(1).replace(".", "") if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", m.group(1))
                       else m.group(1))
        if value is None:
            continue
        unit = m.group(2)
        loose = bool(re.fullmatch(_LOOSE, unit))
        before = _CLAUSE.split(folded[max(0, m.start() - WINDOW):m.start()])[-1]
        after = folded[m.end():m.end() + 30]
        if _DISTANCE_BEFORE.search(before) or (loose and _DISTANCE_AFTER.match(after)):
            continue
        kind = _kind(_AFTER.match(after)) or _kind(next(reversed(list(_WORD.finditer(before))), None))
        if kind is None or kind == "part":
            continue
        m2 = value * 10_000 if re.fullmatch(_HECTARE, unit) else value
        if 1 <= m2 <= 100_000_000:
            out.append((round(m2, 1), kind))
    return out


def _source(text: str) -> tuple[list[tuple[float, str | None]], str] | None:
    """(the JSON-LD areas with their kind, the rest of the text) of a post that starts with a JSON-LD line."""
    first, _, rest = (text or "").lstrip().partition("\n")
    if not first.startswith(PREFIX):
        return None
    try:
        data: Any = json.loads(first[len(PREFIX):])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    def positive(key: str) -> float | None:
        value = data.get(key)
        ok = isinstance(value, int | float) and not isinstance(value, bool) and value > 0
        return float(value) if ok else None

    land = str(data.get("property_type") or "").lower() == "landparcel"
    plot = positive("plot_m2")
    values: list[tuple[float, str | None]] = [(plot, "plot")] if plot else []
    for key in ("area_m2", "surface"):
        value = positive(key)
        if value is not None:
            values.append((value, "plot" if land else "built" if plot else None))
    return values, rest


def _near(value: float, others: list[float]) -> bool:
    return any(max(value, o) <= RATIO * min(value, o) for o in others)


def _fits(value: float, kind: str | None, plots: list[float], built: list[float]) -> bool:
    """Is the JSON-LD area one of the text's areas of its kind (within twice)?"""
    if kind == "plot":
        return not plots or _near(value, plots)
    if kind == "built":
        return not built or _near(value, built)
    # unknown: the house (as the text's building, not larger than its plot) or the plot (as the text's plot,
    # not smaller than its building)
    house = (not built or _near(value, built)) and (not plots or value <= RATIO * max(plots))
    plot = (not plots or _near(value, plots)) and (not built or value * RATIO >= min(built))
    return house or plot


def conflict(text: str, title: str = "") -> AreaConflict | None:
    """The JSON-LD and text areas when a JSON-LD area is none of the text's areas of its kind; else None."""
    parsed = _source(text)
    if parsed is None:
        return None
    source, rest = parsed
    if not source:
        return None
    body = "\n".join(line for line in rest.split("\n") if not line.startswith(LINK_PREFIX))
    if title and title not in body:
        body = f"{title}\n{body}"
    found = stated(body)
    plots = [m2 for m2, k in found if k == "plot"]
    built = [m2 for m2, k in found if k == "built"]
    for value, kind in source:
        same = plots if kind == "plot" else built if kind == "built" else plots + built
        if same and not _fits(value, kind, plots, built):
            return AreaConflict(tuple(v for v, _ in source), tuple(dict.fromkeys(same)))
    return None


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
