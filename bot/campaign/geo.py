"""Where a campaign looks, as far as "clearly somewhere else" is concerned. Pure functions, no I/O.

The architect's gazetteer only knows the cities a campaign can target. A
finding (or a search result) can come from anywhere, so this module adds the
little that is needed to tell that it is clearly in another country:

* ``COUNTRY`` -- the ISO-2 country of each gazetteer city;
* ``REGIONS`` -- region names that stand for a city in a search query
  («Comunidad de Madrid»);
* ``foreign_tld`` -- a host under another country's code (``.ru``, ``.ua``,
  ``.by``, ``.kz``, ``.pl`` ...); generic endings (``.com``, ``.net``,
  ``.org``, ``.eu``, ``.info``, ``.io`` ...) are never foreign;
* ``foreign_places`` -- a few well-known cities and country names outside the
  gazetteer (Москва, Минск, Warszawa, Россия ...) with their country.

Only a clear signal counts: an unknown place is never "foreign".
"""

from __future__ import annotations

import re
import unicodedata

from .architect import GAZETTEER, find_places

COUNTRY: dict[str, str] = {place.canonical: ("UA" if place.canonical == "Kyiv" else "ES") for place in GAZETTEER}
# The only currency a listing in that country is priced in (a Spanish listing in roubles is not in Spain).
CURRENCY: dict[str, str] = {"ES": "EUR"}

REGIONS: dict[str, tuple[str, ...]] = {
    "Madrid": ("comunidad de madrid", "madrid region", "region de madrid", "мадридская область", "мадридська область"),
    "Barcelona": ("cataluna", "catalunya", "catalonia", "каталони", "каталоні"),
    "Valencia": ("comunidad valenciana", "comunitat valenciana", "валенсийское сообщество"),
    "Málaga": ("costa del sol", "provincia de malaga", "andalucia", "andalusia"),
    "Alicante": ("costa blanca", "provincia de alicante"),
    "Sevilla": ("provincia de sevilla", "andalucia", "andalusia"),
    "Marbella": ("costa del sol",),
    "Kyiv": ("київська область", "киевская область", "kyiv oblast", "kyiv region"),
}

# Second-level labels that are not a country (``.com.ua`` is still Ukrainian, handled by the last label).
GENERIC_CC = frozenset({"eu", "io", "co", "me", "tv", "ai", "ws", "cc", "fm", "ly", "gg", "to", "app", "so", "sh", "vc"})
# Country codes of places a user never means when asking about a gazetteer city of another country.
_FOREIGN_PLACES: dict[str, tuple[str, ...]] = {
    "RU": ("москв", "moscow", "moskva", "петербург", "petersburg", "подмосков", "росси", "russia", "новосибирск",
           "екатеринбург", "казан", "краснодар", "сочи", "мытищ", "химки", "балаших", "подольск", "красногорск"),
    "BY": ("минск", "мінськ", "minsk", "беларус", "белорус", "belarus"),
    "KZ": ("алмат", "almaty", "астан", "astana", "казахстан", "kazakhstan"),
    "PL": ("варшав", "warszaw", "warsaw", "krakow", "krakó", "краков", "польш", "польщ", "poland", "polska"),
    "UA": ("харьк", "харків", "kharkiv", "одес", "odesa", "odessa", "львов", "львів", "lviv", "днепр", "дніпр",
           "dnipro", "украин", "україн", "ukraine"),
    "ES": ("испани", "іспані", "spain", "españa", "espana"),
    "PT": ("лиссабон", "lisbon", "lisboa", "portugal", "португал"),
    "GE": ("тбилис", "tbilisi", "батуми", "batumi", "грузи", "georgia"),
    "TR": ("стамбул", "istanbul", "анталь", "antalya", "турци", "turkey", "türkiye"),
    "AE": ("дубай", "dubai", "эмират", "emirates"),
    "CY": ("кипр", "cyprus", "лимасол", "limassol"),
    "RS": ("белград", "belgrade", "сербия", "serbia"),
    "ME": ("черногори", "montenegro", "будва", "budva"),
}
_WORD = re.compile(r"\w+")


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold().replace("ё", "е"))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


_FOREIGN = {country: tuple(_fold(stem) for stem in stems) for country, stems in _FOREIGN_PLACES.items()}


def country_of(location: str | None) -> str | None:
    """ISO-2 country of a gazetteer city (``Madrid`` -> ``ES``), else None."""
    return COUNTRY.get(location or "")


def place_countries(text: str | None) -> set[str]:
    """Countries a free-text location clearly names: gazetteer cities plus the well-known foreign places."""
    if not text:
        return set()
    found = {COUNTRY[name] for name in find_places(text) if name in COUNTRY}
    words = _WORD.findall(_fold(text))
    for country, stems in _FOREIGN.items():
        if any(word.startswith(stem) for word in words for stem in stems):
            found.add(country)
    return found


def host_tld(host: str | None) -> str | None:
    host = (host or "").strip().lower().rstrip(".")
    if not host or "." not in host:
        return None
    return host.rsplit(".", 1)[1]


def foreign_tld(host: str | None, country: str | None) -> bool:
    """True for a host under another country's code TLD than ``country`` (ISO-2); False when unsure."""
    tld = host_tld(host)
    if not country or tld is None or len(tld) != 2 or not tld.isalpha() or tld in GENERIC_CC:
        return False
    target = country.lower()
    return tld != ("uk" if target == "gb" else target)


def host_of_link(link: object) -> str | None:
    text = str(link or "").strip()
    found = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#:@]+)", text, re.IGNORECASE)
    return found.group(1).lower() if found else None


def place_names(location: str, aliases: dict[str, str] | None = None) -> tuple[str, ...]:
    """Every name of a campaign's place a search query may carry: city aliases and regions, folded."""
    names = {_fold(location), *(_fold(a) for a in (aliases or {}).values() if a), *(_fold(r) for r in REGIONS.get(location, ()))}
    for place in GAZETTEER:
        if place.canonical == location:
            names |= set(place.words) | set(place.stems) | {_fold(a) for a in place.aliases.values()}
    return tuple(sorted(n for n in names if n))


def latin_place_names(location: str, aliases: dict[str, str] | None = None) -> tuple[str, ...]:
    return tuple(n for n in place_names(location, aliases) if re.fullmatch(r"[a-z0-9 .'-]+", n))


def mentions_place(text: str, names: tuple[str, ...]) -> bool:
    """Does ``text`` name the place? Whole words, Cyrillic stems as word prefixes, hashtags as substrings."""
    folded = _fold(text)
    words = _WORD.findall(folded)
    for name in names:
        if " " in name:
            if name in folded:
                return True
        elif any(word == name or (not name.isascii() and word.startswith(name)) or
                 (len(name) >= 5 and name in word) for word in words):
            return True
    return False
