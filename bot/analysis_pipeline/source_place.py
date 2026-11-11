"""The place of a post's «JSON-LD: {...}» line against the place its own text names (ANALYSIS_LOCATION_FROM_TEXT).

A portal actor may file an object under the wrong place: ``trev0n/habitaclia`` gave a plot in Mijas (Costa del Sol)
the location «Barrio de Salamanca, Madrid Capital» (``tests/fixtures/portals_live/habitaclia_items.json``,
``i54590000000240``). The worker writes that location into the JSON-LD line, so a model told to trust the line
would put «Madrid» on the card. ``conflict`` finds such a post before the model is called; ``without_place``
removes the place from the line the model reads; ``apply`` notes the conflict in ``reason`` and only fills a
location the model left empty or copied from the source.

Only a clear signal counts (no false alarms):

* the line's address names one of the few areas below, and the title and description never name that area nor
  the address's own district or town («en pleno Triana» for «Triana, Sevilla»);
* another area is named in the portal's own title (not the one we build from the actor's place, «Piso en venta,
  Chamberí») or in the description right after «ubicado en», «situado en», «se encuentra en» -- a strong signal,
  the only one that may fill the location (with a city) and exclude the finding by place (``source_place_conflict``);
* or right after a bare «en» («en Mijas, Costa del Sol») -- a weak signal: the source's place is cut from what the
  model reads and ``reason`` notes it, nothing else; a bare «en» after «residente», «vivo», «trabaja», «diseñada»,
  «como», «más», «oficina» ... (``_WEAK_STOP``) does not count at all;
* that mention is not a street («calle Valencia», «Carrer de Madrid»), a landmark («aeropuerto de», «ciudad de»),
  a distance or a trip («a 30 min de», «AVE a», «se traslada a»), an agency («Inmobiliaria Barcelona Homes», «Costa
  del Sol Properties»), a style («estilo Barcelona») or someone else's place («clientes en Barcelona»).

The areas are coarse on purpose (Málaga, Marbella and Sevilla are one area, Valencia and Alicante another): a
Marbella ad that says «Málaga» is not a conflict. A place the list does not know (Mijas) is never a conflict here;
the prompt rule (``openrouter.LOCATION_FROM_TEXT_RULE``) covers it. Pure functions, no I/O.
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
LINK_PREFIX = "Ссылка:"
# area -> (its cities, as bot/campaign/architect.py find_places knows them; its regions, bot/campaign/geo.py REGIONS),
# accent- and case-folded, Latin script only.
AREAS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "Madrid": (("madrid",), ("comunidad de madrid", "region de madrid")),
    "Cataluña": (("barcelona",), ("cataluna", "catalunya", "catalonia")),
    "Comunidad Valenciana": (("valencia", "alicante", "alacant"),
                             ("comunidad valenciana", "comunitat valenciana", "costa blanca")),
    "Andalucía": (("malaga", "marbella", "sevilla", "seville"), ("costa del sol", "andalucia", "andalusia")),
}
_ARTICLE = r"(?:(?:en|de|del|a|al|con)\s+)?(?:(?:la|las|el|los)\s+)?"
_NOT_PLACE_WORDS = (
    # streets and landmarks (Spanish and Catalan)
    r"calle|c/|cl|carrer|avenida|avinguda|avda|av|plaza|placa|pza|paseo|passeig|ronda|glorieta|puerta|carretera|ctra|"
    r"autovia|autopista|camino|cami|travesia|pasaje|via|estacion|barrio|colonia|parque|metro|urbanizacion|urb|"
    r"aeropuerto|puerto|hotel|club|real|universidad|hospital|ciudad|estadio|"
    # trips, agencies, styles, other people
    r"ave|tren|inmobiliaria|oficina|agencia|office|estilo|clientes|traslada|trasladamos|mudanza|"
    # distances and directions
    r"cerca|junto|proxim[oa]s?|lejos|alrededores|afueras|desde|hacia|direccion|conexion(?:es)?|comunicad[oa]s?|"
    r"acceso|salida|vistas"
)
# What a name right after it is not the object's place.
_NOT_THE_PLACE = re.compile(
    rf"(?:(?<!\w)(?:{_NOT_PLACE_WORDS})\.?\s+{_ARTICLE}"
    rf"|(?<!\w)a\s+(?:unos\s+)?\d+(?:[.,]\d+)?\s*(?:min\w*|km|kms|kilometros?|h|horas?|metros|m|pasos)\.?\s+"
    rf"(?:en\s+\w+\s+)?{_ARTICLE}(?:(?:centro|capital)\s+(?:de\s+)?)?)$")
# What a name right before it is not the object's place: an agency or a team named after a place.
_NOT_THE_PLACE_AFTER = re.compile(
    r"^\s+(?:properties|property|homes|home|realty|real\s+estate|estate|office|oficina|inmobiliaria|group|grupo|"
    r"invest\w*|living|international|sotheby\w*|team|fc|cf|style)(?!\w)")
_LOOKBEHIND_CHARS = 48
# «en X», «ubicado en X»...: the X a description gives as the object's place (up to the end of the clause). Group 1:
# the strong phrases (situado/ubicado en, se encuentra en) that may fill the location; a bare «en» is a weak signal.
_PLACE_PHRASE = re.compile(
    r"(?<!\w)(?:((?:ubicad|situad)[oa]s?\s+en|se\s+encuentra\s+en)|en)\s+([^.;:!?¿()\n]{1,80})")
# A bare «en» after these (within three words) is about someone or something else: «residente en Barcelona», «ahora
# vivo en Valencia», «más pisos en Valencia», «cocina diseñada en Barcelona», «trabaja en», «como en Barcelona».
_WEAK_STOP = re.compile(
    r"(?<!\w)(?:residente?s?|reside|resido|vivo|vive|viven|vivimos|vivia|trabaj\w*|disenad\w*|fabricad\w*|hech[oa]s?|"
    r"como|mas|tambien|otr[oa]s|oficinas?|sede|sucursal\w*|nacid[oa]|estudi\w*|igual)"
    r"(?:\s+\S+){0,2}\s+$")
# The X itself, or the part after its first comma: «en Mijas, Costa del Sol», «en la Costa del Sol».
_PHRASE_START = re.compile(r"(?:(?:la|las|el|los)\s+)?|[^,]{1,40},\s*(?:(?:la|las|el|los)\s+)?(?:provincia\s+de\s+)?")
# The titles the portal sources build from the actor's place when the row has none (portals.py: Fotocasa, Habitaclia).
_SYNTHETIC_TITLE = re.compile(r"^(?:terreno|vivienda|casa|piso|local)\s+en\s+(?:venta|alquiler)(?:\s*,.*)?$")


@dataclass(frozen=True, slots=True)
class PlaceConflict:
    source: str  # the place the JSON-LD line gives (as written there)
    source_areas: frozenset[str]
    phrase: str  # the other place as the text writes it (a verbatim quote)
    area: str
    city: str | None  # the first city (not region) of a strong signal, as written; None: a region or a weak signal only
    strong: bool = False  # from the portal's own title or «situado/ubicado en», «se encuentra en» (not a bare «en»)


def fold(text: str) -> str:
    """Lower case without accents, one character for one: positions in the result are positions in ``text``."""
    out = []
    for char in text:
        bare = "".join(c for c in unicodedata.normalize("NFKD", char) if not unicodedata.combining(c)).lower()
        out.append(bare if len(bare) == 1 else char.lower() if len(char.lower()) == 1 else char)
    return "".join(out)


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


def _names(area: str) -> tuple[str, ...]:
    cities, regions = AREAS[area]
    return cities + regions


def _hits(folded: str, names: tuple[str, ...]) -> list[re.Match[str]]:
    return [m for name in names for m in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", folded)]


def areas_of(text: str) -> frozenset[str]:
    """Every area a place string names (any mention counts)."""
    folded = fold(text)
    return frozenset(area for area in AREAS if _hits(folded, _names(area)))


def _is_the_place(folded: str, start: int, end: int) -> bool:
    before = folded[max(0, start - _LOOKBEHIND_CHARS):start]
    return not _NOT_THE_PLACE.search(before) and not _NOT_THE_PLACE_AFTER.match(folded[end:])


def _title_hits(title: str) -> list[tuple[bool, int, str, str]]:
    """(strong, position, area, phrase) of the portal's own title; a title we built ourselves gives nothing."""
    folded = fold(title)
    if _SYNTHETIC_TITLE.match(folded.strip()):
        return []
    return [(True, m.start(), area, title[m.start():m.end()]) for area in AREAS for m in _hits(folded, _names(area))
            if _is_the_place(folded, m.start(), m.end())]


def _description_hits(description: str) -> list[tuple[bool, int, str, str]]:
    """(strong, position, area, phrase) right after «situado/ubicado en», «se encuentra en» (strong) or «en» (weak)."""
    folded = fold(description)
    found = []
    for phrase in _PLACE_PHRASE.finditer(folded):
        strong = phrase.group(1) is not None
        if not strong and _WEAK_STOP.search(folded[max(0, phrase.start() - _LOOKBEHIND_CHARS):phrase.start()]):
            continue
        start, end = phrase.span(2)
        for area in AREAS:
            for m in _hits(folded[start:end], _names(area)):
                lead = _PHRASE_START.fullmatch(folded[start:start + m.start()])
                if lead and _is_the_place(folded, start + m.start(), start + m.end()):
                    found.append((strong, start + m.start(), area, description[start + m.start():start + m.end()]))
    return found


def _source_parts(source: str) -> tuple[str, ...]:
    """The source's own district and town names (not its areas): «Triana» of «Triana, Sevilla»."""
    area_names = {n for area in AREAS for n in _names(area)}
    parts = (" ".join(fold(p).split()) for p in source.split(","))
    return tuple(p for p in parts if len(p) >= 4 and p not in area_names and not areas_of(p))


def _parts(rest: str, title: str) -> tuple[str, str]:
    """(title, description) of the text after the JSON-LD line.

    A post built from a source listing is «title / Ссылка: url / description» (web_search/worker.py ``_source_text``);
    any other post with a JSON-LD line is description only, and its title is the post's.
    """
    lines = rest.split("\n")
    if len(lines) >= 2 and lines[1].startswith(LINK_PREFIX):
        return lines[0], "\n".join(lines[2:])
    return title, "\n".join(line for line in lines if not line.startswith(LINK_PREFIX))


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
    own_title, description = _parts(rest, title)
    # a title we built from the actor's place repeats the source: it neither confirms nor contradicts it
    own = f"{'' if _SYNTHETIC_TITLE.match(fold(own_title).strip()) else own_title}\n{description}"
    if areas_of(own) & source_areas:
        return None  # the text names the source's area somewhere: no clear conflict
    if _hits(fold(own), _source_parts(source)):
        return None  # the text names the source's own district or town («en pleno Triana»): no clear conflict
    hits = [(0, *h[1:], h[0]) for h in _title_hits(own_title)] + [(1, *h[1:], h[0])
                                                                  for h in _description_hits(description)]
    other = sorted((not strong, part, at, area, phrase) for part, at, area, phrase, strong in hits
                   if area not in source_areas)
    if not other:
        return None
    weak, _, _, area, phrase = other[0]
    city = next((p for w, _, _, a, p in other if not w and fold(p) in AREAS[a][0]), None)
    return PlaceConflict(source, source_areas, phrase, area, city, strong=not weak)


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


def _same(value: str | None, source: str) -> bool:
    return value is not None and " ".join(value.casefold().split()) == " ".join(source.casefold().split())


def apply(result: AnalysisResult, found: PlaceConflict) -> AnalysisResult:
    """The answer with the conflict in ``reason``; the model's own location is never overwritten.

    Only a location the model left empty or copied verbatim from the source is set: to the city the text names
    (one ``find_places`` knows) of a strong signal, or None when the text names only a region or the signal is weak
    (a bare «en»). An address copied from the source goes. A strong conflict is kept in ``source_place_conflict``
    (the text's area): ``bot.campaign.tolerance`` excludes the finding by place in a campaign of another area.
    """
    update: dict[str, Any] = {}
    reason = note(found)
    if reason not in result.reason:
        update["reason"] = f"{result.reason}; {reason}".strip("; ")[:300]
    if result.location is None or _same(result.location, found.source):
        update["location"] = found.city
        update["evidence"] = {**result.evidence, "location": found.city}
    if _same(result.address, found.source):
        update["address"] = None
    if found.strong:
        update["source_place_conflict"] = found.area
    return result.model_copy(update=update) if update else result
