"""Is this the same property seen on two sites or in two groups? Pure functions, no I/O.

The runner (task 3.3) compares each new exact finding with the cards already sent
for the campaign; the same object becomes one card with «Также на: ...» links.
The policy is deliberately conservative: two cards are better than one wrong merge.

``same_object(a, b)``:

* price within 2 % and area within 3 % (a dimension known on both sides must agree);
* at least one of the two is known on both sides; when the other is unknown on
  either side, the listings must also share the rooms count (both known) and a
  location token;
* rooms equal when both are known; deal (rent/sale) and currency equal when both are known;
* location tokens overlap. A city alone is not a location (gazetteer cities and
  generic words such as «calle» are dropped): a district or street token must be
  shared, or the normalised titles (at least 4 words each) must have a Jaccard similarity of at least 0.8.

``object_key`` is an index hint (same key = very likely the same object), built from the
location tokens, rooms and logarithmic price / area buckets; ``None`` when price and area
are both unknown. ``same_object`` is the arbiter.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from .architect import find_places

PRICE_TOLERANCE = 0.02
AREA_TOLERANCE = 0.03
TITLE_JACCARD = 0.8
MIN_TITLE_WORDS = 4

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_STOP = frozenset({
    "calle", "carrer", "avenida", "avda", "plaza", "paseo", "camino", "barrio", "zona", "centro", "street", "road",
    "avenue", "square", "district", "de", "del", "la", "las", "los", "el", "en", "con", "para", "por", "and", "the",
    "piso", "pisos", "casa", "venta", "alquiler", "vendo", "alquilo", "apartamento", "apartment", "flat", "sale",
    "rent", "espana", "spain", "izquierda", "derecha", "bajo", "ático", "atico", "planta", "habitaciones",
    "habitacion", "dormitorios", "baños", "banos", "улица", "ул", "проспект", "район", "квартира", "продажа",
    "аренда", "комнаты", "комнатная", "испания",
})


@dataclass(frozen=True, slots=True)
class Listing:
    """The fields the comparison reads (all optional)."""

    price: float | None = None
    currency: str | None = None
    area: float | None = None
    rooms: int | None = None
    deal: str | None = None
    location: str = ""
    title: str = ""
    url: str | None = None


def _positive(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def listing_of(payload: dict[str, Any] | None, *, url: str | None = None, text: str = "") -> Listing:
    """The comparison fields of a stored finding payload."""
    payload = payload or {}
    rooms = payload.get("rooms")
    place = " ".join(str(payload[k]) for k in ("location", "address", "district") if isinstance(payload.get(k), str))
    title = str(payload.get("title") or payload.get("summary_ru") or payload.get("summary") or text or "")
    currency = payload.get("price_currency")
    deal = payload.get("deal_type")
    return Listing(
        price=_positive(payload.get("price_amount")),
        currency=currency.strip().upper() if isinstance(currency, str) and currency.strip() else None,
        area=_positive(payload.get("area_m2")),
        rooms=int(rooms) if isinstance(rooms, int | float) and not isinstance(rooms, bool) and rooms > 0 else None,
        deal=deal if deal in ("rent", "sale") else None,
        location=place,
        title=title,
        url=url or (str(payload["original_post_link"]) if payload.get("original_post_link") else None),
    )


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


@lru_cache(maxsize=4096)
def _is_city(word: str) -> bool:
    return bool(find_places(word))


def tokens(text: str) -> frozenset[str]:
    """District / street / address tokens: accents folded, generic words, numbers and cities dropped."""
    out = set()
    for word in _WORD.findall(_fold(text)):
        if len(word) < 3 or word.isdigit() or word in _STOP or _is_city(word):
            continue
        out.add(word)
    return frozenset(out)


def _title_words(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(_fold(text)) if len(w) >= 3)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Word-set similarity; titles shorter than ``MIN_TITLE_WORDS`` words say too little to merge on."""
    if len(a) < MIN_TITLE_WORDS or len(b) < MIN_TITLE_WORDS:
        return 0.0
    return len(a & b) / len(a | b)


def _close(a: float | None, b: float | None, tolerance: float) -> bool | None:
    """True / False when both are known, None when either is unknown."""
    if a is None or b is None:
        return None
    return abs(a - b) <= tolerance * max(a, b)


def object_key(listing: Listing) -> str | None:
    """An index hint for the object; ``None`` when price and area are both unknown."""
    if listing.price is None and listing.area is None:
        return None
    place = "-".join(sorted(tokens(listing.location))[:3]) or "?"
    price = round(math.log(listing.price) / math.log(1 + 2 * PRICE_TOLERANCE)) if listing.price else "?"
    area = round(math.log(listing.area) / math.log(1 + 2 * AREA_TOLERANCE)) if listing.area else "?"
    return f"{place}|r{listing.rooms or '?'}|p{price}|a{area}"


def same_object(a: Listing, b: Listing) -> bool:
    """True only when the two listings are almost surely one property (see the module notes)."""
    if a.deal and b.deal and a.deal != b.deal:
        return False
    if a.currency and b.currency and a.currency != b.currency:
        return False
    if a.rooms and b.rooms and a.rooms != b.rooms:
        return False
    price = _close(a.price, b.price, PRICE_TOLERANCE)
    area = _close(a.area, b.area, AREA_TOLERANCE)
    if price is False or area is False:
        return False
    if price is None and area is None:
        return False
    place_a, place_b = tokens(a.location), tokens(b.location)
    shared_place = bool(place_a & place_b)
    same_title = _jaccard(_title_words(a.title), _title_words(b.title)) >= TITLE_JACCARD
    if not (shared_place or same_title):
        return False
    if price is None or area is None:  # one dimension unconfirmed: the rest must agree explicitly
        return bool(a.rooms and b.rooms and shared_place)
    return True


def site_of(url: str | None) -> str:
    """The site's host without ``www.`` / ``m.``, for the «Также на» line."""
    host = (urlsplit(url or "").hostname or "").lower()
    return host.removeprefix("www.").removeprefix("m.")
