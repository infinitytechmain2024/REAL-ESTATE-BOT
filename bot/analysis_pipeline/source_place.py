"""The place of a post's «JSON-LD: {...}» line against the place its own text names (ANALYSIS_LOCATION_FROM_TEXT).

A portal actor may file an object under the wrong place: ``trev0n/habitaclia`` gave a plot in Mijas (Costa del Sol)
the location «Barrio de Salamanca, Madrid Capital» (``tests/fixtures/portals_live/habitaclia_items.json``,
``i54590000000240``). The worker writes that location into the JSON-LD line, so a model told to trust the line
would put «Madrid» on the card. ``conflict`` finds such a post before the model is called; ``without_place``
removes the place from the line the model reads; ``apply`` keeps the model's answer off the source's place.

Only a clear signal counts (no false alarms): the line's address must name one of the few areas below, the title
and description must not name that area anywhere, and they must name another area outside a street name
(«calle Valencia»), a landmark («aeropuerto de Málaga») or a distance («a 30 min de Madrid»). The areas are
coarse on purpose (Málaga, Marbella and Sevilla are one area, Valencia and Alicante another): a Marbella ad that
says «Málaga» is not a conflict. A place the list does not know (Mijas) is never a conflict here; the prompt rule
(``openrouter.LOCATION_FROM_TEXT_RULE``) covers it. Pure functions, no I/O.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .models import AnalysisResult

PREFIX = "JSON-LD: "
PLACE_KEYS = ("address", "location")
# area -> its city and region names, accent- and case-folded (the cities of the campaign gazetteer,
# bot/campaign/architect.py GAZETTEER, and the regions of bot/campaign/geo.py REGIONS, in Latin script).
AREAS: dict[str, tuple[str, ...]] = {
    "Madrid": ("madrid", "comunidad de madrid", "region de madrid"),
    "Cataluña": ("barcelona", "cataluna", "catalunya", "catalonia"),
    "Comunidad Valenciana": ("valencia", "alicante", "alacant", "comunidad valenciana", "comunitat valenciana",
                             "costa blanca"),
    "Andalucía": ("malaga", "marbella", "sevilla", "seville", "costa del sol", "andalucia", "andalusia"),
}
_ARTICLE = r"(?:(?:de|del|a|al)\s+)?(?:(?:la|las|el|los)\s+)?"
# What a name right after it is not the object's place: a street, a landmark or a distance.
_NOT_THE_PLACE = re.compile(
    r"(?:(?<!\w)(?:calle|c/|cl|avenida|avda|av|plaza|pza|paseo|ronda|glorieta|puerta|carretera|ctra|autovia|autopista|"
    r"camino|travesia|pasaje|via|estacion|barrio|colonia|parque|metro|urbanizacion|urb|aeropuerto|puerto|hotel|"
    r"club|real|universidad|hospital)\.?\s+" + _ARTICLE +
    r"|(?<!\w)a\s+(?:unos\s+)?\d+(?:[.,]\d+)?\s*(?:min\w*|km|kms|kilometros?|h|horas?|metros|m)\.?\s+" + _ARTICLE +
    r"(?:(?:centro|capital)\s+(?:de\s+)?)?"
    r"|(?<!\w)(?:cerca|junto|proxim[oa]s?|lejos|alrededores|afueras|desde|hacia|direccion|conexion(?:es)?|"
    r"comunicad[oa]s?|acceso|salida|vistas)\s+" + _ARTICLE + r"(?:con\s+)?" + _ARTICLE + r")$")
_LOOKBEHIND_CHARS = 48


@dataclass(frozen=True, slots=True)
class PlaceConflict:
    source: str  # the place the JSON-LD line gives (as written there)
    source_areas: frozenset[str]
    phrase: str  # the other place as the text writes it (a verbatim quote)
    area: str


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


def _jsonld(text: str) -> tuple[dict[str, Any], str] | None:
    """(the JSON-LD object, the rest of the text) of a post that starts with a JSON-LD line."""
    first, _, rest = text.lstrip().partition("\n")
    if not first.startswith(PREFIX):
        return None
    try:
        data = json.loads(first[len(PREFIX):])
    except ValueError:
        return None
    return (data, rest) if isinstance(data, dict) else None


def _source_place(data: dict[str, Any]) -> str:
    return ", ".join(str(data[k]) for k in PLACE_KEYS if isinstance(data.get(k), str) and data[k].strip())


def _hits(folded: str, names: tuple[str, ...]) -> list[re.Match[str]]:
    return [m for name in names for m in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", folded)]


def areas_of(text: str) -> frozenset[str]:
    """Every area a place string names (any mention counts)."""
    folded = _fold(text)
    return frozenset(area for area, names in AREAS.items() if _hits(folded, names))


def _place_hits(text: str) -> list[tuple[str, str]]:
    """(area, phrase) of every mention in ``text`` that is the object's place (not a street, landmark or distance)."""
    folded = _fold(text)  # NFKD folding of Spanish text keeps the length: positions match the original
    if len(folded) != len(text):
        return []
    found = []
    for area, names in AREAS.items():
        for match in _hits(folded, names):
            before = folded[max(0, match.start() - _LOOKBEHIND_CHARS):match.start()]
            if not _NOT_THE_PLACE.search(before):
                found.append((match.start(), area, text[match.start():match.end()]))
    return [(area, phrase) for _, area, phrase in sorted(found)]


def _own_text(rest: str) -> str:
    """The title and description of a post built from a source listing: without the «Ссылка:» line."""
    return "\n".join(line for line in rest.splitlines() if not line.startswith("Ссылка:"))


def conflict(text: str, title: str = "") -> PlaceConflict | None:
    """The other area the title/description name when the JSON-LD place is clearly not the object's; else None."""
    parsed = _jsonld(text)
    if parsed is None:
        return None
    data, rest = parsed
    source = _source_place(data)
    source_areas = areas_of(source)
    if not source_areas:
        return None  # a place the list does not know is never a conflict
    own = f"{title}\n{_own_text(rest)}"
    if areas_of(own) & source_areas:
        return None  # the text names the source's area somewhere: no clear conflict
    other = [(area, phrase) for area, phrase in _place_hits(own) if area not in source_areas]
    if not other:
        return None
    area, phrase = other[0]
    return PlaceConflict(source, source_areas, phrase, area)


def without_place(text: str) -> str:
    """``text`` with the place keys removed from its JSON-LD line (what the model reads on a conflict)."""
    parsed = _jsonld(text)
    if parsed is None:
        return text
    data, rest = parsed
    kept = {k: v for k, v in data.items() if k not in PLACE_KEYS}
    line = PREFIX + json.dumps(kept, ensure_ascii=False, separators=(", ", ": "))
    return f"{line}\n{rest}" if rest else line


def note(found: PlaceConflict) -> str:
    return f"source location '{found.source}' contradicts the text: '{found.phrase}'"


def apply(result: AnalysisResult, found: PlaceConflict) -> AnalysisResult:
    """The answer with the conflict in ``reason`` and no location, district or address of the source's area."""
    update: dict[str, Any] = {}
    reason = note(found)
    if reason not in result.reason:
        update["reason"] = f"{result.reason}; {reason}".strip("; ")[:300]

    def wrong(value: str | None) -> bool:
        areas = areas_of(value or "")
        return bool(areas & found.source_areas) and found.area not in areas

    if result.location is None or wrong(result.location):
        update["location"] = found.phrase
        update["evidence"] = {**result.evidence, "location": found.phrase}
    for key in ("district", "address"):
        if wrong(getattr(result, key)):
            update[key] = None
    return result.model_copy(update=update) if update else result
