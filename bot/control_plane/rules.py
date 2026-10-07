"""The deterministic interviewer: reads an answer with rules and asks the next field, one question at a time.

Used when no AI key is set or a call fails (the bot never breaks). It asks the same ordered hard fields as the
AI interviewer (``TaskSpec.missing_hard``), then two optional ones for real estate (district, must-haves), each
once. It reads what the person wrote with the planner's gazetteer and keyword rules (``plan_campaign``,
``details.py``) and, for a bare answer, with the field that was just asked about.
"""

from __future__ import annotations

import re
from typing import Any

from bot.campaign.architect import GAZETTEER, InvalidGoal, find_places, plan_campaign
from bot.campaign.spec import DEAL_RU, INVESTOR_WHO_RU, PROPERTY_RU, ROLE_RU, TaskSpec
from bot.control_plane.details import details, property_type
from bot.control_plane.interviewer import InterviewTurn

SKIP_WORDS = frozenset({"пропустить", "пропуск", "нет", "не важно", "неважно", "не имеет значения", "skip", "no", "-",
                        "без разницы", "любой", "любая", "любое"})
# With the AI interviewer only an explicit «doesn't matter» is a skip: «нет», «no», «-», «любой» may be the answer to a
# question («Есть ли парковка?» - «нет») and go to the model.
AI_SKIP_WORDS = frozenset({"не важно", "неважно", "без разницы", "всё равно", "все равно", "any"})
SOFT_ORDER = ("place.districts", "must_have")  # real estate: optional, asked once after the hard fields

SLOT_QUESTIONS = {
    "place": "В каком городе искать?",
    "deal": "Аренда или покупка?",
    "property_type": "Что ищете: квартиру, дом, участок, комнату или коммерческую недвижимость?",
    "budget.max": "Какой бюджет? Например, до 1200 € или «от 500 до 900 €».",
    "rooms.min": "Сколько комнат нужно? Например: студия, 2, от 2 до 3.",
    "place.districts": "Район или вся {city}? Например: «центр», «Руссафа и Кабаньял» или «вся {city}».",
    "must_have": "Что обязательно должно быть? Например: «балкон», «можно с животными», «лифт».",
    "investor.who": "Кого ищете? Например: инвесторы в недвижимость, стартапы, бизнес-ангелы.",
    "investor.ticket": "Какой размер вложения (тикет)? Например: от 100 тыс. €, 500 тыс. – 2 млн €.",
    "investor.user_role": "Вы ищете деньги для своего проекта или сами хотите вкладывать?",
    # fields offered only from «Изменить»
    "area_m2.min": "Какая площадь, м²? Например: от 50, 60–80.",
    "exclude": "Что исключить? Например: «первый этаж», «агентства», «без лифта».",
    "wishes": "Какие пожелания? Например: «рядом с метро», «новый дом».",
    "sources.required": "Какие сайты или группы обязательно проверить?",
    "investor.geography": "В каких странах или городах искать? Например: Испания, Португалия.",
}
SLOT_HINTS = {"place": " Напишите город, район или регион в любой стране."}
NOT_UNDERSTOOD = {"place": "Не понял город. ", "budget.max": "Не понял сумму. ", "investor.ticket": "Не понял сумму. "}
# The summary label (details/targets) -> TaskSpec values
TARGETS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("инвест", "інвест", "invest", "inversor", "inversion"), "инвесторы"),
    (("стартап", "startup"), "стартапы"),
    (("ангел", "angel"), "бизнес-ангелы"),
    (("фонд", "венчур", "fund", "venture", "vc"), "фонды"),
    (("компан", "бизнес", "бізнес", "предприним", "підприєм", "фирм", "фірм", "company", "companies",
      "business", "empresa", "emprendedor", "entrepreneur"), "компании и предприниматели"),
    (("девелопер", "застройщ", "забудовник", "developer", "promotor"), "девелоперы"),
    (("недвиж", "нерухом", "real", "inmobil"), "в недвижимость"),
)
_WHO_CODE = {"инвесторы": "private", "стартапы": "network", "бизнес-ангелы": "private", "фонды": "fund",
             "компании и предприниматели": "network", "девелоперы": "developer"}
_PROPERTY_CODE = {"участок": "land", "квартира": "apartment", "комната": "room", "дом": "house",
                  "коммерческая": "commercial"}
_ROLE_WORDS = {
    "raising": ("привлеч", "ищу инвест", "ищу деньги", "нужны деньги", "нужен инвестор", "нужны инвестор", "raising",
                "для проекта", "для стартапа", "для моего", "финансирован", "поднять"),
    "deploying": ("инвестир", "вложить", "вкладыва", "ищу проект", "deploy", "хочу купить бизнес", "вложу", "вкладу"),
}
_TARGET_WORD = re.compile(r"\w+")
_NUMBER = re.compile(r"(\d{1,3}(?:[ .,]\d{3})+|\d+(?:[.,]\d+)?)\s*(k|к|тыс\w*|тис\w*|млн\w*|mln|million\w*|m\b)?", re.IGNORECASE)
_VAGUE = re.compile(r"(где-?нибудь|где угодно|любо[йме]|неважно|не важно|не знаю|без разницы|anywhere|"
                    r"somewhere|де завгодно|будь-де)", re.IGNORECASE)
_NOT_A_PLACE = re.compile(r"(?<!\w)(квартир|комнат|участ|аренд|оренд|покуп|снять|купить|инвест|жиль|недвиж|apartment|"
                          r"rent|buy|house|invest)", re.IGNORECASE)
_CURRENCIES = (("€", "EUR"), ("eur", "EUR"), ("евро", "EUR"), ("євро", "EUR"), ("$", "USD"), ("usd", "USD"),
               ("долл", "USD"), ("£", "GBP"), ("gbp", "GBP"), ("₴", "UAH"), ("грн", "UAH"), ("₽", "RUB"), ("руб", "RUB"))


def targets(text: str) -> list[str]:
    """Summary labels for who an investors task looks for; empty when it does not say."""
    words = _TARGET_WORD.findall(text.casefold())
    return [label for stems, label in TARGETS if any(w.startswith(stems) for w in words)]


def parse_deal(text: str) -> str | None:
    t = text.casefold()
    if any(w in t for w in ("не важно", "неважно", "любая", "любой", "все равно", "всё равно", "any")):
        return "any"
    rent = any(w in t for w in ("аренд", "снять", "сним", "оренд", "rent", "alquiler"))
    sale = any(w in t for w in ("покуп", "купи", "купл", "продаж", "buy", "sale", "compra", "venta"))
    return "rent" if rent and not sale else "sale" if sale and not rent else None


def _amounts(text: str) -> list[int]:
    """Every amount in the text, in order: «от 100 тыс до 1,5 млн» -> [100000, 1500000]."""
    found: list[int] = []
    for match in _NUMBER.finditer(text):
        raw, suffix = match.group(1).replace(" ", ""), (match.group(2) or "").casefold()
        if suffix.startswith(("млн", "mln", "million")) or suffix == "m":
            factor = 1_000_000
        elif suffix:
            factor = 1000
        else:
            factor = 1
        if factor == 1 and re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
            value = int(re.sub(r"[.,]", "", raw))
        else:
            value = round(float(raw.replace(",", ".")) * factor)
        if 0 < value <= 1_000_000_000:
            found.append(value)
    return found


def parse_budget(text: str) -> int | None:
    amounts = _amounts(text)
    return amounts[0] if amounts and amounts[0] <= 100_000_000 else None


def _currency(text: str) -> str | None:
    t = text.casefold()
    return next((code for sign, code in _CURRENCIES if sign in t), None)


def _span(text: str) -> tuple[int | None, int | None]:
    """(min, max) of an amount range: «от 500 до 900», «до 900», «от 500», «500–900», a bare «900» is a maximum."""
    amounts = _amounts(text)
    if not amounts:
        return None, None
    t = text.casefold()
    if len(amounts) >= 2:
        return min(amounts[:2]), max(amounts[:2])
    if re.search(r"(?<!\w)(от|from|min|не меньше|не менее|больше)\b", t) and not re.search(r"(?<!\w)(до|up to|max|under)\b", t):
        return amounts[0], None
    return None, amounts[0]


def geo_ru(canonical: str) -> str:
    """The Russian name of a well-known city (``Madrid`` -> «Мадрид»), else the name as it is."""
    return next((p.aliases["ru"] for p in GAZETTEER if p.canonical == canonical), canonical)


def gazetteer_place(canonical: str) -> dict[str, Any]:
    """``Place`` fields of a well-known city: its country and names in the planner's languages."""
    for p in GAZETTEER:
        if p.canonical == canonical:
            names = {"ru": p.aliases["ru"], "es": p.aliases["es"], "uk": p.aliases["uk"],
                     "ru_in": p.locative["ru"], "uk_in": p.locative["uk"]}
            return {"name": canonical, "country": p.country, "names": names}
    return {"name": canonical}


def typed_place(text: str) -> str | None:
    """A reply that is only a place («Убуд, Бали», «в Дубае»): its name as typed."""
    name = re.sub(r"^(?:в|во|у|на|in|en)\s+", "", " ".join(text.split()).strip(" .!?"), flags=re.IGNORECASE)
    if not 2 <= len(name) <= 80 or re.search(r"\d", name) or len(name.split()) > 4 or _VAGUE.search(name):
        return None
    if _NOT_A_PLACE.search(name):  # a sentence about the task, not a place
        return None
    return name


def _split(text: str) -> list[str]:
    return [p.strip(" .") for p in re.split(r"[,;\n]| и ", text) if p.strip(" .")]


def question_for(spec: TaskSpec, path: str) -> str:
    template = SLOT_QUESTIONS.get(path, "Что указать?")
    city = spec.place.names.get("ru") or spec.place_name() or "город"
    return template.format(city=city) + SLOT_HINTS.get(path, "")


def options_for(path: str) -> tuple[tuple[str, str], ...]:
    if path == "deal":
        return (("Аренда", "deal:rent"), ("Покупка", "deal:sale"))
    if path == "investor.user_role":
        return (("Ищу деньги", "role:raising"), ("Хочу вкладывать", "role:deploying"))
    return ()


def next_path(spec: TaskSpec, mode: str, *, soft: bool = True) -> str | None:
    """The next field to ask about: a missing hard one, then the optional ones (real estate), else None."""
    if missing := spec.missing_hard(mode):
        return missing[0]
    if soft and mode == "real_estate":
        return next((p for p in SOFT_ORDER if not spec.answered(p)), None)
    return None


class RuleInterviewer:
    """Reads the message with rules; asks the next field with a fixed question."""

    model = "rules"

    async def interview(self, *, mode: str, spec: TaskSpec, dialogue: list[dict[str, str]], message: str,
                        asking: str | None = None, editing: bool = False) -> InterviewTurn:
        before = spec
        after = spec.model_copy(deep=True)
        after.mode = mode  # type: ignore[assignment]
        text = message.strip()
        if editing and ":" in text:
            text = text.split(":", 1)[1].strip()
        many = self._read(after, mode, text, asking, editing)
        changed = after.model_dump() != before.model_dump()
        path = next_path(after, mode, soft=not editing)
        understood = _describe(before, after) if changed else ""
        if path is None:
            return InterviewTurn(after, None, True, understood)
        if path == "place" and many:
            names = ", ".join(many)
            return InterviewTurn(after, f"Указано несколько городов ({names}). Одна задача — один город. Какой выбрать?",
                                 False, understood, path)
        question = question_for(after, path)
        if asking and not changed and not editing and text and path == asking:
            question = NOT_UNDERSTOOD.get(path, "Не понял ответ. ") + question
        return InterviewTurn(after, question, False, understood, path, options_for(path))

    def _read(self, spec: TaskSpec, mode: str, text: str, asking: str | None, editing: bool) -> list[str]:
        """Fill ``spec`` from ``text``; returns the cities named when there are several."""
        many: list[str] = []
        places = [geo_ru(p) for p in find_places(text)]
        if len(find_places(text)) == 1:
            self._set_place(spec, gazetteer_place(find_places(text)[0]))
        elif len(places) > 1:
            many = places
        elif asking == "place" and (name := typed_place(text)) is not None:
            self._set_place(spec, {"name": name})
        if mode == "investors":
            self._read_investors(spec, text, asking, editing)
        else:
            self._read_real_estate(spec, text, asking, editing)
        self._read_asked_lists(spec, text, asking, editing)
        return many

    @staticmethod
    def _set_place(spec: TaskSpec, fields: dict[str, Any]) -> None:
        districts = spec.place.districts
        spec.place = spec.place.__class__.model_validate({**fields, "districts": districts})
        spec.unspecified = [p for p in spec.unspecified if p != "place"]

    def _read_real_estate(self, spec: TaskSpec, text: str, asking: str | None, editing: bool) -> None:
        fresh = spec.place_name() or "Madrid"
        try:
            plan = plan_campaign(text, vertical="real_estate", location=fresh)
        except InvalidGoal:
            plan = None
        deal = parse_deal(text)
        if deal == "any" and asking != "deal":
            deal = None
        if deal is None and plan is not None:
            deal = plan.constraints.get("deal")  # type: ignore[assignment]
        if deal:
            spec.deal = deal  # type: ignore[assignment]
        label = property_type(text)
        if label in _PROPERTY_CODE:
            spec.property_type = _PROPERTY_CODE[label]  # type: ignore[assignment,index]
        elif asking == "property_type" and parse_deal(text) == "any":
            spec.property_type = "any"
        if asking == "area_m2.min":
            low, high = _span(text)
            spec.area_m2 = spec.area_m2.model_copy(update={"min": low if low is not None else high,
                                                           "max": high if low is not None else None})
        elif asking == "rooms.min" and (found := re.findall(r"\d{1,2}", text)):
            nums = [int(n) for n in found]
            spec.rooms = spec.rooms.model_copy(update={"min": min(nums[:2]), "max": max(nums[:2]) if len(nums) > 1 else None})
        elif asking == "rooms.min" and re.search(r"студи", text, re.IGNORECASE):
            spec.rooms = spec.rooms.model_copy(update={"min": 1})
        elif plan is not None and plan.constraints.get("rooms") and not spec.rooms.is_set():
            spec.rooms = spec.rooms.model_copy(update={"min": plan.constraints["rooms"]})
        low, high = (_span(text) if asking == "budget.max" else (None, None))
        if asking != "budget.max" and plan is not None:
            high = plan.constraints.get("max_price")  # type: ignore[assignment]
            low = _span(text)[0] if high is not None and len(_amounts(text)) > 1 else None
        if low is not None or high is not None:
            spec.budget = spec.budget.model_copy(update={
                "min": low if low is not None else spec.budget.min,
                "max": high if high is not None else spec.budget.max,
                "currency": _currency(text) or spec.budget.currency})
        # extra wishes read by the keyword rules ("площадь от 1000 м²", "до метро 5 мин", "под застройку", ...)
        known = {w.text.casefold() for w in spec.wishes}
        wishes = [*({"text": w.text, "weight": w.weight} for w in spec.wishes),
                  *({"text": w, "weight": 2} for w in details(text) if w.casefold() not in known)]
        if len(wishes) != len(spec.wishes):
            spec.wishes = spec.__class__.model_validate({"wishes": wishes}).wishes

    def _read_investors(self, spec: TaskSpec, text: str, asking: str | None, editing: bool) -> None:
        labels = targets(text)
        who = [_WHO_CODE[label] for label in labels if label in _WHO_CODE]
        if "в недвижимость" in labels and "real_estate" not in spec.investor.asset_class:
            spec.investor.asset_class = [*spec.investor.asset_class, "real_estate"]
            who = who or ["private"]
        if who:
            spec.investor.who = list(dict.fromkeys([*([] if editing else spec.investor.who), *who]))
        elif asking == "investor.who" and text and _norm(text) not in SKIP_WORDS:
            spec.investor.who = [text[:80].casefold()]
        if asking == "investor.ticket":
            low, high = _span(text)
            if low is not None or high is not None:
                spec.investor.ticket = spec.investor.ticket.model_copy(update={
                    "min": low, "max": high, "currency": _currency(text) or spec.investor.ticket.currency})
        lowered = text.casefold()
        for role, words in _ROLE_WORDS.items():
            if any(w in lowered for w in words) and (asking == "investor.user_role" or re.search(r"(?<!\w)я\s", lowered)):
                spec.investor.user_role = role  # type: ignore[assignment]
                break

    @staticmethod
    def _read_asked_lists(spec: TaskSpec, text: str, asking: str | None, editing: bool) -> None:
        """A bare answer to a list field («центр, Руссафа»): its items; an edit replaces, an answer adds."""
        if asking not in ("place.districts", "must_have", "exclude", "wishes", "sources.required",
                          "investor.geography"):
            return
        items = [] if _norm(text) in SKIP_WORDS else _split(text)
        if not items:
            return
        if asking == "place.districts":
            current = [] if editing else spec.place.districts
            spec.place = spec.place.model_copy(update={"districts": [*current, *items][:12]})
        elif asking == "must_have":
            spec.must_have = [*([] if editing else spec.must_have), *items][:12]
        elif asking == "exclude":
            spec.exclude = [*([] if editing else spec.exclude), *items][:12]
        elif asking == "wishes":
            keep = [] if editing else [{"text": w.text, "weight": w.weight} for w in spec.wishes]
            spec.wishes = spec.__class__.model_validate({"wishes": [*keep, *items]}).wishes
        elif asking == "sources.required":
            spec.sources = spec.sources.model_copy(update={"required": items[:12]})
        else:
            spec.investor.geography = [*([] if editing else spec.investor.geography), *items][:12]


def _norm(text: str) -> str:
    return text.casefold().strip(".! ")


def _describe(before: TaskSpec, after: TaskSpec) -> str:
    """What the message added, as one short Russian phrase («Мадрид, аренда, квартира, до 1200 €»)."""
    parts: list[str] = []
    if after.place_name() and after.place != before.place:
        parts.append(after.place.names.get("ru") or after.place_name() or "")
    if after.deal and after.deal != before.deal:
        parts.append(DEAL_RU[after.deal])
    if after.property_type and after.property_type != before.property_type:
        parts.append(PROPERTY_RU[after.property_type])
    if after.budget != before.budget and after.budget.is_set():
        parts.append(_money_phrase(after))
    if after.rooms != before.rooms and after.rooms.is_set():
        parts.append(f"комнат: {_phrase_range(after.rooms.min, after.rooms.max)}")
    if after.area_m2 != before.area_m2 and after.area_m2.is_set():
        parts.append(f"площадь {_phrase_range(after.area_m2.min, after.area_m2.max)} м²")
    if after.investor.who != before.investor.who and after.investor.who:
        parts.append(", ".join(INVESTOR_WHO_RU.get(w, w) for w in after.investor.who))
    if after.investor.ticket != before.investor.ticket and after.investor.ticket.is_set():
        parts.append("тикет " + _phrase_range(after.investor.ticket.min, after.investor.ticket.max))
    if after.investor.user_role and after.investor.user_role != before.investor.user_role:
        parts.append(ROLE_RU[after.investor.user_role])
    if after.place.districts != before.place.districts and after.place.districts:
        parts.append("район: " + ", ".join(after.place.districts))
    if after.must_have != before.must_have and after.must_have:
        parts.append("обязательно: " + ", ".join(after.must_have))
    if len(after.wishes) > len(before.wishes):
        parts.append(", ".join(w.text for w in after.wishes[len(before.wishes):]))
    return ", ".join(p for p in parts if p)


def _phrase_range(low: float | None, high: float | None) -> str:
    def num(v: float) -> str:
        return str(int(v))

    if low is not None and high is not None:
        return f"{num(low)}–{num(high)}"
    if low is not None:
        return f"от {num(low)}"
    return f"до {num(high)}" if high is not None else ""


def _money_phrase(spec: TaskSpec) -> str:
    sign = {"EUR": "€", "USD": "$", "GBP": "£", "UAH": "₴", "RUB": "₽"}.get(spec.budget.currency or "EUR", spec.budget.currency)
    return f"{_phrase_range(spec.budget.min, spec.budget.max)} {sign}"
