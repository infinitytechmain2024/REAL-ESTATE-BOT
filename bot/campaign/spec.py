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

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

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
# Field paths the interviewer asks about, in asking order, per mode (see ``TaskSpec.missing_hard``).
REAL_ESTATE_ORDER = ("place", "deal", "property_type", "budget.max", "rooms.min")
INVESTORS_ORDER = ("place", "investor.who", "investor.ticket", "investor.user_role")
ROOMS_TYPES = ("apartment", "house")
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


class Range(_Model):
    min: float | None = None
    max: float | None = None

    @field_validator("min", "max", mode="before")
    @classmethod
    def _number(cls, value: Any) -> Any:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, str):
            digits = re.sub(r"[^\d.,]", "", value).replace(",", ".")
            try:
                value = float(digits) if digits else None
            except ValueError:
                return None
        if isinstance(value, int | float) and not 0 <= value <= 1_000_000_000:
            return None
        return value

    def is_set(self) -> bool:
        return self.min is not None or self.max is not None


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
    rooms: Range = Field(default_factory=Range)
    area_m2: Range = Field(default_factory=Range)
    must_have: list[str] = Field(default_factory=list)
    # soft
    wishes: list[Wish] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    investor: Investor = Field(default_factory=Investor)
    sources: Sources = Field(default_factory=Sources)
    delivery: Delivery = Field(default_factory=Delivery)
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
        return _clean_list(value, 40, 30)

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
                trial = {**base, key: _overlay(base[key], value)}
                try:
                    TaskSpec.model_validate(trial)
                except ValidationError:
                    continue
                base = trial
        spec = TaskSpec.model_validate(base)
        extra = partial.get("unspecified") if isinstance(partial, dict) else None
        unspecified = [*self.unspecified, *_clean_list(extra, 40, 30)]
        spec.unspecified = [p for i, p in enumerate(unspecified)
                            if p not in unspecified[:i] and not (spec.filled(p) and p != "place")]
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


def _overlay(current: Any, new: Any) -> Any:
    """``new`` laid over ``current``: nested dicts merge, empty values keep the current one."""
    if new is None or new == "" or new == [] or new == {}:
        return current
    if isinstance(current, dict) and isinstance(new, dict):
        return {**current, **{k: _overlay(current.get(k), v) for k, v in new.items()}}
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
