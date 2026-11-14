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
* the same phone number and the price within 2 %, plus the area known on both sides (within 3 %) or the same rooms
  (both known) with text Jaccard >= 0.60: the same object (an agent's phone is on all his ads);
* the small numbers of the two texts (floor, door, m2, rooms, «primera» / «tercera» ...) must be the same list, else
  no text rule applies; two addresses without a common street never merge, by any rule;
* text Jaccard >= 0.60, the price within 2 % and the same rooms (both known): the same object.

Across two different sites the same price (within 1 %), area (3 %), rooms (or none on both) and city merge when the
phone, the agency or at least two features are shared too, even if the districts are named differently.

Across two sites when the districts differ in the address too (``same_object(..., cross_portal_facts=True)``,
``DEDUP_CROSS_PORTAL_FACTS``, Q32): without the flag two addresses without a shared street token never reach the rule
above (Fotocasa «Las Lomas de Río Verde» and Habitaclia «Milla de Oro» for one villa). With it, such a pair merges by
such a pair merges only with exact facts and a strong sign (``_same_cross_facts``): the price equal when both are
round (a multiple of 1 000, or of 50 below 10 000), else within 0.5 %; the area within 3 %; the rooms and the property
type known on both sides and equal; one named city; no new development; not two named streets (two streets that
differ are two objects) and a street on one side only with a shared district token; two agency references, when both
are known, equal. The strong sign is one of: a shared phone, the same strong agency reference (``strong_ref``) on both
sides when the agencies are not two named ones (as in ``_same_reference``), or the same description (the post body
without its «JSON-LD», title and «Ссылка» lines, compared on the common length of the two, at least 400 characters:
5-word shingles Jaccard >= 0.60, the same small numbers, at least 15 words). Shared features are not
one (most villas of a town share «piscina» and «garaje»), nor is the agency name (one agency lists several houses at
one price in two districts; a franchise is many agencies).

Two ads of one portal are not merged by text (only Facebook, where one flat is posted in many groups, is).

One ad filed twice on one portal (``same_object(..., same_host_copy=True)``, ``DEDUP_SAME_HOST_COPY``, Q47): an
advertiser lists one estate under two addresses (Idealista «San Juan» and «Urbanova», 14 km apart). Two ads of one
portal (not a social network) are one object when all hold: the same description (``_same_description`` with the
stricter ``COPY_JACCARD``: on the common length of at least 400 characters, the same small numbers, 5-word shingles
Jaccard >= 0.90); a shared phone or the same agency; the price exactly equal and the area within 1 % (both known; a
plot's ``plot_m2`` when there is no ``area_m2``); the property type and the deal known on both sides and equal; the
same rooms and floor (or unknown on both); the two addresses (with the locations) share no street or district token
(``_DISTRICT_STOP`` dropped: one urbanization's typical plots share it, and a copy at the same address is the rule of
one host above). Never: a new development, two named streets (``_named_street`` on both sides: units of a development
not recognised as one), two agency references that differ. An agency template for other units differs in its numbers
(floor, m², rooms) and stays two cards; numbers past the compared excerpt (about 1 100 characters of the description
after the «JSON-LD» line) are not seen.

One description on two portals with a wrapper (``same_object(..., description_normalize=True)``,
``DEDUP_DESCRIPTION_NORMALIZE``, Q50): Milanuncios shows an agency's Fotocasa ad with «Ref: Ausias Marc 127.» before
the same text, no street, no phone (``stripSellerPII``), no agency and another district name («Eixample» for «Fort
Pienc»); the reference adds a small number and the shorter «JSON-LD» line moves the excerpt, so no text rule holds.
Two sites (no social network), at least one description under a leading «Ref: ...» wrapper, are one object when all
hold: the same strong reference (``strong_ref``) on both sides (the agency's ``agency_ref`` or the wrapper's one: a
reference on one side only may be a template of another unit); the descriptions without the wrapper
(``normalized_description``) are the same (``_same_description`` with ``COPY_JACCARD``); the price equal as in
``_cross_price``; the area within 3 % (both known); the rooms, the property type and the deal known on both sides and
equal; one named city. Never: a new development, two named streets without a shared street token. Two aggregators renting one flat (Spotahome and
HousingAnywhere) write other texts and other references: two cards.

One plot or lot offered several times (``same_object(..., same_lot=True)``, ``HELD_OFFER_QUALITY``, Q55): two
agencies sell one lot of 31 rustic plots on Idealista at 357 900 € and 396 400 € (other municipality in the address,
the same two cadastral references in both texts), and three agencies list one lot at one street with one description
and one price, the area not stated (the rule of one host needs the floor or the area). Such pairs are one object:
both texts state whole cadastral references (20 characters, ``_PARCEL``) and the same set, both ``land`` and
``sale``, the area within 1 % (``_same_parcels``; the price may differ); or two ads of one portal (no social network)
at one address (a shared street token), the area unknown on both sides, the same description (``COPY_JACCARD``, the
same small numbers), the price exactly equal, both ``land``, the deal known and equal, the same rooms, no floor, no
new development and no two agency references that differ (``_same_listed_lot``; the agencies may differ).

A district for a street (``same_object(..., district_not_street=True)``, ``DEDUP_DISTRICT_NOT_STREET``, Q15): portals
fill the «address» of an ad without a street with the district, and two portals name it differently («L'Amistat» and
«Algirós»). When one side's address has no street (no street word such as «calle», «C/», «avenida», «plaza»,
«carrer» and no house number), the shared street is not required,
but such a pair (no shared street token) merges only by the agency reference or by the text rules, never by the
facts rule; a street on one side and no shared district token is never the same object. The street and place tokens
drop the district stop words (``_DISTRICT_STOP``: ``_PLACE_STOP`` and «capital» of «Valencia Capital», with the flag
only). The agency's own reference of the ad (``agency_ref``: «agencyReference» of Fotocasa, «agencyRef» of Habitaclia,
in the post's «JSON-LD» line) is strong when it has letters and digits or at least 5 digits (no year, no word): across
two sites the same strong reference on both sides with the price, area, rooms and city matching is the same object
(never for a new development); two different references are two objects when a street is missing or the advertiser is
the same (one agency, two flats in neighbouring districts at one price).

Without an address (``same_object(..., without_address=True)``, ``DEDUP_WITHOUT_ADDRESS``; at least one side has no
street address, so the rules above never merge two ads of one portal): only plots of land for sale, one plot listed
by several agencies. Both are ``land`` and ``sale``, the same rooms (or none on both), the price within 1 %, the area
within 2 % (both known; ``plot_m2`` when there is no ``area_m2``), the same city and, when both name a district, a
shared district token (generic words such as «urbanización», «costa», «residencial» and regions dropped). The texts'
small numbers (zone codes aside, the zones are compared on their own) must be the same list. Never merged: a new development («obra nueva»,
«promoción», «Residencial X»), the same agency or phone on one portal (one advertiser lists several plots there), two
plots with a round area (a multiple of 50 m², typical plots of one estate) unless both texts state the same
cadastral reference (one zone covers a whole estate). A planning zone («N.Z. 8.1.A», «norma zonal 8.1.A») or a
cadastral reference stated in both texts must agree.

``object_key`` is an index hint (same key = very likely the same object), built from the
location tokens, rooms and logarithmic price / area buckets; ``None`` when price and area
are both unknown. ``same_object`` is the arbiter.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from .architect import find_places

PRICE_TOLERANCE = 0.02
AREA_TOLERANCE = 0.03
FACTS_PRICE_TOLERANCE = 0.01  # the cross-portal «same facts» rule is stricter than ``PRICE_TOLERANCE``
BARE_PRICE_TOLERANCE = 0.01  # the rule without an address (``_same_bare_facts``)
BARE_AREA_TOLERANCE = 0.02
BARE_ROUND_AREA = 50  # a plot area that is a multiple of this is a typical estate plot
FACTS_MIN_FEATURES = 2
CROSS_PRICE_TOLERANCE = 0.005  # Q32 (``_same_cross_facts``): a price that is not round
CROSS_ROUND_PRICE = 1000  # Q32: a multiple of this is a round price (compared exactly)
CROSS_SMALL_PRICE = 10_000  # Q32: below it (a rent) a multiple of ``CROSS_ROUND_RENT`` is round too
CROSS_ROUND_RENT = 50
# Q32: the descriptions are compared on their common length (the «JSON-LD» line ends the excerpt anywhere), at least
# this many characters: a short text is mostly an agency's opening words
DESCRIPTION_MIN_CHARS = 400
# Q47 (``_same_host_copy``): one portal's two ads of one object share the description almost word for word (a template
# of an agency for two units keeps its words too, so the numbers decide); stricter than ``TEXT_PRICE_JACCARD``
COPY_JACCARD = 0.90
COPY_AREA_TOLERANCE = 0.01
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
_ZONE = re.compile(r"(?<!\w)(?:n\s?\.?\s?z\s?\.?|norma\s+zonal)\s*:?\s*(\d{1,2}(?:\s?\.\s?\d{1,2}){0,3}(?:\s?\.\s?[a-z]|[a-z])?(?!\w))"
                   r"(?:\s*,?\s*grado\s*(\d{1,2})\s*\.?\s*[ºo°]?\s*,?\s*(?:nivel\s*([a-z])(?!\w))?)?", re.IGNORECASE)
_CADASTRE = re.compile(r"ref(?:erencia|\.)?\s*catastral\s*:?\s*([0-9a-z]{14})(?:[0-9a-z]{6})?(?!\w)", re.IGNORECASE)
# Q55: a whole Spanish cadastral reference anywhere in the text (20 characters: the 14-character parcel, 4 digits and
# 2 control letters), rustic («28124A005003650000JR») or urban («9872023VH5797S0001WX»)
_PARCEL = re.compile(r"(?<![0-9a-z])(\d{5}[a-z]\d{8}|\d{7}[a-z]{2}\d{4}[a-z])\d{4}[a-z]{2}(?![0-9a-z])", re.IGNORECASE)
_NEW_BUILD_NAME = re.compile(r"(?<!\w)(?:[Rr]esidencial|RESIDENCIAL)\s+(?:[A-ZÁÉÍÓÚÑ]|(?:[Ee]l|[Ll]a|[Ll]os|[Ll]as)\s+[A-ZÁÉÍÓÚÑ])")
_NEW_BUILD_WORDS = re.compile(r"(?<!\w)(?:obra\s+nueva|promoci[oó]n|(?:complejo|conjunto)\s+residencial)(?!\w)",
                              re.IGNORECASE)
# «Residencial» as a zoning, not a complex: «Residencial Unifamiliar», «Uso: Residencial», «Zona Residencial X».
_ZONING_AFTER = re.compile(r"\s*(?:unifamiliar|plurifamiliar|multifamiliar|intensiva|extensiva)(?!\w)", re.IGNORECASE)
_ZONING_BEFORE = frozenset({"uso", "usos", "suelo", "calificacion", "urbanizacion"})  # «zona» only right before
_SENTENCE_END = re.compile(r"[.!?;\n](?=\s|$)")
_PROMOTION_PRICE = re.compile(r"precio\s+de\s*$", re.IGNORECASE)
# District words that do not identify a place (estates, new builds, coasts) and Spain's regions.
_PLACE_STOP = frozenset({
    "urbanizacion", "urbanizaciones", "urb", "obra", "nueva", "nuevo", "residencial", "promocion", "complejo",
    "conjunto", "costa", "sol", "playa", "mar", "golf", "alto", "alta", "norte", "sur", "este", "oeste", "san",
    "santa", "parcela", "parcelas", "solar", "terreno", "terrenos", "urbano", "rustico", "sector", "poligono",
    "andalucia", "cataluna", "catalunya", "comunidad", "comunitat", "valenciana", "aragon", "asturias", "cantabria",
    "galicia", "extremadura", "navarra", "rioja", "pais", "vasco", "euskadi", "baleares", "balears", "illes", "islas",
    "canarias", "castilla", "mancha", "leon", "murcia", "region", "provincia", "blanca", "brava", "dorada", "calida",
    "tropical", "azahar", "luz", "maresme", "garraf",
})
# Q15 (``DEDUP_DISTRICT_NOT_STREET``): the district stop words also drop «capital» («Valencia Capital»).
_DISTRICT_STOP = _PLACE_STOP | {"capital"}
# A street in an address: a street word (Spanish, Catalan, Valencian, abbreviations) or a house number.
_STREET_WORD = re.compile(
    r"(?<![^\W_])(?:calle|c|cl|cll|avenida|avinguda|avda|av|plaza|placa|pza|pl|carrer|paseo|passeig|pg|pso|camino|cami"
    r"|ronda|travesia|carretera|ctra|glorieta|callejon|via|rambla|bulevar|boulevard|street|road|avenue|square|улица"
    r"|ул|проспект)(?![^\W_])")
# The agency's reference in the post's «JSON-LD» line (``SourceListing.agency_ref``, Q15).
# Street words that also name districts («Camí Fondo», «Gran Vía», «La Rambla»): a street only when the address is
# more than the listing's district (``_named_street``); the other street words are always a street.
_DISTRICT_STREET_WORD = re.compile(r"(?<![^\W_])(?:camino|cami|via|ronda|rambla|bulevar|boulevard|glorieta)(?![^\W_])")
# Q50 (``DEDUP_DESCRIPTION_NORMALIZE``): a portal's «Ref: <agency reference>.» before the description (Milanuncios).
_REF_WRAPPER = re.compile(r"\A\s*ref(?:erencia)?\s*\.?\s*:\s*([^\n.]{1,40})\.\s*", re.IGNORECASE)
_AGENCY_REF_FIELD = re.compile(r',?\s*"agency_ref":\s*"((?:[^"\\]|\\.)*)"')
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
    numbers: tuple[int, ...] = ()  # the small numbers of the body (floor, door, m2, rooms ...), sorted
    agency: str = ""  # normalised advertiser / agency name (``agency_key``), "" when unknown
    features: frozenset[str] = frozenset()  # lower-case features of the payload (terraza, exterior ...)
    kind: str | None = None  # the payload's property_type (land, apartment ...)
    plot: float | None = None  # the payload's plot_m2
    zones: frozenset[str] = frozenset()  # planning zones of the body («N.Z. 8.1.A» -> "8.1.a")
    bare_numbers: tuple[int, ...] = ()  # ``numbers`` without the zone codes (the rule without an address)
    cadastre: frozenset[str] = frozenset()  # cadastral references of the body (the 14-character parcel part)
    parcels: frozenset[str] = frozenset()  # Q55: whole cadastral references anywhere in the body (``_PARCEL``)
    new_build: bool = False  # the body, title or location names a new development («obra nueva», «promoción» ...)
    agency_ref: str = ""  # the agency's own reference of the ad (``ref_key``), "" when unknown (Q15)
    # Q32: the description only (the body excerpt without a portal post's «JSON-LD», title and «Ссылка» lines,
    # ``description_of``)
    description: str = ""


def _positive(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def listing_of(payload: dict[str, Any] | None, *, url: str | None = None, text: str = "",
               body: str = "") -> Listing:
    """The comparison fields of a stored finding payload; ``body`` is the post text (``collected_posts.body_text``;
    its first ``TEXT_LIMIT`` characters are fingerprinted). Both sides of a comparison use the post body only: the
    card / summary ``text`` is never a stand-in (it would be another text source on one side), it only titles."""
    payload = payload or {}
    excerpt = (body or "")[:TEXT_LIMIT]
    first_line = excerpt.lstrip().split("\n", 1)[0]
    found_ref = _AGENCY_REF_FIELD.search(first_line) if first_line.startswith("JSON-LD:") else None
    if found_ref:  # its digits are no detail of the flat (``numbers``) and no phone
        excerpt = excerpt.replace(found_ref.group(0), "", 1)
    shingles, words = fingerprint(excerpt)
    description = description_of(excerpt)
    price = _positive(payload.get("price_amount"))
    if price is None:
        price = main_price(excerpt)
    rooms = payload.get("rooms")
    place = " ".join(str(payload[k]) for k in ("location", "address", "district") if isinstance(payload.get(k), str))
    floor = payload.get("floor")
    title = str(payload.get("title") or payload.get("summary_ru") or payload.get("summary") or text or "")
    currency = payload.get("price_currency")
    deal = payload.get("deal_type")
    raw_agency = next((payload[k] for k in ("agency", "advertiser") if isinstance(payload.get(k), str)), "")
    raw_features = payload.get("features")
    features = frozenset(_fold(f).strip() for f in raw_features if isinstance(f, str) and f.strip()) \
        if isinstance(raw_features, list | tuple) else frozenset()
    stated_phones = phones_of(" ".join(str(payload[k]) for k in ("phone", "contact_phone") if payload.get(k)))
    kind = payload.get("property_type")
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
        phones=phones_of(excerpt) | stated_phones,
        numbers=numbers_of(excerpt),
        agency=agency_key(raw_agency),
        features=features,
        kind=kind.strip().lower() if isinstance(kind, str) and kind.strip() else None,
        plot=_positive(payload.get("plot_m2")),
        zones=zones_of(excerpt),
        bare_numbers=numbers_of(_ZONE.sub(" ", excerpt)),
        cadastre=frozenset(found.upper() for found in _CADASTRE.findall(excerpt)),
        parcels=frozenset(found.group(0).upper() for found in _PARCEL.finditer(excerpt)),
        new_build=is_new_build(" ".join((excerpt, title, place))),
        agency_ref=ref_key(found_ref.group(1)) if found_ref else "",
        description=description,
    )


def description_of(body: str) -> str:
    """Q32: the description of a post body. A portal post (``web_search.worker._source_text``) is «JSON-LD: {...}», its
    title, «Ссылка: ...» and the description; another post with the «JSON-LD» line loses that line; else the body."""
    lines = (body or "").lstrip().split("\n")
    if not lines[0].startswith("JSON-LD:"):
        return body or ""
    rest = lines[1:]
    if len(rest) >= 2 and rest[1].startswith("Ссылка:"):
        rest = rest[2:]
    return "\n".join(rest)


def ref_key(value: str | None) -> str:
    """An agency reference for comparison: lower case letters and digits only («REO-111-860» -> "reo111860"); "" when
    shorter than 3 characters (no identifier)."""
    key = "".join(_WORD.findall(_fold(value or "")))
    return key if len(key) >= 3 else ""


def has_street(address: str) -> bool:
    """The address names a street (a street word or a house number), not only a district or a city."""
    return bool(_STREET_WORD.search(_fold(address or "")) or _house_number(address or ""))


def is_new_build(text: str) -> bool:
    """«obra nueva», «promoción», «complejo / conjunto residencial» or a «Residencial <Name>» complex. A zoning is not
    one: «uso residencial», «Residencial Unifamiliar», «Uso: Residencial», «Calificación: Residencial ...», «Zona /
    Urbanización Residencial X»; nor is a «precio de promoción»."""
    text = text or ""
    for found in _NEW_BUILD_WORDS.finditer(text):
        if not (found.group(0).lower().startswith("promoci") and _PROMOTION_PRICE.search(text[:found.start()])):
            return True
    for found in _NEW_BUILD_NAME.finditer(text):
        word_end = found.start() + len("residencial")
        head = text[max(0, found.start() - 60):found.start()]
        sentence = _SENTENCE_END.split(head)[-1]  # the words before it in its own sentence
        before = _WORD.findall(_fold(sentence))[-3:]
        zoning = _ZONING_BEFORE & set(before) or (before and before[-1] == "zona")
        if not (_ZONING_AFTER.match(text, word_end) or zoning):
            return True
    return False


def zones_of(text: str) -> frozenset[str]:
    """Planning zones a text states as dotted parts: «N. Z. 8.1. A», «norma zonal 8.1.A» and «Norma Zonal 8, grado
    1.º, nivel A» -> "8.1.a"; «N.Z. 8» -> "8"."""
    out = set()
    for code, grade, level in _ZONE.findall(text or ""):
        parts = re.findall(r"\d+|[a-z]", code.lower()) + [p for p in (grade, level.lower()) if p]
        out.add(".".join(parts))
    return frozenset(out)


def _zones_agree(a: frozenset[str], b: frozenset[str]) -> bool:
    """Some zone of one side equals a zone of the other or is a less precise form of it («8» and «8.1.a»)."""
    return any(x == y or y.startswith(x + ".") or x.startswith(y + ".") for x in a for y in b)


_LEGAL_FORM = re.compile(r"\b(?:s\.?\s?l\.?\s?u\.?|s\.?\s?l\.?|s\.?\s?a\.?)(?=\W|$)", re.IGNORECASE)


def agency_key(name: str | None) -> str:
    """An agency name for comparison: lower case, accent-free, without «S.L.» / «S.L.U.» / «S.A.» and punctuation."""
    text = _LEGAL_FORM.sub(" ", _fold(name or ""))
    return " ".join(_WORD.findall(text))


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
    """The amount of a text written with a currency sign / code («199.000 €», «€ 199 000»), or None.

    Only when the text states exactly one such amount (the same amount repeated is still one): a post with two
    prices («1200 € + 3000 € deposit») has no single price to compare on.
    """
    values = set()
    for found in _PRICE_TEXT.finditer(text or ""):
        digits = re.sub(r"\D", "", found.group("a") or found.group("b") or "")
        if digits and (value := _positive(float(digits))) and value >= 1000:
            values.add(value)
    return next(iter(values)) if len(values) == 1 else None


_NUMBER_WORDS = {
    "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10,
    "primero": 1, "primera": 1, "primer": 1, "segundo": 2, "segunda": 2, "tercero": 3, "tercera": 3, "tercer": 3,
    "cuarto": 4, "cuarta": 4, "quinto": 5, "quinta": 5, "sexto": 6, "sexta": 6, "septimo": 7, "septima": 7,
    "octavo": 8, "octava": 8, "noveno": 9, "novena": 9, "decimo": 10, "decima": 10,
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5, "дві": 2, "чотири": 4,
    "первый": 1, "второй": 2, "третий": 3, "четвертый": 4, "пятый": 5,
    "перший": 1, "другий": 2, "третій": 3, "четвертий": 4,
}
_BIG_GROUPED = re.compile(r"\d{1,3}(?:[.,\u00a0 ]\d{3})+")
_AREA_UNIT = re.compile(r"\bm\s?[2\u00b2]|\bм\s?[2\u00b2]", re.IGNORECASE)
_SMALL_NUMBER = re.compile(r"(?<![\d])\d{1,3}(?![\d])")


def numbers_of(text: str) -> tuple[int, ...]:
    """The sorted small numbers (1-999) of a post, number / ordinal words included («Planta tercera» -> 3), without
    phones, prices and URLs: the identifying details (floor, door, m2, rooms) that two ads of one flat share."""
    text = _HASHTAG.sub(" ", _URL.sub(" ", text or ""))
    text = _PRICE_TEXT.sub(" ", text)
    text = _PHONE.sub(lambda m: " " if len(re.sub(r"\D", "", m.group(0))) >= MIN_PHONE_DIGITS else m.group(0), text)
    text = _BIG_GROUPED.sub(" ", text)
    text = _AREA_UNIT.sub(" m ", text)
    found = [int(n) for n in _SMALL_NUMBER.findall(text) if int(n) > 0]
    found += [_NUMBER_WORDS[w] for w in _WORD.findall(_fold(text.casefold())) if w in _NUMBER_WORDS]
    return tuple(sorted(found))


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


def same_object(a: Listing, b: Listing, *, without_address: bool = False, district_not_street: bool = False,
                cross_portal_facts: bool = False, same_host_copy: bool = False,
                description_normalize: bool = False, same_lot: bool = False) -> bool:
    """True only when the two listings are almost surely one property (see the module notes); ``without_address``
    (``DEDUP_WITHOUT_ADDRESS``) adds the rule for listings without a street address (``_same_bare_facts``);
    ``district_not_street`` (``DEDUP_DISTRICT_NOT_STREET``, Q15) does not require a shared street when an address is
    only a district and compares the agency references; ``cross_portal_facts`` (``DEDUP_CROSS_PORTAL_FACTS``, Q32)
    lets two portals' addresses without a shared street merge by exact facts and a strong sign (``_same_cross_facts``);
    ``same_host_copy`` (``DEDUP_SAME_HOST_COPY``, Q47) merges one ad filed twice on one portal (``_same_host_copy``);
    ``description_normalize`` (``DEDUP_DESCRIPTION_NORMALIZE``, Q50) merges one description on two sites under a
    «Ref: ...» wrapper (``_same_wrapped_description``); ``same_lot`` (``HELD_OFFER_QUALITY``, Q55) merges one plot
    with the same whole cadastral references at two prices (``_same_parcels``) and one portal's two ads of one
    address with the same description and price while the area is unknown (``_same_listed_lot``)."""
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
    if same_lot and _same_parcels(a, b):
        return True  # Q55: one plot (its cadastral references) offered by two agencies at two prices
    price = _close(a.price, b.price, PRICE_TOLERANCE)
    area = _close(a.area, b.area, AREA_TOLERANCE)
    if price is False or area is False:
        return False
    street_a, street_b = tokens(a.address), tokens(b.address)
    both_addressed = bool(a.address.strip() and b.address.strip())
    stop = _DISTRICT_STOP if district_not_street else _PLACE_STOP
    place_a, place_b = tokens(a.location), tokens(b.location)
    both_streets = False
    if district_not_street:
        street_a, street_b, place_a, place_b = (t - _DISTRICT_STOP for t in (street_a, street_b, place_a, place_b))
        both_streets = _named_street(a) and _named_street(b)
        if a.agency_ref and b.agency_ref and a.agency_ref != b.agency_ref and (
                not both_streets or a.phones & b.phones or (a.agency and a.agency == b.agency)):
            return False  # another reference of the agency: another flat
    if same_host_copy and _same_host_copy(a, b):
        return True  # Q47: one ad filed twice on one portal, under two addresses
    if description_normalize and _same_wrapped_description(a, b):
        return True  # Q50: one description on two sites, one under a «Ref: ...» wrapper
    if both_addressed and not (street_a & street_b):
        if cross_portal_facts and _same_cross_facts(a, b):
            return True  # Q32: one object, two portals naming the district differently
        if not district_not_street or both_streets:
            return False  # a district alone does not identify a flat: the street must be shared (every rule, text included)
        # Q15: an address is only a district, named differently by two portals: the agency reference or the text only
        if _named_street(a) != _named_street(b) and not (place_a & place_b):
            return False  # a street on one side, another district on the other
        return _same_reference(a, b, price, area) or (
            not a.new_build and not b.new_build and _same_text(a, b, price, area))  # a template of a development
    if _same_facts(a, b):
        return True
    if without_address and not both_addressed and _same_bare_facts(a, b, stop):
        return True
    if _same_text(a, b, price, area):
        return True
    if price is None and area is None:
        return False
    shared_place = True if both_addressed else bool(place_a & place_b)
    full = price is not None and area is not None
    host_a, host_b = _host(a.url), _host(b.url)
    if host_a and host_a == host_b and a.url != b.url:
        # Two ads of one portal: the same flat only with the same address and floor (or the same address, price and
        # area while the floor is unknown on both sides).
        if not (both_addressed and (street_a & street_b)):
            return False
        if same_lot and host_a not in _SOCIAL_HOSTS and _same_listed_lot(a, b, area):
            return True  # Q55: one lot of one address listed by several agencies, its area not stated
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


def _named_street(listing: Listing) -> bool:
    """Q15: the address names a street (``has_street``). Only a street word that also names districts
    (``_DISTRICT_STREET_WORD``) in an address that is just the listing's district names none («Camí Fondo» is a
    district of Valencia: «Camí Fondo, Valencia» with that district); «Calle Colón» is a street, district Colón or not."""
    if not has_street(listing.address):
        return False
    folded = _fold(listing.address)
    if _house_number(listing.address) or _STREET_WORD.sub(" ", _DISTRICT_STREET_WORD.sub(" ", folded)) != \
            _DISTRICT_STREET_WORD.sub(" ", folded):
        return True  # a house number or another street word («calle», «C/», «plaza» ...)
    own = tokens(listing.address) - _DISTRICT_STOP
    return not own or not own <= tokens(listing.district)


def _cities(listing: Listing) -> frozenset[str]:
    return frozenset(find_places(" ".join((listing.location, listing.address, listing.district))))


def strong_ref(key: str) -> bool:
    """An agency reference (``ref_key``) that identifies an ad: letters and digits, or at least 5 digits; a year
    (1900-2099) or a word («torrefiel») does not."""
    digits = sum(c.isdigit() for c in key)
    if re.fullmatch(r"(?:19|20)\d\d", key):
        return False
    return digits >= 5 or (digits > 0 and digits < len(key))


def _same_reference(a: Listing, b: Listing, price: bool | None, area: bool | None) -> bool:
    """Q15: across two sites the same strong agency reference (on both sides), the price and the area matching (both
    known), the same rooms (or none on both) and one city; never for a new development (one reference per project) or two
    agencies named on both sides."""
    host_a, host_b = _host(a.url), _host(b.url)
    if not host_a or not host_b or host_a == host_b or a.new_build or b.new_build:
        return False
    if a.agency and b.agency and a.agency != b.agency:
        return False  # two agencies: one reference by chance
    return bool(a.agency_ref and a.agency_ref == b.agency_ref and strong_ref(a.agency_ref) and price is True
                and area is True and a.rooms == b.rooms and _cities(a) == _cities(b))


def _same_facts(a: Listing, b: Listing) -> bool:
    """Strong match of facts across two portals, whatever the districts are called: the price within 1 %, the area
    within 3 % (both known), the same rooms (or both unknown), one city (or none named on both sides), and a shared
    phone, the same agency or at least two shared features. Two ads of one portal never merge here."""
    host_a, host_b = _host(a.url), _host(b.url)
    if not host_a or not host_b or host_a == host_b:
        return False
    if _close(a.price, b.price, FACTS_PRICE_TOLERANCE) is not True or _close(a.area, b.area, AREA_TOLERANCE) is not True:
        return False
    if a.rooms != b.rooms:
        return False
    if _cities(a) != _cities(b):
        return False
    return bool(a.phones & b.phones) or bool(a.agency and a.agency == b.agency) \
        or len(a.features & b.features) >= FACTS_MIN_FEATURES


def _round_price(price: float) -> bool:
    return price % CROSS_ROUND_PRICE == 0 or (price < CROSS_SMALL_PRICE and price % CROSS_ROUND_RENT == 0)


def _cross_price(a: float | None, b: float | None) -> bool:
    """Q32: a round price is copied from the agency's feed as is (999 000 and 995 000 are two objects); another within
    ``CROSS_PRICE_TOLERANCE``."""
    if a is None or b is None:
        return False
    if _round_price(a) and _round_price(b):
        return a == b
    return _close(a, b, CROSS_PRICE_TOLERANCE) is True


def _same_description(a: Listing, b: Listing, jaccard: float = TEXT_PRICE_JACCARD) -> bool:
    """Q32: one description reposted on two portals, compared on the common length of the two (at least
    ``DESCRIPTION_MIN_CHARS``): Jaccard of the 5-word shingles >= ``jaccard``, at least
    ``TEXT_SAME_MIN_WORDS`` words and the same small numbers (another floor or door is another flat of a template)."""
    size = min(len(a.description), len(b.description))
    if size < DESCRIPTION_MIN_CHARS:
        return False
    text_a, text_b = a.description[:size], b.description[:size]
    if numbers_of(text_a) != numbers_of(text_b):
        return False
    (shingles_a, words_a), (shingles_b, words_b) = fingerprint(text_a), fingerprint(text_b)
    if words_a < TEXT_SAME_MIN_WORDS or words_b < TEXT_SAME_MIN_WORDS:
        return False
    return shingle_similarity(shingles_a, shingles_b) >= jaccard


def _same_host_copy(a: Listing, b: Listing) -> bool:
    """Q47: one ad filed twice on one portal (see the module notes): the same description (``COPY_JACCARD``), the same
    phone or agency, the price exactly and the area within 1 %, the same type, deal, rooms and floor, two addresses
    without a shared street or district token; never a new development, two named streets or two agency references."""
    host_a, host_b = _host(a.url), _host(b.url)
    if not host_a or host_a != host_b or a.url == b.url or host_a in _SOCIAL_HOSTS:
        return False
    if a.new_build or b.new_build or not (a.kind and a.kind == b.kind) or not (a.deal and a.deal == b.deal):
        return False
    if a.rooms != b.rooms or a.floor != b.floor:
        return False
    if a.price is None or a.price != b.price or _close(_bare_area(a), _bare_area(b), COPY_AREA_TOLERANCE) is not True:
        return False
    if a.agency_ref and b.agency_ref and a.agency_ref != b.agency_ref:
        return False  # another reference of the agency: another unit
    if not (a.phones & b.phones or (a.agency and a.agency == b.agency)):
        return False
    if _place_tokens(a) & _place_tokens(b):
        return False  # one street or district: typical plots or units of one estate (the rule of one host decides)
    if _named_street(a) and _named_street(b):
        return False  # two streets that differ: two objects (units of a development not named one)
    return _same_description(a, b, COPY_JACCARD)


def _same_parcels(a: Listing, b: Listing) -> bool:
    """Q55: one plot for sale offered by two agencies (often at two prices): both texts state whole cadastral
    references (``parcels``) and the same set, both are ``land`` and ``sale``, the area within 1 % (both known; a
    plot's ``plot_m2`` when there is no ``area_m2``); never a new development."""
    if not a.parcels or a.parcels != b.parcels or a.new_build or b.new_build:
        return False
    if not (a.kind == b.kind == "land" and a.deal == b.deal == "sale"):
        return False
    return _close(_bare_area(a), _bare_area(b), COPY_AREA_TOLERANCE) is True


def _same_listed_lot(a: Listing, b: Listing, area: bool | None) -> bool:
    """Q55: two ads of one portal at one address (the caller checked a shared street token) whose area is unknown on
    both sides: the same description (``COPY_JACCARD``, the same small numbers), the price exactly equal, both
    ``land``, the deal known and equal, the same rooms and no floor on either; never a new development or two agency
    references that differ. Agencies may differ (a bank's lot resold by its servicers)."""
    if area is not None or a.price is None or a.price != b.price:
        return False
    if a.new_build or b.new_build or not (a.kind == b.kind == "land") or not (a.deal and a.deal == b.deal):
        return False  # a flat's template (one agency, several units, one rent) is not one object
    if a.rooms != b.rooms or a.floor is not None or b.floor is not None:
        return False
    if a.agency_ref and b.agency_ref and a.agency_ref != b.agency_ref:
        return False
    return _same_description(a, b, COPY_JACCARD)


def normalized_description(description: str) -> tuple[str, str]:
    """Q50: (the description without a leading «Ref: <reference>.» wrapper, ``ref_key`` of that reference or "")."""
    found = _REF_WRAPPER.match(description or "")
    if not found:
        return description or "", ""
    return description[found.end():], ref_key(found.group(1))


def _same_wrapped_description(a: Listing, b: Listing) -> bool:
    """Q50: one description on two sites, a «Ref: ...» wrapper dropped (see the module notes)."""
    host_a, host_b = _host(a.url), _host(b.url)
    if not host_a or not host_b or host_a == host_b or host_a in _SOCIAL_HOSTS or host_b in _SOCIAL_HOSTS:
        return False
    if a.new_build or b.new_build or not (a.kind and a.kind == b.kind) or not (a.deal and a.deal == b.deal):
        return False
    if not (a.rooms and a.rooms == b.rooms):
        return False
    if not _cross_price(a.price, b.price) or _close(a.area, b.area, AREA_TOLERANCE) is not True:
        return False
    cities = _cities(a)
    if not cities or cities != _cities(b):
        return False
    if _named_street(a) and _named_street(b) and not ((tokens(a.address) & tokens(b.address)) - _DISTRICT_STOP):
        return False  # two streets that differ: two objects
    text_a, wrapped_a = normalized_description(a.description)
    text_b, wrapped_b = normalized_description(b.description)
    if not (wrapped_a or wrapped_b):
        return False  # no wrapper: the rules of Q32 decide
    ref_a, ref_b = a.agency_ref or wrapped_a, b.agency_ref or wrapped_b
    if not (ref_a and ref_a == ref_b and strong_ref(ref_a)):
        return False  # no reference on one side (a template of another unit) or another reference: another unit
    return _same_description(replace(a, description=text_a), replace(b, description=text_b), COPY_JACCARD)


def _place_tokens(listing: Listing) -> frozenset[str]:
    """Q47: the street and district tokens of a listing (``tokens`` of its address and location, without
    ``_DISTRICT_STOP``)."""
    return (tokens(listing.address) | tokens(listing.location)) - _DISTRICT_STOP


def _same_cross_facts(a: Listing, b: Listing) -> bool:
    """Q32: two portals' addresses without a shared street (the districts named differently): exact facts and a strong
    sign (see the module notes). Shared features and agency names are no sign (one agency lists several houses at one
    price, a franchise is many agencies); no new development (units of one project share price,
    area and rooms); the property type known on both sides; two agency references never merge."""
    host_a, host_b = _host(a.url), _host(b.url)
    if not host_a or not host_b or host_a == host_b:
        return False
    if a.new_build or b.new_build or not (a.rooms and a.rooms == b.rooms) or not (a.kind and a.kind == b.kind):
        return False
    if not _cross_price(a.price, b.price) or _close(a.area, b.area, AREA_TOLERANCE) is not True:
        return False
    cities = _cities(a)
    if not cities or cities != _cities(b):
        return False
    street_a, street_b = _named_street(a), _named_street(b)
    if street_a and street_b:
        return False  # two streets that differ: two objects
    if street_a != street_b and not ((tokens(a.location) - _DISTRICT_STOP) & (tokens(b.location) - _DISTRICT_STOP)):
        return False  # a street on one side, another district on the other
    if a.agency_ref and b.agency_ref and a.agency_ref != b.agency_ref:
        return False  # another reference of the agency: another unit
    same_ref = bool(a.agency_ref and a.agency_ref == b.agency_ref and strong_ref(a.agency_ref)
                    and not (a.agency and b.agency and a.agency != b.agency))  # two agencies: one reference by chance
    return bool(a.phones & b.phones) or same_ref or _same_description(a, b)


def _bare_area(listing: Listing) -> float | None:
    return listing.plot if listing.area is None and listing.kind == "land" else listing.area


def _same_bare_facts(a: Listing, b: Listing, stop: frozenset[str] = _PLACE_STOP) -> bool:
    """The rule without an address (see the module notes): every negative rule of ``same_object`` has passed and at
    least one side has no street address. Plots for sale only; works on two ads of one portal (one plot listed by
    several agencies), so an advertiser shared on one portal means two of its plots, not one."""
    if not (a.kind == b.kind == "land" and a.deal == b.deal == "sale") or a.rooms != b.rooms:
        return False
    if a.new_build or b.new_build or a.bare_numbers != b.bare_numbers:
        return False
    host_a, host_b = _host(a.url), _host(b.url)
    if host_a and host_a == host_b and (a.phones & b.phones or (a.agency and a.agency == b.agency)):
        return False
    if _close(a.price, b.price, BARE_PRICE_TOLERANCE) is not True:
        return False
    area_a, area_b = _bare_area(a), _bare_area(b)
    if _close(area_a, area_b, BARE_AREA_TOLERANCE) is not True:
        return False
    marked = bool(a.cadastre & b.cadastre)  # a zone covers a whole estate: only the parcel tells its plots apart
    if not marked and any(area % BARE_ROUND_AREA == 0 for area in (area_a, area_b) if area is not None):
        return False  # typical plots of one estate: only one cadastral reference stated on both sides is one plot
    if a.zones and b.zones and not _zones_agree(a.zones, b.zones):
        return False  # another planning zone: another plot
    if a.cadastre and b.cadastre and not a.cadastre & b.cadastre:
        return False  # another parcel
    cities = _cities(a)
    if not cities or cities != _cities(b):
        return False
    place_a, place_b = _district_tokens(a, stop), _district_tokens(b, stop)
    return bool(place_a & place_b) if place_a and place_b else True


def _district_tokens(listing: Listing, stop: frozenset[str] = _PLACE_STOP) -> frozenset[str]:
    """The district / neighbourhood tokens of a listing: no street words, no estate / coast words, no regions (and
    with Q15 no «capital», ``_DISTRICT_STOP``)."""
    return frozenset(tokens(listing.location) - tokens(listing.address) - stop)


def _same_text(a: Listing, b: Listing, price: bool | None, area: bool | None) -> bool:
    """The body-text rules (see the module notes); every negative rule has already passed, and the texts' own
    identifying numbers (floor, door, m2, rooms ...) must agree too."""
    host_a, host_b = _host(a.url), _host(b.url)
    if host_a and host_a == host_b and a.url != b.url and host_a not in _SOCIAL_HOSTS:
        return False  # one portal does not list a flat twice; a template ad of an agency is not a duplicate
    if a.numbers != b.numbers:
        return False  # «Planta primera» vs «Planta tercera», other door, other m2: a template, not one flat
    similarity = shingle_similarity(a.shingles, b.shingles)
    if similarity >= TEXT_SAME and a.words >= TEXT_SAME_MIN_WORDS and b.words >= TEXT_SAME_MIN_WORDS:
        return True
    if price is not True:
        return False
    if a.phones & b.phones and (area is True or (a.rooms and a.rooms == b.rooms and similarity >= TEXT_PRICE_JACCARD)):
        return True
    return similarity >= TEXT_PRICE_JACCARD and bool(a.rooms and a.rooms == b.rooms)


def site_of(url: str | None) -> str:
    """The site's host without ``www.`` / ``m.``, for the «Также на» line."""
    host = (urlsplit(url or "").hostname or "").lower()
    return host.removeprefix("www.").removeprefix("m.")
