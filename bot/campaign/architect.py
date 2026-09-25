"""Deterministic campaign planning from a free-text (or transcribed voice) goal.

No network, no LLM, no browsing: the text is matched against a small built-in
gazetteer and multilingual keyword lists, and seeds come from fixed templates.
Nothing here starts a collector.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .models import (
    LANGUAGES,
    MAX_GROUPS,
    MAX_SEEDS_PER_LANGUAGE,
    CampaignLimits,
    CampaignPlan,
    Language,
    Vertical,
)

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


def _place(canonical: str, es: str, en: str, ru: str, uk: str, ru_loc: str, uk_loc: str,
           words: tuple[str, ...], stems: tuple[str, ...]) -> _Place:
    return _Place(
        canonical,
        {"es": es, "en": en, "ru": ru, "uk": uk},
        {"es": es, "en": en, "ru": ru_loc, "uk": uk_loc},
        frozenset(_norm(w) for w in words),
        tuple(_norm(s) for s in stems),
    )


GAZETTEER: tuple[_Place, ...] = (
    _place("Madrid", "Madrid", "Madrid", "Мадрид", "Мадрид", "Мадриде", "Мадриді",
           ("madrid",), ("мадрид",)),
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
           ("kyiv", "kiev", "kiew", "kyjiw"), ("киев", "київ", "києв")),
)

# Latin words match exactly; Cyrillic stems match as word prefixes.
_RE_WORDS = frozenset({
    "alquiler", "alquilar", "alquilo", "piso", "pisos", "apartamento", "apartamentos",
    "habitacion", "habitaciones", "vivienda", "viviendas", "inmobiliaria", "inmobiliarias",
    "casa", "casas", "venta", "vendo", "estudio", "atico", "chalet", "inmueble", "inmuebles",
    "rent", "rental", "rentals", "renting", "flat", "flats", "apartment", "apartments", "room",
    "rooms", "house", "houses", "housing", "property", "properties", "realestate", "bedroom",
    "bedrooms", "studio", "lease", "sale", "home", "homes", "accommodation", "landlord",
})
_RE_STEMS = ("квартир", "аренд", "снять", "сниму", "съем", "комнат", "жиль", "жилье", "недвижим",
             "продаж", "студи", "оренд", "житл", "кімнат", "нерухом", "будин", "винайм", "зняти")
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


def plan_campaign(text: str) -> CampaignPlan:
    """Turn a user goal in ES/EN/RU/UK into a bounded plan, or raise ``InvalidGoal``."""
    if not isinstance(text, str) or not text.strip():
        raise InvalidGoal("Пустая задача. Напишите, что искать и где, например: «квартиры в аренду в Мадриде».")
    if len(text) > MAX_TEXT_CHARS:
        raise InvalidGoal(f"Слишком длинная задача (больше {MAX_TEXT_CHARS} символов). Сократите её.")
    normalized = _norm(text)
    words = _WORD.findall(normalized)
    place = _detect_place(words)
    vertical = _detect_vertical(words)

    rest = normalized
    max_groups, rest = _extract_groups(rest)
    rooms, rest = _extract_rooms(rest)
    max_price = _extract_price(rest)
    deal = _detect_deal(words) if vertical != "investors" else None
    constraints: dict[str, str | int | None] = {"deal": deal, "max_price": max_price, "rooms": rooms}

    limits = CampaignLimits(max_groups=max_groups) if max_groups is not None else CampaignLimits()
    return CampaignPlan(
        goal=_summary(vertical, place.canonical, deal, max_price, rooms),
        location=place.canonical,
        location_aliases=dict(place.aliases),
        vertical=vertical,
        languages=list(LANGUAGES),
        query_seeds={lang: _seeds(place, vertical, deal, lang) for lang in LANGUAGES},
        constraints=constraints,
        limits=limits,
    )


def _has(words: list[str], exact: frozenset[str], stems: tuple[str, ...]) -> bool:
    return any(w in exact or w.startswith(stems) for w in words)


def _detect_place(words: list[str]) -> _Place:
    found = [p for p in GAZETTEER if any(w in p.words or w.startswith(p.stems) for w in words)]
    if not found:
        raise InvalidGoal(
            "Не понял город. Укажите его явно: Мадрид, Барселона, Валенсия, Малага, Аликанте, "
            "Севилья, Марбелья или Киев."
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
