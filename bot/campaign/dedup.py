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
  generic words such as «calle» are dropped): when both have a street address a street token must be shared
  (a district alone never identifies a flat); otherwise a district or street token must be shared;
* different ``floor`` (both known) or different house number in the address (both known) is never the same object;
* two URLs on the SAME host are the same object only with the same address AND floor (or address, price and
  area with the floor unknown on both): one portal does not list a flat twice;
* the titles alone merge only with at least 6 words each, Jaccard >= 0.8 and price and area both matching.

The listing body (not only the structured fields) is compared too, because Facebook posts rarely carry an address;
the text rules run after the negative rules above (a price, area, rooms, floor, deal or currency conflict still
wins) and work across platforms (group A vs group B, Facebook vs a portal):

* text Jaccard (5-word shingles) >= 0.80, both texts at least 15 words: the same object;
* the same phone number and the price within 2 % (rooms not contradicting): the same object;
* text Jaccard >= 0.60, the price within 2 % and the same rooms (both known): the same object.

Two ads of one portal are not merged by text (only Facebook, where one flat is posted in many groups, is).

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
MIN_TITLE_WORDS = 6

TEXT_LIMIT = 1500  # characters of the post body that are fingerprinted (heads keep only this excerpt)
SHINGLE_WORDS = 5
TEXT_SAME = 0.80
TEXT_SAME_MIN_WORDS = 15
TEXT_PRICE_JACCARD = 0.60
MIN_PHONE_DIGITS = 9

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_HASHTAG = re.compile(r"[#@]\w+", re.UNICODE)
_PHONE = re.compile(r"(?<![\w.,])\+?\d{2,}(?:[ .-]\d{2,}){0,6}(?![\w])")
_PRICE_TEXT = re.compile(
    r"(?:(?P<a>\d{1,3}(?:[ .,\u00a0]\d{3})+|\d{4,9})\s*(?:€|eur\b|euros?\b|\$|usd\b|грн|₴)"
    r"|(?:€|\$)\s*(?P<b>\d{1,3}(?:[ .,\u00a0]\d{3})+|\d{4,9}))", re.IGNORECASE)
_SOCIAL_HOSTS = frozenset({"facebook.com", "web.facebook.com", "fb.com", "fb.watch", "instagram.com"})
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
    floor: int | None = None
    address: str = ""
    district: str = ""
    shingles: frozenset[str] = frozenset()  # 5-word shingles of the body, computed once (``fingerprint``)
    words: int = 0
    phones: frozenset[str] = frozenset()


def _positive(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def listing_of(payload: dict[str, Any] | None, *, url: str | None = None, text: str = "",
               body: str = "") -> Listing:
    """The comparison fields of a stored finding payload; ``body`` is the post text (its first ``TEXT_LIMIT``
    characters are fingerprinted; the summary ``text`` stands in when there is no body)."""
    payload = payload or {}
    excerpt = (body or text or "")[:TEXT_LIMIT]
    shingles, words = fingerprint(excerpt)
    price = _positive(payload.get("price_amount"))
    if price is None:
        price = main_price(excerpt)
    rooms = payload.get("rooms")
    place = " ".join(str(payload[k]) for k in ("location", "address", "district") if isinstance(payload.get(k), str))
    floor = payload.get("floor")
    title = str(payload.get("title") or payload.get("summary_ru") or payload.get("summary") or text or "")
    currency = payload.get("price_currency")
    deal = payload.get("deal_type")
    return Listing(
        price=price,
        currency=currency.strip().upper() if isinstance(currency, str) and currency.strip() else None,
        area=_positive(payload.get("area_m2")),
        rooms=int(rooms) if isinstance(rooms, int | float) and not isinstance(rooms, bool) and rooms > 0 else None,
        deal=deal if deal in ("rent", "sale") else None,
        location=place,
        title=title,
        url=url or (str(payload["original_post_link"]) if payload.get("original_post_link") else None),
        floor=int(floor) if isinstance(floor, int | float) and not isinstance(floor, bool) else None,
        address=str(payload["address"]).strip() if isinstance(payload.get("address"), str) else "",
        district=str(payload["district"]).strip() if isinstance(payload.get("district"), str) else "",
        shingles=shingles,
        words=words,
        phones=phones_of(excerpt),
    )


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c))


def _clean_words(text: str) -> list[str]:
    """Casefolded, accent-free words of a post: URLs, hashtags, emojis and punctuation gone, digits kept."""
    text = _HASHTAG.sub(" ", _URL.sub(" ", text or ""))
    return _WORD.findall(_fold(text.casefold()))


def fingerprint(text: str) -> tuple[frozenset[str], int]:
    """(5-word shingles, word count) of a listing body; a text shorter than 5 words is one shingle."""
    words = _clean_words(text)
    if not words:
        return frozenset(), 0
    if len(words) < SHINGLE_WORDS:
        return frozenset({" ".join(words)}), len(words)
    return frozenset(" ".join(words[i:i + SHINGLE_WORDS]) for i in range(len(words) - SHINGLE_WORDS + 1)), len(words)


def shingle_similarity(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard of two shingle sets (0.0 when either is empty)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def text_similarity(a: str, b: str) -> float:
    """Jaccard similarity of the 5-word shingles of two listing texts."""
    return shingle_similarity(fingerprint(a)[0], fingerprint(b)[0])


def phones_of(text: str) -> frozenset[str]:
    """Phone numbers in a text as normalised digits (the last 9 digits, so +34 / 0034 / none agree); >= 9 digits only."""
    out = set()
    for found in _PHONE.findall(_URL.sub(" ", text or "")):
        digits = re.sub(r"\D", "", found)
        if len(digits) >= MIN_PHONE_DIGITS and len(digits) <= 15:
            out.add(digits[-MIN_PHONE_DIGITS:])
    return frozenset(out)


def main_price(text: str) -> float | None:
    """The first amount in a text written with a currency sign / code («199.000 €», «€ 199 000»), or None."""
    for found in _PRICE_TEXT.finditer(text or ""):
        digits = re.sub(r"\D", "", found.group("a") or found.group("b") or "")
        if digits and (value := _positive(float(digits))) and value >= 1000:
            return value
    return None


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


def _house_number(address: str) -> str | None:
    found = re.search(r"(?<![\w.])(\d{1,4})(?![\w])", _fold(address))
    return found.group(1) if found else None


def _host(url: str | None) -> str:
    return site_of(url)


def same_object(a: Listing, b: Listing) -> bool:
    """True only when the two listings are almost surely one property (see the module notes)."""
    if a.deal and b.deal and a.deal != b.deal:
        return False
    if a.currency and b.currency and a.currency != b.currency:
        return False
    if a.rooms and b.rooms and a.rooms != b.rooms:
        return False
    if a.floor is not None and b.floor is not None and a.floor != b.floor:
        return False  # one building, two flats
    number_a, number_b = _house_number(a.address), _house_number(b.address)
    if number_a and number_b and number_a != number_b:
        return False
    price = _close(a.price, b.price, PRICE_TOLERANCE)
    area = _close(a.area, b.area, AREA_TOLERANCE)
    if price is False or area is False:
        return False
    if _same_text(a, b, price):
        return True
    if price is None and area is None:
        return False
    place_a, place_b = tokens(a.location), tokens(b.location)
    street_a, street_b = tokens(a.address), tokens(b.address)
    both_addressed = bool(a.address.strip() and b.address.strip())
    if both_addressed:
        # A district alone does not identify a flat: the street must be shared (and the house number, when both state one).
        if not (street_a & street_b):
            return False
        shared_place = True
    else:
        shared_place = bool(place_a & place_b)
    full = price is not None and area is not None
    host_a, host_b = _host(a.url), _host(b.url)
    if host_a and host_a == host_b and a.url != b.url:
        # Two ads of one portal: the same flat only with the same address and floor (or the same address, price and
        # area while the floor is unknown on both sides).
        if not (both_addressed and (street_a & street_b)):
            return False
        same_floor = a.floor is not None and a.floor == b.floor
        if not (same_floor or (a.floor is None and b.floor is None and full)):
            return False
    words_a, words_b = _title_words(a.title), _title_words(b.title)
    same_title = full and len(words_a) >= MIN_TITLE_WORDS and len(words_b) >= MIN_TITLE_WORDS and _jaccard(words_a, words_b) >= TITLE_JACCARD
    if not (shared_place or same_title):
        return False
    if price is None or area is None:  # one dimension unconfirmed: the rest must agree explicitly
        return bool(a.rooms and b.rooms and shared_place)
    return True


def _same_text(a: Listing, b: Listing, price: bool | None) -> bool:
    """The body-text rules (see the module notes); the negative rules have already passed."""
    host_a, host_b = _host(a.url), _host(b.url)
    if host_a and host_a == host_b and a.url != b.url and host_a not in _SOCIAL_HOSTS:
        return False  # one portal does not list a flat twice; a template ad of an agency is not a duplicate
    similarity = shingle_similarity(a.shingles, b.shingles)
    if similarity >= TEXT_SAME and a.words >= TEXT_SAME_MIN_WORDS and b.words >= TEXT_SAME_MIN_WORDS:
        return True
    if price is True and a.phones & b.phones:
        return True
    return price is True and similarity >= TEXT_PRICE_JACCARD and bool(a.rooms and a.rooms == b.rooms)


def site_of(url: str | None) -> str:
    """The site's host without ``www.`` / ``m.``, for the «Также на» line."""
    host = (urlsplit(url or "").hostname or "").lower()
    return host.removeprefix("www.").removeprefix("m.")
