"""``TaskSpec``: what the person wants, as the interviewer fills it in (PLAN stage 2).

One structured, JSON-serialisable description of a task: the *hard* facts a
search cannot start without (place, deal, type, budget, rooms for real
estate; place, who, ticket and role for investors), the *soft* wishes, what to
exclude, the sources to use and avoid, and how to deliver the results. The
interviewer (``bot/control_plane/interviewer.py``) fills it turn by turn;
``missing_hard`` says what is still unknown; ``summary_ru`` is the card the
person confirms. A field the person said does not matter is listed in
``unspecified`` and counts as answered.

The model is tolerant on purpose: it parses what an LLM returns (strings for
numbers, stray keys, a plain string where a wish is expected) and never
raises for harmless drift; ``merged`` applies a partial update key by key and
skips a key that would make the spec invalid. Everything is bounded because
the spec travels inside a queued command and is stored as ``jsonb``.

No imports from the rest of the bot: the spec is shared by the control plane,
the architect and the Orchestra.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

Mode = Literal["real_estate", "investors"]
Deal = Literal["rent", "sale", "any"]
PropertyType = Literal["apartment", "house", "land", "room", "commercial", "other", "any"]
Level = Literal["city", "province", "region"]
UserRole = Literal["raising", "deploying"]

MODE_TITLES: dict[str, str] = {
    "real_estate": "🏡 Участки и объекты",
    "investors": "💼 Инвесторы и компании",
}
DEAL_RU = {"rent": "аренда", "sale": "покупка", "any": "не важно"}
PROPERTY_RU = {
    "apartment": "квартира",
    "house": "дом",
    "land": "участок",
    "room": "комната",
    "commercial": "коммерческая недвижимость",
    "other": "другое",
    "any": "не важно",
}
INVESTOR_WHO_RU = {
    "private": "частные инвесторы",
    "fund": "фонды",
    "family_office": "семейные офисы",
    "developer": "девелоперы",
    "agency": "агентства",
    "network": "сообщества и нетворкинг",
}
ROLE_RU = {"raising": "привлекаю деньги", "deploying": "вкладываю деньги"}
CURRENCY_SIGNS = {"EUR": "€", "USD": "$", "GBP": "£", "UAH": "₴", "RUB": "₽"}

MAX_TEXT = 200
MAX_ITEMS = 12
MAX_NOTES = 1000
MAX_QA = 20
MAX_QA_CHARS = 400
# Field paths the interviewer asks about, in asking order, per mode (see ``TaskSpec.missing_hard``).
REAL_ESTATE_ORDER = ("place", "deal", "property_type", "budget.max", "rooms.min")
INVESTORS_ORDER = ("place", "investor.who", "investor.ticket", "investor.user_role")
ROOMS_TYPES = ("apartment", "house")
# Field paths a person can waive or a question can be about: leaves only (never a group such as «investor» or
# «budget», which would waive several hard fields at once, and never «place»).
UNSPECIFIABLE_PATHS = frozenset({
    *(p for p in (*REAL_ESTATE_ORDER, *INVESTORS_ORDER) if p != "place"),
    "budget.min", "budget.max", "rooms.min", "rooms.max", "area_m2.min", "area_m2.max", "place.districts",
    "must_have", "exclude", "wishes", "sources.required", "sources.extra", "sources.blocked",
    "investor.who", "investor.ticket", "investor.ticket.min", "investor.ticket.max", "investor.user_role",
    "investor.geography", "investor.asset_class", "investor.languages", "investor.yield_min",
    # the deviation question and the task-specific round (see ``Deviations`` / ``Context``)
    "deviations", "deviations.budget_pct", "deviations.area_pct", "deviations.rooms_delta",
    "deviations.nearby_areas", "deviations.radius_km", "deviations.other", "context.answers",
})
ASKABLE_PATHS = UNSPECIFIABLE_PATHS | {"place"}


def known_paths(paths: list[str], *, allowed: frozenset[str] = UNSPECIFIABLE_PATHS) -> list[str]:
    """``paths`` without the unknown and group-level ones."""
    return [p for p in paths if p in allowed]
PLACE_NAME_KEYS = ("es", "ru", "uk", "ru_in", "uk_in")


def _clean(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None or isinstance(value, bool | dict | list):
        return None
    text = " ".join(str(value).split())
    return text[:limit] if text and text.casefold() not in {"null", "none", "n/a"} else None


def _clean_list(value: Any, limit: int = MAX_TEXT, count: int = MAX_ITEMS) -> list[str]:
    if value is None:
        return []
    raw = value if isinstance(value, list) else re.split(r"[;\n]", str(value))
    seen: set[str] = set()
    items: list[str] = []
    for item in raw:
        text = _clean(item, limit)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            items.append(text)
    return items[:count]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=False)


class Place(_Model):
    """Where to search: a city, a province or a region, anywhere in the world."""

    name: str | None = None  # in English (or as typed): 'Madrid', 'Ubud, Bali'
    country: str | None = None  # ISO 3166-1 alpha-2
    level: Level = "city"
    districts: list[str] = Field(default_factory=list)
    radius_km: float | None = Field(default=None, gt=0, le=1000)
    # The same place in other languages ('ru', 'es', 'uk', 'ru_in', 'uk_in'), when known.
    names: dict[str, str] = Field(default_factory=dict)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, value: Any) -> str | None:
        return _clean(value, 80)

    @field_validator("country", mode="before")
    @classmethod
    def _country(cls, value: Any) -> str | None:
        text = (_clean(value, 2) or "").upper()
        return text if re.fullmatch(r"[A-Z]{2}", text) else None

    @field_validator("level", mode="before")
    @classmethod
    def _level(cls, value: Any) -> str:
        text = str(value or "").strip().casefold()
        return text if text in ("city", "province", "region") else "city"

    @field_validator("districts", mode="before")
    @classmethod
    def _districts(cls, value: Any) -> list[str]:
        return _clean_list(value, 80)

    @field_validator("radius_km", mode="before")
    @classmethod
    def _radius(cls, value: Any) -> Any:
        return None if isinstance(value, bool) or value in ("", 0) else value

    @field_validator("names", mode="before")
    @classmethod
    def _names(cls, value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        names = {k: _clean(value.get(k), 80) for k in PLACE_NAME_KEYS}
        return {k: v for k, v in names.items() if v}


_MULTIPLIER = re.compile(r"\s*(млн|миллион\w*|million\w*|mm?(?![a-zа-яё])|тыс|тысяч\w*|k(?![a-zа-яё])|к(?![a-zа-яё]))\.?",
                         re.IGNORECASE)


def parse_number(text: str) -> float | None:
    """A number out of free text: «1,200» and «1.200» are 1200 (a comma or dot with exactly three digits after it is a
    thousands separator), «1,5 млн» is 1500000, «200k» and «200 тыс» are 200000, «1.5» is 1.5; ``None`` if none."""
    found = re.search(r"\d[\d\s\u00a0.,]*", text)
    if found is None:
        return None
    raw = re.sub(r"[\s\u00a0]", "", found.group()).rstrip(".,")
    suffix = _MULTIPLIER.match(text[found.end():])
    factor = 1.0
    if suffix:
        word = suffix.group(1).casefold()
        factor = 1_000_000.0 if word.startswith(("м", "m")) else 1000.0
    dots, commas = raw.count("."), raw.count(",")
    if dots and commas:  # the last separator is the decimal one
        decimal = "." if raw.rfind(".") > raw.rfind(",") else ","
        raw = raw.replace("," if decimal == "." else ".", "").replace(decimal, ".")
    elif dots + commas > 1:  # «1.200.000», «1,200,000»
        raw = re.sub(r"[.,]", "", raw)
    elif dots + commas == 1:
        head, tail = re.split(r"[.,]", raw)
        thousands = len(tail) == 3 and head not in ("", "0") and not suffix
        raw = head + tail if thousands else f"{head or '0'}.{tail}"
    try:
        return float(raw) * factor
    except ValueError:
        return None


class Range(_Model):
    min: float | None = None
    max: float | None = None

    @field_validator("min", "max", mode="before")
    @classmethod
    def _number(cls, value: Any) -> Any:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, str):
            value = parse_number(value)
        if isinstance(value, int | float) and not 0 <= value <= 1_000_000_000:
            return None
        return value

    @model_validator(mode="after")
    def _ordered(self) -> Range:
        """An inverted pair (min > max) keeps only the maximum, the person's ceiling."""
        if self.min is not None and self.max is not None and self.min > self.max:
            self.min = None
        return self

    def is_set(self) -> bool:
        return self.min is not None or self.max is not None


class Rooms(Range):
    """Rooms: an inverted pair is swapped («3-2» means 2 to 3)."""

    @model_validator(mode="after")
    def _ordered(self) -> Rooms:
        if self.min is not None and self.max is not None and self.min > self.max:
            self.min, self.max = self.max, self.min
        return self


class Money(Range):
    currency: str | None = None

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str | None:
        text = (_clean(value, 3) or "").upper()
        return text if re.fullmatch(r"[A-Z]{3}", text) else None


class Wish(_Model):
    text: str = Field(min_length=1, max_length=120)
    weight: int = Field(default=2, ge=1, le=3)

    @field_validator("text", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return _clean(value, 120) or ""

    @field_validator("weight", mode="before")
    @classmethod
    def _weight(cls, value: Any) -> int:
        try:
            return max(1, min(3, int(value)))
        except (TypeError, ValueError):
            return 2


class Investor(_Model):
    """Investors mode: who to look for and on what terms."""

    who: list[str] = Field(default_factory=list)  # private|fund|family_office|developer|agency|network
    ticket: Money = Field(default_factory=Money)
    asset_class: list[str] = Field(default_factory=list)
    yield_min: float | None = Field(default=None, ge=0, le=1000)
    geography: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    user_role: UserRole | None = None  # raising: the person seeks money; deploying: the person invests

    @field_validator("who", "asset_class", "geography", "languages", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        return _clean_list(value, 80)

    @field_validator("who", mode="after")
    @classmethod
    def _who_lower(cls, value: list[str]) -> list[str]:
        return [item.casefold() for item in value]

    @field_validator("user_role", mode="before")
    @classmethod
    def _role(cls, value: Any) -> str | None:
        text = str(value or "").strip().casefold()
        return text if text in ("raising", "deploying") else None

    @field_validator("yield_min", mode="before")
    @classmethod
    def _yield(cls, value: Any) -> Any:
        return None if isinstance(value, bool) or value == "" else value


class Deviations(_Model):
    """Compromises the person approved in advance ("если точных вариантов не будет, что допустимо?").

    A finding that differs from the request only within these is sent as a normal card marked with what differs
    (no «Одобрить?» question). ``asked``: the deviation question was put to the person (answered or waived).
    """

    budget_pct: float | None = None  # allowed % over the maximum (under the minimum); 0 = only exact
    area_pct: float | None = None  # allowed % below the minimum area (above the maximum)
    rooms_delta: int | None = None  # how many rooms fewer than the minimum are fine
    radius_km: float | None = None
    nearby_areas: list[str] = Field(default_factory=list)  # neighbouring districts or towns accepted as the place
    other: list[str] = Field(default_factory=list)  # free-text compromises: «без лифта ок до 2 этажа»
    asked: bool = False

    @field_validator("budget_pct", "area_pct", mode="before")
    @classmethod
    def _pct(cls, value: Any) -> float | None:
        number = _loose_number(value)
        return number if number is not None and 0 <= number <= 50 else None

    @field_validator("radius_km", mode="before")
    @classmethod
    def _radius(cls, value: Any) -> float | None:
        number = _loose_number(value)
        return number if number is not None and 0 < number <= 1000 else None

    @field_validator("rooms_delta", mode="before")
    @classmethod
    def _delta(cls, value: Any) -> int | None:
        number = _loose_number(value)
        return int(number) if number is not None and 0 <= number <= 5 else None

    @field_validator("nearby_areas", mode="before")
    @classmethod
    def _areas(cls, value: Any) -> list[str]:
        return _clean_list(value, 80)

    @field_validator("other", mode="before")
    @classmethod
    def _other(cls, value: Any) -> list[str]:
        return _clean_list(value, 120, 8)

    @field_validator("asked", mode="before")
    @classmethod
    def _asked(cls, value: Any) -> bool:
        return value if isinstance(value, bool) else False

    @model_validator(mode="after")
    def _implied(self) -> Deviations:
        """Any approved compromise (even «0 %», only exact) means the question was answered."""
        if self.has_values():
            self.asked = True
        return self

    def has_values(self) -> bool:
        return (any(v is not None for v in (self.budget_pct, self.area_pct, self.rooms_delta, self.radius_km))
                or bool(self.nearby_areas or self.other))

    def allows(self) -> bool:
        """Some compromise beyond the exact request is approved (not «только точные»)."""
        return any(v for v in (self.budget_pct, self.area_pct, self.rooms_delta, self.radius_km)) or bool(
            self.nearby_areas or self.other)

    def line_ru(self) -> str:
        parts: list[str] = []
        if self.budget_pct:
            parts.append(f"бюджет ±{_num(self.budget_pct)} %")
        if self.area_pct:
            parts.append(f"площадь −{_num(self.area_pct)} %")
        if self.rooms_delta:
            parts.append(f"комнат на {self.rooms_delta} меньше")
        if self.nearby_areas:
            parts.append("соседние районы: " + ", ".join(self.nearby_areas))
        if self.radius_km:
            parts.append(f"радиус {_num(self.radius_km)} км")
        parts += self.other
        return "; ".join(parts)


class QA(_Model):
    question: str = ""
    answer: str = ""

    @field_validator("question", "answer", mode="before")
    @classmethod
    def _text(cls, value: Any) -> str:
        return _clean(value, MAX_QA_CHARS) or ""


class Context(_Model):
    """Every task-specific question and the person's answer, verbatim (bounded), for the planner and the reviewer."""

    answers: list[QA] = Field(default_factory=list)

    @field_validator("answers", mode="before")
    @classmethod
    def _answers(cls, value: Any) -> list[Any]:
        items = [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []
        return [QA.model_validate(v) for v in items if _clean(v.get("question")) and _clean(v.get("answer"))][-MAX_QA:]


def _loose_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = parse_number(value)
    return float(value) if isinstance(value, int | float) and value == value else None


class Sources(_Model):
    required: list[str] = Field(default_factory=list)  # sites or groups that must be searched
    extra: list[str] = Field(default_factory=list)  # nice to search as well
    blocked: list[str] = Field(default_factory=list)  # never use

    @field_validator("required", "extra", "blocked", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        return _clean_list(value, 120)


class Delivery(_Model):
    max_results: int | None = Field(default=None, ge=1, le=500)
    show_similar: bool = False

    @field_validator("max_results", mode="before")
    @classmethod
    def _max(cls, value: Any) -> Any:
        return None if isinstance(value, bool) or value in ("", 0) else value

    @field_validator("show_similar", mode="before")
    @classmethod
    def _similar(cls, value: Any) -> bool:
        return bool(value) if isinstance(value, bool) else False


class TaskSpec(_Model):
    mode: Mode | None = None
    # hard
    place: Place = Field(default_factory=Place)
    deal: Deal | None = None
    property_type: PropertyType | None = None
    budget: Money = Field(default_factory=Money)
    rooms: Rooms = Field(default_factory=Rooms)
    area_m2: Range = Field(default_factory=Range)
    must_have: list[str] = Field(default_factory=list)
    # soft
    wishes: list[Wish] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    investor: Investor = Field(default_factory=Investor)
    sources: Sources = Field(default_factory=Sources)
    delivery: Delivery = Field(default_factory=Delivery)
    deviations: Deviations = Field(default_factory=Deviations)
    context: Context = Field(default_factory=Context)
    notes: str = ""
    # field paths the person said do not matter ('budget.max', 'rooms.min', 'investor.ticket' ...)
    unspecified: list[str] = Field(default_factory=list)

    @field_validator("deal", mode="before")
    @classmethod
    def _deal(cls, value: Any) -> str | None:
        text = str(value or "").strip().casefold()
        return text if text in ("rent", "sale", "any") else None

    @field_validator("property_type", mode="before")
    @classmethod
    def _type(cls, value: Any) -> str | None:
        text = str(value or "").strip().casefold()
        return text if text in PROPERTY_RU else None

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: Any) -> str | None:
        text = str(value or "").strip().casefold()
        return text if text in MODE_TITLES else None

    @field_validator("must_have", "exclude", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        return _clean_list(value)

    @field_validator("wishes", mode="before")
    @classmethod
    def _wishes(cls, value: Any) -> list[Any]:
        if value is None:
            return []
        raw = value if isinstance(value, list) else re.split(r"[;\n]", value) if isinstance(value, str) else [value]
        items: list[Any] = []
        seen: set[str] = set()
        for item in raw:
            wish = {"text": item} if isinstance(item, str) else item
            text = _clean(wish.get("text"), 120) if isinstance(wish, dict) else None
            if text and text.casefold() not in seen:
                seen.add(text.casefold())
                items.append({**wish, "text": text})
        return items[:MAX_ITEMS]

    @field_validator("notes", mode="before")
    @classmethod
    def _notes(cls, value: Any) -> str:
        return _clean(value, MAX_NOTES) or ""

    @field_validator("unspecified", mode="before")
    @classmethod
    def _unspecified(cls, value: Any) -> list[str]:
        return known_paths(_clean_list(value, 40, 30))

    # --- what is known -------------------------------------------------------------------

    def place_name(self) -> str | None:
        """The place to search: the stated one; for investors the first geography entry stands in."""
        if self.place.name:
            return self.place.name
        return self.investor.geography[0] if self.mode == "investors" and self.investor.geography else None

    def is_unspecified(self, path: str) -> bool:
        parts = path.split(".")
        return any(".".join(parts[:i]) in self.unspecified for i in range(1, len(parts) + 1))

    def filled(self, path: str) -> bool:
        """The field has a value (an explicit 'any' counts)."""
        match path:
            case "place":
                return self.place_name() is not None
            case "deal":
                return self.deal is not None
            case "property_type":
                return self.property_type is not None
            case "budget.max":
                return self.budget.max is not None
            case "budget.min":
                return self.budget.min is not None
            case "rooms.min" | "rooms":
                return self.rooms.is_set()
            case "area_m2.min" | "area_m2":
                return self.area_m2.is_set()
            case "investor.who":
                return bool(self.investor.who)
            case "investor.ticket":
                return self.investor.ticket.is_set()
            case "investor.user_role":
                return self.investor.user_role is not None
            case "investor.geography":
                return bool(self.investor.geography)
            case "place.districts":
                return bool(self.place.districts)
            case "must_have":
                return bool(self.must_have)
            case "wishes":
                return bool(self.wishes)
            case "sources.required" | "sources":
                return bool(self.sources.required or self.sources.extra)
            case "deviations":
                return self.deviations.asked
            case "context.answers":
                return bool(self.context.answers)
        return False

    def answered(self, path: str) -> bool:
        return self.filled(path) or self.is_unspecified(path)

    def missing_hard(self, mode: str | None = None) -> list[str]:
        """Hard fields neither filled nor marked unspecified, in the order they are asked.

        real_estate: place, deal, property_type, budget.max, rooms.min (only for an apartment or a house).
        investors: place (or geography), investor.who, investor.ticket, investor.user_role.
        """
        mode = mode or self.mode or "real_estate"
        if mode == "investors":
            order: tuple[str, ...] = INVESTORS_ORDER
        else:
            order = tuple(p for p in REAL_ESTATE_ORDER if p != "rooms.min" or self.property_type in ROOMS_TYPES)
        # The place can never be waved away: a search needs somewhere to look.
        return [p for p in order if not (self.filled(p) or (p != "place" and self.is_unspecified(p)))]

    # --- updates -------------------------------------------------------------------------

    def merged(self, partial: Any) -> TaskSpec:
        """A new spec with ``partial`` (a dict, as an LLM returns it) applied over this one.

        Scalars override, nested objects merge, a non-empty list replaces the list, ``null`` and empty values
        change nothing, ``unspecified`` accumulates; the mode is never changed. A key that would make the spec
        invalid is skipped. A field that now has a value leaves ``unspecified``.
        """
        base = self.model_dump()
        if isinstance(partial, dict):
            for key, value in partial.items():
                if key in ("mode", "unspecified") or key not in base:
                    continue
                if key == "context":
                    value = _append_answers(base[key], value)
                trial = {**base, key: _overlay(base[key], value)}
                try:
                    checked = TaskSpec.model_validate(trial).model_dump()[key]
                except ValidationError:
                    continue
                # A value that validates to nothing (junk, a negative number) never wipes a field that is set.
                kept = _keep_set(base[key], checked)
                if key == "deviations" and base[key].get("asked"):
                    kept["asked"] = True  # a question once asked stays asked
                base = {**base, key: kept}
        spec = TaskSpec.model_validate(base)
        extra = partial.get("unspecified") if isinstance(partial, dict) else None
        unspecified = known_paths([*self.unspecified, *_clean_list(extra, 40, 30)])
        spec.unspecified = [p for i, p in enumerate(unspecified)
                            if p not in unspecified[:i] and not spec.filled(p)]
        return spec

    def mark_unspecified(self, path: str) -> TaskSpec:
        spec = self.model_copy(deep=True)
        if path and path not in spec.unspecified:
            spec.unspecified = [*spec.unspecified, path][:30]
        return spec

    # --- the card ------------------------------------------------------------------------

    def summary_ru(self) -> str:
        """The structured task card in Russian, one fact per line."""
        lines = [f"Режим: {MODE_TITLES.get(self.mode or '', self.mode or 'не выбран')}"]
        name = self.place.names.get("ru") or self.place_name()
        place = name or "не указан"
        if name and self.place.level != "city":
            place += f" ({'область' if self.place.level == 'province' else 'регион'})"
        lines.append(f"Город: {place}")
        if self.place.districts:
            lines.append(f"Районы: {', '.join(self.place.districts)}")
        if self.place.radius_km:
            lines.append(f"Радиус: {_num(self.place.radius_km)} км")
        if self.mode == "investors":
            self._investor_lines(lines)
        else:
            self._real_estate_lines(lines)
        if self.must_have:
            lines.append(f"Обязательно: {', '.join(self.must_have)}")
        if self.wishes:
            lines.append("Пожелания: " + ", ".join(w.text + (" (очень важно)" if w.weight == 3 else "") for w in self.wishes))
        if self.exclude:
            lines.append(f"Исключить: {', '.join(self.exclude)}")
        sources = []
        if self.sources.required:
            sources.append("обязательно " + ", ".join(self.sources.required))
        if self.sources.extra:
            sources.append("ещё " + ", ".join(self.sources.extra))
        if self.sources.blocked:
            sources.append("не использовать " + ", ".join(self.sources.blocked))
        if sources:
            lines.append("Источники: " + "; ".join(sources))
        if self.deviations.asked:
            lines.append("Допустимые отступления: " + (
                self.deviations.line_ru() or ("нет, только точные" if self.deviations.has_values() else "не заданы")))
        if self.context.answers:
            lines.append("Уточнения:")
            lines += [f"• {_short(qa.question)} — {qa.answer}" for qa in self.context.answers[:6]]
        if self.delivery.max_results or self.delivery.show_similar:
            parts = [f"до {self.delivery.max_results} вариантов"] if self.delivery.max_results else []
            if self.delivery.show_similar:
                parts.append("показывать похожие")
            lines.append("Выдача: " + ", ".join(parts))
        if self.notes:
            lines.append(f"Заметки: {self.notes}")
        return "\n".join(lines)

    def _real_estate_lines(self, lines: list[str]) -> None:
        lines.append(f"Сделка: {DEAL_RU[self.deal] if self.deal else self._gap('deal')}")
        lines.append(f"Тип: {PROPERTY_RU[self.property_type] if self.property_type else self._gap('property_type')}")
        lines.append(f"Бюджет: {_money(self.budget) or self._gap('budget.max')}")
        if self.rooms.is_set() or self.property_type in ROOMS_TYPES:
            lines.append(f"Комнаты: {_range(self.rooms, '') or self._gap('rooms.min').replace('указан', 'указаны')}")
        if self.area_m2.is_set():
            lines.append(f"Площадь: {_range(self.area_m2, ' м²')}")

    def _investor_lines(self, lines: list[str]) -> None:
        who = ", ".join(INVESTOR_WHO_RU.get(w, w) for w in self.investor.who)
        lines.append(f"Кого ищем: {who or self._gap('investor.who')}")
        lines.append(f"Тикет: {_money(self.investor.ticket) or self._gap('investor.ticket')}")
        role = ROLE_RU.get(self.investor.user_role or "")
        lines.append(f"Ваша роль: {role or self._gap('investor.user_role')}")
        if self.investor.asset_class:
            lines.append(f"Активы: {', '.join(self.investor.asset_class)}")
        if self.investor.yield_min:
            lines.append(f"Доходность: от {_num(self.investor.yield_min)}%")
        if self.investor.geography:
            lines.append(f"География: {', '.join(self.investor.geography)}")
        if self.investor.languages:
            lines.append(f"Языки: {', '.join(self.investor.languages)}")

    def _gap(self, path: str) -> str:
        return "не важно" if self.is_unspecified(path) else "не указан"


def _short(question: str) -> str:
    """The question without its examples, for a card line: «Этаж и лифт важны?» -> «Этаж и лифт важны?»."""
    head = question.split("Например")[0].strip()
    return head if len(head) <= 80 else head[:79].rstrip() + "…"


def _append_answers(current: Any, new: Any) -> Any:
    """The model returns only the NEW question/answer pairs; they are appended (a repeated question is replaced)."""
    if not isinstance(new, dict) or not isinstance(new.get("answers"), list):
        return {}
    have = [a for a in (current or {}).get("answers", []) if isinstance(a, dict)]
    added = [a for a in new["answers"] if isinstance(a, dict) and _clean(a.get("question")) and _clean(a.get("answer"))]
    asked = {(_clean(a["question"]) or "").casefold() for a in added}
    kept = [a for a in have if (a.get("question") or "").casefold() not in asked]
    return {"answers": [*kept, *added][-MAX_QA:]}


def _overlay(current: Any, new: Any) -> Any:
    """``new`` laid over ``current``: nested dicts merge, empty values keep the current one."""
    if new is None or new == "" or new == [] or new == {}:
        return current
    if isinstance(current, dict) and isinstance(new, dict):
        return {**current, **{k: _overlay(current.get(k), v) for k, v in new.items()}}
    return new


def _keep_set(old: Any, new: Any) -> Any:
    """``new`` (validated), except that an empty leaf never replaces a leaf that is set in ``old``."""
    if isinstance(old, dict) and isinstance(new, dict):
        return {k: _keep_set(old.get(k), v) for k, v in new.items()}
    if (new is None or new == "" or new == [] or new == {}) and not (old is None or old == "" or old == [] or old == {}):
        return old
    return new


def _num(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _money(money: Money) -> str:
    sign = CURRENCY_SIGNS.get(money.currency or "EUR", money.currency or "€")
    if money.min is not None and money.max is not None:
        return f"от {_num(money.min)} до {_num(money.max)} {sign}"
    if money.max is not None:
        return f"до {_num(money.max)} {sign}"
    if money.min is not None:
        return f"от {_num(money.min)} {sign}"
    return ""


def _range(span: Range, unit: str) -> str:
    if span.min is not None and span.max is not None:
        return f"{_num(span.min)}–{_num(span.max)}{unit}" if span.min != span.max else f"{_num(span.min)}{unit}"
    if span.min is not None:
        return f"от {_num(span.min)}{unit}"
    if span.max is not None:
        return f"до {_num(span.max)}{unit}"
    return ""
