"""Deterministic campaign planning from a free-text (or transcribed voice) goal.

No network, no LLM, no browsing: keyword lists and fixed templates. The place
can be anywhere in the world: the task intake passes it with its names in
es/en/ru/uk and its country (``place=``), or any name is taken as it is
(``location=``). The small built-in list of well-known cities is only a
dictionary of spellings and case endings, so a typed goal («… в Мадриде»)
is still understood without the model; it never limits where to search.
Nothing here starts a collector.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .models import (
    LANGUAGES,
    MAX_GROUPS,
    MAX_SEEDS_PER_LANGUAGE,
    CampaignLimits,
    CampaignPlan,
    Language,
    Vertical,
)
from .spec import TaskSpec

MAX_TEXT_CHARS = 2000


class InvalidGoal(ValueError):
    """The goal cannot be planned; ``str(exc)`` is safe to show the user."""


def _norm(text: str) -> str:
    """Casefold and drop diacritics (Málaga -> malaga, Київ -> киів) consistently."""
    decomposed = unicodedata.normalize("NFKD", text.casefold().replace("ё", "е"))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


@dataclass(frozen=True, slots=True)
class _Place:
    canonical: str
    aliases: dict[Language, str]
    locative: dict[Language, str]  # "в Мадриде" / "в Мадриді"
    words: frozenset[str]  # exact Latin tokens
    stems: tuple[str, ...]  # Cyrillic prefixes that cover case endings
    country: str | None = None  # ISO-2, when known


Place = _Place


def _place(canonical: str, es: str, en: str, ru: str, uk: str, ru_loc: str, uk_loc: str,
           words: tuple[str, ...], stems: tuple[str, ...], country: str = "ES") -> _Place:
    return _Place(
        canonical,
        {"es": es, "en": en, "ru": ru, "uk": uk},
        {"es": es, "en": en, "ru": ru_loc, "uk": uk_loc},
        frozenset(_norm(w) for w in words),
        tuple(_norm(s) for s in stems),
        country,
    )


MAX_PLACE_CHARS = 80
_COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")
_PREPOSITION = re.compile(r"^(?:в|во|у|на|in|en)\s+", re.IGNORECASE)


def world_place(names: Mapping[str, Any], *, locative: Mapping[str, Any] | None = None,
                country: object = None) -> _Place:
    """Any place in the world from its names (``{"en": "Ubud, Bali", "ru": "Убуд, Бали", ...}``).

    A name the known list has (``Madrid``) keeps the list's case endings and
    country. Missing languages fall back to the English (or any given) name.
    """
    given = {lang: " ".join(str(names.get(lang) or "").split())[:MAX_PLACE_CHARS] for lang in LANGUAGES}
    canonical = given.get("en") or next((n for n in given.values() if n), "")
    if not canonical:
        raise InvalidGoal("Не понял, где искать. Напишите город, район или регион.")
    for place in GAZETTEER:
        if _norm(canonical) in {_norm(place.canonical), *(_norm(a) for a in place.aliases.values())}:
            return place
    aliases = {lang: given.get(lang) or canonical for lang in LANGUAGES}
    # The locative goes after «в» in the seeds («бизнес в {in}»): «в Убуде» -> «Убуде».
    loc = {lang: _PREPOSITION.sub("", " ".join(str((locative or {}).get(lang) or "").split()))[:MAX_PLACE_CHARS]
           or aliases[lang] for lang in LANGUAGES}
    tokens = {w for name in aliases.values() for w in _WORD.findall(_norm(name)) if len(w) >= 3}
    words = frozenset(w for w in tokens if w.isascii())
    # Cyrillic names change their endings (Убуд -> Убуде): match their stems.
    stems = tuple(sorted({w[:max(3, len(w) - 2)] for w in tokens if not w.isascii()}))
    code = str(country or "").strip().upper()
    return _Place(canonical, aliases, loc, words, stems, code if _COUNTRY_CODE.match(code) else None)


GAZETTEER: tuple[_Place, ...] = (
    _place("Madrid", "Madrid", "Madrid", "Мадрид", "Мадрид", "Мадриде", "Мадриді",
           ("madrid", "madryd"), ("мадрид", "мадрід")),
    _place("Barcelona", "Barcelona", "Barcelona", "Барселона", "Барселона", "Барселоне", "Барселоні",
           ("barcelona", "bcn"), ("барселон",)),
    _place("Valencia", "Valencia", "Valencia", "Валенсия", "Валенсія", "Валенсии", "Валенсії",
           ("valencia", "valència"), ("валенси", "валенсі")),
    _place("Málaga", "Málaga", "Malaga", "Малага", "Малага", "Малаге", "Малазі",
           ("malaga",), ("малаг", "малаз")),
    _place("Alicante", "Alicante", "Alicante", "Аликанте", "Аліканте", "Аликанте", "Аліканте",
           ("alicante", "alacant"), ("аликант", "алікант")),
    _place("Sevilla", "Sevilla", "Seville", "Севилья", "Севілья", "Севилье", "Севільї",
           ("sevilla", "seville"), ("севиль", "севил", "севіль", "севіл")),
    _place("Marbella", "Marbella", "Marbella", "Марбелья", "Марбелья", "Марбелье", "Марбельї",
           ("marbella",), ("марбель", "марбел")),
    _place("Kyiv", "Kiev", "Kyiv", "Киев", "Київ", "Киеве", "Києві",
           ("kyiv", "kiev", "kiew", "kyjiw"), ("киев", "київ", "києв"), country="UA"),
)

# Latin words match exactly; Cyrillic stems match as word prefixes.
_RE_WORDS = frozenset({
    "alquiler", "alquilar", "alquilo", "piso", "pisos", "apartamento", "apartamentos",
    "habitacion", "habitaciones", "vivienda", "viviendas", "inmobiliaria", "inmobiliarias",
    "casa", "casas", "venta", "vendo", "estudio", "atico", "chalet", "inmueble", "inmuebles",
    "rent", "rental", "rentals", "renting", "flat", "flats", "apartment", "apartments", "room",
    "rooms", "house", "houses", "housing", "property", "properties", "realestate", "bedroom",
    "bedrooms", "studio", "lease", "sale", "home", "homes", "accommodation", "landlord",
    # land, buildings and commercial property
    "terreno", "terrenos", "parcela", "parcelas", "solar", "solares", "finca", "fincas", "local", "locales",
    "nave", "naves", "oficina", "oficinas", "edificio", "edificios", "adosado", "duplex", "villa", "villas",
    "land", "plot", "plots", "lot", "lots", "acre", "acres", "commercial", "office", "offices", "warehouse",
    "building", "buildings", "townhouse", "cottage",
    # «дом» only as a whole word: as a prefix it would match «домашний», «домен»
    "дом", "дома", "домов", "домик", "домом", "доме", "будинок", "хата",
})
_RE_STEMS = ("квартир", "аренд", "снять", "сниму", "съем", "комнат", "жиль", "жилье", "недвижим",
             "продаж", "студи", "оренд", "житл", "кімнат", "нерухом", "будин", "винайм", "зняти",
             # land, houses and commercial property (RU / UK)
             "участ", "земл", "сотк", "коттедж", "дач", "таунхаус", "особняк", "вилл", "апартамент",
             "помещен", "офис", "склад", "здани", "ділянк", "котедж", "приміщен", "офіс")
_INV_WORDS = frozenset({
    "inversor", "inversores", "inversion", "inversiones", "invertir", "inversionista",
    "inversionistas", "financiacion", "startup", "startups", "emprendedor", "emprendedores",
    "invest", "investor", "investors", "investment", "investments", "investing", "funding",
    "fundraising", "angel", "angels", "venture", "vc",
})
_INV_STEMS = ("инвест", "стартап", "финансирован", "венчур", "інвест", "фінансуван")

_RENT_WORDS = frozenset({"alquiler", "alquilar", "alquilo", "rent", "rental", "rentals", "renting", "lease"})
_RENT_STEMS = ("аренд", "снять", "сниму", "съем", "оренд", "винайм", "зняти")
_SALE_WORDS = frozenset({"venta", "vendo", "comprar", "compra", "sale", "buy", "buying", "purchase"})
_SALE_STEMS = ("продаж", "продам", "купить", "купл", "покупк", "купити", "придба")

_WORD = re.compile(r"\w+")
_NUM = r"(\d{1,3}(?:[ .,]\d{3})+|\d+(?:[.,]\d+)?)"
_CUR = r"(?:€|eur\b|euros?\b|евро\b|євро\b)"
_GROUPS = re.compile(r"(\d{1,4})\s*(?:групп\w*|груп\w*|groups?\b|grupos?\b)")
_ROOMS = re.compile(
    r"(\d{1,2})\s*-?\s*(?:х\s*)?(?:комнат\w*|комн\b|кімнат\w*|спал\w*|habitacion\w*|hab\b|"
    r"dormitorios?\b|bedrooms?\b|rooms?\b|br\b|beds?\b)"
)
_PRICE_KEYWORD = re.compile(
    r"(?<!\w)(?:до|не дороже|не дорожче|максимум|under|below|up to|max(?:imum)?|less than|hasta|"
    r"maximo|menos de|no mas de|<=|≤|<)\s*€?\s*" + _NUM + r"\s*(k|к|тыс\w*|тис\w*)?\s*(" + _CUR + r")?"
)
_PRICE_BARE = re.compile(r"(?:€\s*" + _NUM + r"|" + _NUM + r"\s*(k|к|тыс\w*|тис\w*)?\s*" + _CUR + r")")

_TEMPLATES: dict[str, dict[Language, tuple[tuple[str, str], ...]]] = {
    "real_estate": {
        "es": (("rent", "alquiler pisos {loc}"), ("rent", "habitaciones {loc}"), ("any", "inmobiliaria {loc}"),
               ("sale", "pisos en venta {loc}"), ("rent", "compartir piso {loc}"), ("any", "vivienda {loc}")),
        "en": (("rent", "{loc} apartments for rent"), ("any", "{loc} expats housing"), ("rent", "{loc} rooms for rent"),
               ("any", "{loc} real estate"), ("sale", "{loc} property for sale"), ("rent", "{loc} flat share")),
        "ru": (("rent", "аренда квартир {loc}"), ("any", "русские в {in} жильё"), ("rent", "снять комнату {loc}"),
               ("any", "недвижимость {loc}"), ("sale", "продажа квартир {loc}"), ("any", "жильё в {in}")),
        "uk": (("rent", "оренда житла {loc}"), ("any", "українці в {in}"), ("rent", "оренда квартир {loc}"),
               ("rent", "кімнати {loc}"), ("any", "нерухомість {loc}"), ("sale", "продаж квартир {loc}")),
    },
    "investors": {
        "es": (("any", "inversores {loc}"), ("any", "startups {loc}"), ("any", "emprendedores {loc}"),
               ("any", "business angels {loc}"), ("any", "inversión inmobiliaria {loc}"), ("any", "networking empresarial {loc}")),
        "en": (("any", "{loc} investors"), ("any", "{loc} startups"), ("any", "{loc} entrepreneurs"),
               ("any", "{loc} angel investors"), ("any", "{loc} business networking"), ("any", "{loc} expat entrepreneurs")),
        "ru": (("any", "инвесторы {loc}"), ("any", "стартапы {loc}"), ("any", "предприниматели в {in}"),
               ("any", "бизнес в {in}"), ("any", "русский бизнес {loc}"), ("any", "инвестиции {loc}")),
        "uk": (("any", "інвестори {loc}"), ("any", "стартапи {loc}"), ("any", "підприємці в {in}"),
               ("any", "український бізнес {loc}"), ("any", "бізнес в {in}"), ("any", "інвестиції {loc}")),
    },
}


EXPLICIT_VERTICALS = ("real_estate", "investors")


def plan_campaign(text: str, *, vertical: Vertical | None = None, location: str | None = None,
                  place: Mapping[str, Any] | None = None, spec: TaskSpec | None = None) -> CampaignPlan:
    """Turn a user goal in ES/EN/RU/UK into a bounded plan, or raise ``InvalidGoal``.

    ``vertical`` (real_estate|investors), ``place`` (names per language,
    ``ru_in``/``uk_in`` locatives and ``country``, see :func:`world_place`)
    and ``location`` (any place name) are choices a person already made;
    they override what the text says, so "no vertical" and "several cities"
    cannot fire.

    ``spec`` (the interviewer's ``TaskSpec``) is what the person confirmed: when given, the constraints (deal,
    price range, rooms, area, property type, districts) are taken from it instead of being read from the text,
    and a place not passed otherwise comes from it. The text still feeds the query seeds' vertical detection.
    """
    if not isinstance(text, str) or not text.strip():
        raise InvalidGoal("Пустая задача. Напишите, что искать и где, например: «квартиры в аренду в Мадриде».")
    if len(text) > MAX_TEXT_CHARS:
        raise InvalidGoal(f"Слишком длинная задача (больше {MAX_TEXT_CHARS} символов). Сократите её.")
    normalized = _norm(text)
    words = _WORD.findall(normalized)
    if spec is not None and place is None and location is None and spec.place_name():
        place = _spec_place(spec)
    if place is not None:
        where = world_place(place, locative={"ru": place.get("ru_in"), "uk": place.get("uk_in")},
                            country=place.get("country"))
    elif location is not None:
        where = world_place({"en": location})
    else:
        where = _detect_place(words)
    if vertical is None:
        vertical = _detect_vertical(words)
    elif vertical not in EXPLICIT_VERTICALS:
        raise InvalidGoal(f"Неизвестный режим: {vertical}.")

    rest = normalized
    max_groups, rest = _extract_groups(rest)
    rooms, rest = _extract_rooms(rest)
    max_price = _extract_price(rest)
    deal = _detect_deal(words) if vertical != "investors" else None
    constraints: dict[str, str | int | None] = {"deal": deal, "max_price": max_price, "rooms": rooms}
    if spec is not None:
        constraints = _spec_constraints(spec, vertical)
        deal, max_price, rooms = constraints.get("deal"), constraints.get("max_price"), constraints.get("rooms")  # type: ignore[assignment]

    limits = CampaignLimits(max_groups=max_groups) if max_groups is not None else CampaignLimits()
    return CampaignPlan(
        goal=_summary(vertical, where.canonical, deal, max_price, rooms),
        location=where.canonical,
        location_aliases=dict(where.aliases),
        country=where.country,
        vertical=vertical,
        languages=list(LANGUAGES),
        query_seeds={lang: _seeds(where, vertical, deal, lang) for lang in LANGUAGES},
        constraints=constraints,
        limits=limits,
    )


def _spec_place(spec: TaskSpec) -> dict[str, str]:
    """The spec's place as the ``place=`` mapping of ``world_place`` (names per language, locatives, country)."""
    names = {k: v for k, v in spec.place.names.items() if v}
    place: dict[str, str] = {"en": spec.place_name() or "", **names}
    if spec.place.country:
        place["country"] = spec.place.country
    return place


def _whole(value: float | None) -> int | None:
    return round(value) if value is not None and value >= 1 else None


def _spec_constraints(spec: TaskSpec, vertical: str) -> dict[str, str | int | None]:
    """The plan's constraints from what the person confirmed (rent/sale only for real estate)."""
    constraints: dict[str, str | int | None] = {"deal": None, "max_price": None, "rooms": None}
    if vertical == "investors":
        return constraints
    constraints["deal"] = spec.deal if spec.deal in ("rent", "sale") else None
    constraints["max_price"] = _whole(spec.budget.max)
    constraints["rooms"] = _whole(spec.rooms.min if spec.rooms.min is not None else spec.rooms.max)
    if (low := _whole(spec.budget.min)) is not None:
        constraints["min_price"] = low
    if (low := _whole(spec.area_m2.min)) is not None:
        constraints["min_area"] = low
    if (high := _whole(spec.area_m2.max)) is not None:
        constraints["max_area"] = high
    if spec.property_type not in (None, "any"):
        constraints["property_type"] = spec.property_type
    if spec.place.districts:
        constraints["districts"] = ", ".join(spec.place.districts)[:200]
    return constraints


def _has(words: list[str], exact: frozenset[str], stems: tuple[str, ...]) -> bool:
    return any(w in exact or w.startswith(stems) for w in words)


def _matches(place: _Place, word: str) -> bool:
    return word in place.words or word.startswith(place.stems)


def find_places(text: str) -> list[str]:
    """Canonical names of the gazetteer cities a text mentions, in gazetteer order."""
    words = _WORD.findall(_norm(text))
    return [p.canonical for p in GAZETTEER if any(_matches(p, w) for w in words)]


def _detect_place(words: list[str]) -> _Place:
    found = [p for p in GAZETTEER if any(_matches(p, w) for w in words)]
    if not found:
        raise InvalidGoal(
            "Не понял, где искать. Напишите место в начале задачи, например: city=Ubud,_Bali "
            "(любой город, район или регион мира; пробелы — через _)."
        )
    if len(found) > 1:
        names = ", ".join(p.canonical for p in found)
        raise InvalidGoal(f"Указано несколько городов ({names}). Одна кампания — один город.")
    return found[0]


def _detect_vertical(words: list[str]) -> Vertical:
    joined = " ".join(words)
    real_estate = _has(words, _RE_WORDS, _RE_STEMS) or "real estate" in joined or "bienes raices" in joined
    investors = _has(words, _INV_WORDS, _INV_STEMS) or "business angel" in joined
    if real_estate and investors:
        return "both"
    if real_estate:
        return "real_estate"
    if investors:
        return "investors"
    if _detect_deal(words) is not None:
        return "real_estate"  # «купить в Мадриде до 60 000 €»: buying or renting here is always property
    raise InvalidGoal("Не понял, что искать: жильё (аренда/продажа) или инвесторов. Уточните задачу.")


def _detect_deal(words: list[str]) -> str | None:
    rent = _has(words, _RENT_WORDS, _RENT_STEMS)
    sale = _has(words, _SALE_WORDS, _SALE_STEMS)
    return "rent" if rent and not sale else "sale" if sale and not rent else None


def _extract_groups(text: str) -> tuple[int | None, str]:
    match = _GROUPS.search(text)
    if not match:
        return None, text
    value = max(1, min(MAX_GROUPS, int(match.group(1))))
    return value, text[: match.start()] + " " + text[match.end():]


def _extract_rooms(text: str) -> tuple[int | None, str]:
    match = _ROOMS.search(text)
    if not match:
        return None, text
    value = int(match.group(1))
    rest = text[: match.start()] + " " + text[match.end():]
    return (value if 1 <= value <= 20 else None), rest


def _to_number(raw: str, thousands: str | None) -> int | None:
    raw = raw.replace(" ", "")
    if thousands:
        value = float(raw.replace(",", "."))
        number = round(value * 1000)
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        number = int(re.sub(r"[.,]", "", raw))
    else:
        number = round(float(raw.replace(",", ".")))
    return number if 0 < number <= 100_000_000 else None


def _extract_price(text: str) -> int | None:
    for match in _PRICE_KEYWORD.finditer(text):
        number = _to_number(match.group(1), match.group(2))
        # "до 3" without a currency is not a price; real budgets are >= 100.
        if number is not None and (match.group(3) or number >= 100):
            return number
    match = _PRICE_BARE.search(text)
    if match:
        raw = match.group(1) or match.group(2)
        return _to_number(raw, match.group(3))
    return None


def _seeds(place: _Place, vertical: Vertical, deal: str | None, lang: Language) -> list[str]:
    def ordered(kind: str) -> list[str]:
        templates = _TEMPLATES[kind][lang]
        if deal is not None:
            rank = {deal: 0, "any": 1}
            templates = tuple(sorted(templates, key=lambda t: rank.get(t[0], 2)))
        return [t.format(loc=place.aliases[lang], **{"in": place.locative[lang]}) for _, t in templates]

    if vertical == "both":
        candidates = [s for pair in zip(ordered("real_estate"), ordered("investors"), strict=True) for s in pair]
    else:
        candidates = ordered(vertical)
    seeds: list[str] = []
    for seed in candidates:
        if seed.casefold() not in {s.casefold() for s in seeds}:
            seeds.append(seed)
    return seeds[:MAX_SEEDS_PER_LANGUAGE]


def _summary(vertical: str, location: str, deal: str | None, max_price: int | None, rooms: int | None) -> str:
    parts = [vertical, location]
    terms = " ".join(x for x in (deal, f"≤ {max_price} EUR" if max_price else None) if x)
    if terms:
        parts.append(terms)
    if rooms:
        parts.append(f"{rooms} rooms")
    return " · ".join(parts)
