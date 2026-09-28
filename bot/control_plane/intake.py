"""Task intake for people with the ``user`` role (also open to operators and owners).

The person picks a mode like the old bot did (🏡 objects -> ``real_estate``,
💼 investors -> ``investors``), then writes or says the task. There is no
fixed script: the task is parsed first and only the critical fields that are
still missing are asked -- for real estate the city, rent or purchase and the
budget; for investors the city and who to look for -- at most three short
questions, all in one message, answered with buttons or one typed reply
("Мадрид, аренда, до 1200 €"). A complete task skips the questions. Then
comes «Проверьте задачу» with [Запустить] [Изменить] [Отмена]; only
"Запустить" queues an Orchestra ``campaign`` command, and the draft is reset
in the same statement, so a double tap queues one campaign.

Replies are plain Russian and never repeat what the person said (a voice
note's transcript stays internal). Owners also see the technical details:
the planner's goal line, search languages, group windows and the queue id.

The chosen mode and city are authoritative: they are queued as leading
``mode=<vertical> city=<name>`` tokens that the Orchestra passes to
``plan_campaign`` as overrides. A draft untouched for 24 hours is treated as
gone. Owners get a one-line notice when a user launches.

Understanding is done by AI when an ``Understander`` is configured
(``bot/control_plane/understanding.py``): one call per task and one per
answer; the model picks the main points and the extra wishes, asks for what
is missing and writes the «Проверьте задачу» body in Russian. The code still
enforces the critical fields (a gazetteer city always; the deal once for real
estate; who to look for once for investors) and caps the questions. Without a
key, or when a call fails or returns nothing usable, the deterministic
clarifier (``plan_campaign`` and its gazetteer, ``details.py``) takes over, so
the bot never breaks. The Orchestra still re-plans the goal and applies its
quotas and breakers.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from bot.campaign import geo
from bot.campaign.architect import (
    MAX_TEXT_CHARS,
    InvalidGoal,
    find_places,
    plan_campaign,
)
from bot.campaign.models import CampaignPlan
from bot.control_plane.details import details, property_type
from bot.control_plane.models import Button, CommandEnvelope, IncomingMessage, Reply
from bot.control_plane.understanding import (
    MAX_ANSWER_CHARS,
    MAX_ANSWERS,
    PROPERTY_RU,
    TaskUnderstanding,
    Understander,
    UnderstandingError,
)

log = logging.getLogger(__name__)
CommandSink = Callable[[CommandEnvelope], Awaitable[object]]
OwnerNotice = Callable[[str], Awaitable[None]]

# mode (the plan's vertical) -> button title from the old bot
MODES: dict[str, str] = {
    "real_estate": "🏡 Участки и объекты",
    "investors": "💼 Инвесторы и компании",
}
DRAFT_TTL = timedelta(hours=24)
MAX_QUESTIONS = 3
MAX_TASK_CHARS = MAX_TEXT_CHARS - 100  # room for the mode word and the answers
CANCEL_WORDS = frozenset({"отмена", "отменить", "cancel", "скасувати"})
SKIP_WORDS = frozenset({"пропустить", "пропуск", "нет", "не важно", "неважно", "skip", "no", "-"})
DEAL_TEXT = {"rent": "аренда", "sale": "покупка", "any": "не важно"}
EXAMPLES = {"real_estate": "«квартиры в аренду в Мадриде до 1200 €»", "investors": "«инвесторы для стартапа в Барселоне»"}
ANSWER_EXAMPLES = {"real_estate": "«Мадрид, аренда, до 1200 €»", "investors": "«Мадрид, инвесторы в недвижимость»"}
# investors: who to look for (word prefixes, casefolded) -> the label shown in the summary
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
_TARGET_WORD = re.compile(r"\w+")
_NUMBER = re.compile(r"(\d{1,3}(?:[ .,]\d{3})+|\d+(?:[.,]\d+)?)\s*(k|к|тыс\w*|тис\w*)?", re.IGNORECASE)


@dataclass(slots=True)
class Draft:
    user_id: int
    chat_id: int
    mode: str | None = None
    step: str = "idle"  # idle | city | deal | budget | target | summary
    task: str = ""
    message_id: int = 0  # the task's Telegram message: the Orchestra idempotency key
    city: str | None = None
    deal: str | None = None  # rent | sale | any
    budget: int | None = None
    asked: list[str] = field(default_factory=list)  # slots asked about, in order
    options: list[str] = field(default_factory=list)  # cities offered when the task named several
    updated_at: datetime | None = None
    target: str | None = None  # investors: who to look for, when the task did not say
    pending: list[str] = field(default_factory=list)  # the slots of the question on screen
    ai: dict[str, Any] | None = None  # the AI's TaskUnderstanding; None = deterministic rules
    answers: list[str] = field(default_factory=list)  # typed answers and button choices, for the next AI call
    # The city's names per language and its country (from the AI, or the typed name): any place in the world.
    place: dict[str, Any] | None = None

    def copy(self, **changes: Any) -> Draft:
        return replace(self, asked=list(self.asked), options=list(self.options), pending=list(self.pending),
                       ai=dict(self.ai) if self.ai is not None else None, answers=list(self.answers),
                       place=dict(self.place) if self.place is not None else None, **changes)

    def understanding(self) -> TaskUnderstanding | None:
        return TaskUnderstanding.load(self.ai) if self.ai is not None else None

    def goal_text(self) -> str:
        """The task plus the answers given in words (who, deal, budget).

        With an AI understanding: the deal and budget first (the planner reads
        the first price it finds), the type, the task, then the main points and
        the extra wishes in Russian, within the planner's length limit.
        """
        if (ai := self.understanding()) is not None:
            return self._ai_goal(ai)
        parts = [self.task]
        if self.target:
            parts.append(self.target)
        if self.deal in ("rent", "sale"):
            parts.append(DEAL_TEXT[self.deal])
        if self.budget:
            parts.append(f"до {self.budget} €")
        return " ".join(p for p in parts if p)

    def _ai_goal(self, ai: TaskUnderstanding) -> str:
        head: list[str] = []
        if self.deal in ("rent", "sale"):
            head.append(DEAL_TEXT[self.deal])
        if self.budget:
            currency = ai.currency or "EUR"
            head.append(f"до {self.budget} €" if currency == "EUR" else f"бюджет {self.budget} {currency}")
        if self.mode != "investors" and ai.property_type in PROPERTY_RU and ai.property_type != "other":
            head.append(f"Тип: {PROPERTY_RU[ai.property_type]}.")
        if self.mode == "investors" and (self.target or ai.target):
            head.append(f"Кого ищем: {self.target or ai.target}.")
        tail: list[str] = []
        if ai.primary:
            tail.append("Главное: " + "; ".join(ai.primary) + ".")
        if ai.secondary:
            tail.append("Дополнительно: " + "; ".join(ai.secondary) + ".")
        head_text, tail_text = " ".join(head), " ".join(tail)
        room = MAX_TEXT_CHARS - len(head_text) - len(tail_text) - 12
        task = self.task if len(self.task) <= room else self.task[:max(room, 0)].rstrip() + "…"
        return " ".join(p for p in (head_text, f"Задача: {task}" if task else "", tail_text) if p)[:MAX_TEXT_CHARS]

    def plan(self) -> CampaignPlan:
        """What the Orchestra will plan; raises ``InvalidGoal``."""
        return plan_campaign(self.goal_text(), vertical=self.mode,  # type: ignore[arg-type]
                             location=None if self.place else self.city, place=self.place)

    def command_arguments(self) -> str:
        """``mode=<vertical> [place=<names>|city=<name>] <goal>``: the choices travel with the queued command."""
        tokens = [f"mode={self.mode}"] if self.mode else []
        if self.place:
            tokens.append(f"place={encode_place(self.place)}")
        elif self.city:
            tokens.append(f"city={self.city.replace(' ', '_')}")
        return " ".join([*tokens, self.goal_text()])

    def fresh(self) -> Draft:
        """Same person, chat and mode; no task."""
        return Draft(self.user_id, self.chat_id, self.mode)

    def payload(self) -> dict[str, Any]:
        return {"task": self.task, "message_id": self.message_id, "city": self.city, "deal": self.deal,
                "budget": self.budget, "asked": self.asked, "options": self.options, "target": self.target,
                "pending": self.pending, "ai": self.ai, "answers": self.answers, "place": self.place}

    @classmethod
    def load(cls, user_id: int, chat_id: int, mode: str | None, step: str, payload: dict[str, Any],
             updated_at: datetime | None = None) -> Draft:
        return cls(user_id, chat_id, mode, step, str(payload.get("task") or ""), int(payload.get("message_id") or 0),
                   payload.get("city"), payload.get("deal"), payload.get("budget"),
                   list(payload.get("asked") or []), list(payload.get("options") or []), updated_at,
                   payload.get("target"), list(payload.get("pending") or []),
                   dict(payload["ai"]) if isinstance(payload.get("ai"), dict) else None,
                   [str(a) for a in payload.get("answers") or []],
                   dict(payload["place"]) if isinstance(payload.get("place"), dict) else None)

    def expired(self, now: datetime) -> bool:
        return self.updated_at is not None and now - self.updated_at > DRAFT_TTL


@dataclass(frozen=True, slots=True)
class Question:
    slot: str  # city | deal | budget | target: the first question; its buttons are shown
    text: str
    options: tuple[tuple[str, str], ...] = ()  # (button text, value)
    also: tuple[str, ...] = ()  # further slots asked in the same message


Clarifier = Callable[[Draft], Question | None]

SLOT_QUESTIONS = {
    "city": "В каком городе искать?",
    "deal": "Аренда или покупка?",
    "budget": "Какой бюджет? Например, до 1200 €.",
    "target": "Кого ищете? Например: инвесторы в недвижимость, стартапы, бизнес-ангелы.",
}
SLOT_HINTS = {"city": " Напишите город, район или регион в любой стране.", "budget": " Или нажмите «Пропустить».", "target": ""}


def targets(text: str) -> list[str]:
    """Summary labels for who an investors task looks for; empty when it does not say."""
    words = _TARGET_WORD.findall(text.casefold())
    return [label for stems, label in TARGETS if any(w.startswith(stems) for w in words)]


def missing_slots(draft: Draft) -> list[str]:
    """The critical fields the task still lacks, in asking order; raises ``InvalidGoal``.

    The city is always needed. Other slots are asked once: one the person
    skipped stays in ``draft.asked`` and is not asked again.
    """
    city_missing = draft.city is None and len(find_places(draft.task)) != 1
    # Without a place the planner cannot run; a stand-in reads deal and budget.
    location = draft.city or ("Madrid" if city_missing else None)
    plan = plan_campaign(draft.goal_text(), vertical=draft.mode,  # type: ignore[arg-type]
                         location=None if draft.place else location, place=draft.place)
    missing = ["city"] if city_missing else []

    def may_ask(slot: str) -> bool:
        return slot not in draft.asked and len(draft.asked) < MAX_QUESTIONS

    if plan.vertical == "investors":
        if draft.target is None and not targets(draft.task) and may_ask("target"):
            missing.append("target")
        return missing
    if plan.constraints.get("deal") is None and draft.deal is None and may_ask("deal"):
        missing.append("deal")
    if plan.constraints.get("max_price") is None and may_ask("budget"):
        missing.append("budget")
    return missing


def deterministic_clarifier(draft: Draft) -> Question | None:
    """What to ask next (every missing field in one message), or None when the task is ready; raises ``InvalidGoal``."""
    slots = missing_slots(draft)
    if not slots:
        return None
    first = slots[0]
    options: tuple[tuple[str, str], ...] = ()
    city_text = SLOT_QUESTIONS["city"]
    if first == "city":
        places = find_places(draft.task)
        if len(places) > 1:
            names = ", ".join(geo_ru(name) for name in places)
            city_text = f"Указано несколько городов ({names}). Одна задача — один город. Какой выбрать?"
    elif first == "deal":
        options = (("Аренда", "rent"), ("Покупка", "sale"), ("Не важно", "any"))
    elif first == "budget":
        options = (("Пропустить", "skip"),)
    texts = [city_text if slot == "city" else SLOT_QUESTIONS[slot] for slot in slots]
    if len(slots) == 1:
        hint = SLOT_HINTS.get(first, "") if texts[0] in SLOT_QUESTIONS.values() else ""
        return Question(first, texts[0] + hint, options)
    example = ANSWER_EXAMPLES.get(draft.mode or "", ANSWER_EXAMPLES["real_estate"])
    lines = ["Уточните, пожалуйста:", *(f"{n}. {q}" for n, q in enumerate(texts, 1)),
             f"Можно ответить одним сообщением, например {example}."]
    return Question(first, "\n".join(lines), options, tuple(slots[1:]))


class IntakeStore(Protocol):
    async def get(self, user_id: int) -> Draft | None: ...
    async def save(self, draft: Draft) -> None: ...
    async def launch(self, user_id: int) -> Draft | None:
        """Atomically take a draft that reached the summary and reset it; None if there is none."""
        ...


class MemoryIntakeStore:
    def __init__(self) -> None:
        self.drafts: dict[int, Draft] = {}

    async def get(self, user_id: int) -> Draft | None:
        draft = self.drafts.get(user_id)
        return draft.copy() if draft else None

    async def save(self, draft: Draft) -> None:
        self.drafts[draft.user_id] = draft.copy(updated_at=datetime.now(UTC))

    async def launch(self, user_id: int) -> Draft | None:
        draft = self.drafts.get(user_id)
        if draft is None or draft.step != "summary":
            return None
        self.drafts[user_id] = replace(draft.fresh(), updated_at=datetime.now(UTC))
        return draft


class PostgresIntakeStore:
    """Shares the control plane's pool (migration 018)."""

    def __init__(self, pool_owner: Any) -> None:
        self._owner = pool_owner

    def _pool(self) -> Any:
        return self._owner._pool()

    async def get(self, user_id: int) -> Draft | None:
        row = await self._pool().fetchrow(
            "select telegram_chat_id, mode, step, draft::text, updated_at from public.user_task_drafts where telegram_user_id = $1",
            user_id)
        return Draft.load(user_id, row[0], row[1], row[2], json.loads(row[3]), row[4]) if row else None

    async def save(self, draft: Draft) -> None:
        await self._pool().execute(
            """insert into public.user_task_drafts (telegram_user_id, telegram_chat_id, mode, step, draft)
               values ($1, $2, $3, $4, $5::jsonb)
               on conflict (telegram_user_id) do update set telegram_chat_id = excluded.telegram_chat_id,
                 mode = excluded.mode, step = excluded.step, draft = excluded.draft, updated_at = now()""",
            draft.user_id, draft.chat_id, draft.mode, draft.step, json.dumps(draft.payload(), ensure_ascii=False),
        )

    async def launch(self, user_id: int) -> Draft | None:
        # The row lock makes a concurrent second tap see step = 'idle' and update nothing.
        row = await self._pool().fetchrow(
            """update public.user_task_drafts d
                  set step = 'idle', draft = '{}'::jsonb, updated_at = now(), launched_at = now()
                 from (select telegram_user_id, draft from public.user_task_drafts
                        where telegram_user_id = $1 and step = 'summary' for update) old
                where d.telegram_user_id = old.telegram_user_id and d.step = 'summary'
            returning d.telegram_chat_id, d.mode, old.draft::text""",
            user_id,
        )
        return Draft.load(user_id, row[0], row[1], "summary", json.loads(row[2])) if row else None


def mode_menu(text: str = "Выберите режим:") -> Reply:
    return Reply(text, tuple(Button(title, callback_data=f"mode:{mode}") for mode, title in MODES.items()))


# The bottom (reply) keyboard: the control buttons stay at hand below the chat, not under one message.
# A tap sends the label as a message; ``key_command`` turns it back into the command.
LAUNCH_KEY = "▶️ Запустить"
EDIT_KEY = "✏️ Изменить"
CANCEL_KEY = "❌ Отмена"
STOP_KEY = "⏹ Остановить поиск"
NEW_SEARCH_KEY = "🔍 Новый поиск"
DRAFT_KEYBOARD: tuple[tuple[str, ...], ...] = ((CANCEL_KEY,),)
TASK_KEYBOARD: tuple[tuple[str, ...], ...] = ((LAUNCH_KEY, EDIT_KEY), (CANCEL_KEY,))
SEARCH_KEYBOARD: tuple[tuple[str, ...], ...] = ((STOP_KEY,),)
IDLE_KEYBOARD: tuple[tuple[str, ...], ...] = ((NEW_SEARCH_KEY,),)
_KEY_COMMANDS = {LAUNCH_KEY: "запустить", EDIT_KEY: "изменить", CANCEL_KEY: "отмена", STOP_KEY: "остановить поиск",
                 NEW_SEARCH_KEY: "новый поиск"}


def key_command(text: str) -> str | None:
    """The plain command of a bottom-keyboard label («⏹ Остановить поиск» -> «остановить поиск»), else None."""
    return _KEY_COMMANDS.get(text.strip())


def parse_deal(text: str) -> str | None:
    t = text.casefold()
    if any(w in t for w in ("не важно", "неважно", "любая", "любой", "все равно", "всё равно", "any")):
        return "any"
    rent = any(w in t for w in ("аренд", "снять", "сним", "оренд", "rent", "alquiler"))
    sale = any(w in t for w in ("покуп", "купи", "купл", "продаж", "buy", "sale", "compra", "venta"))
    return "rent" if rent and not sale else "sale" if sale and not rent else None


def parse_budget(text: str) -> int | None:
    match = _NUMBER.search(text)
    if not match:
        return None
    raw = match.group(1).replace(" ", "")
    if match.group(2):
        value = round(float(raw.replace(",", ".")) * 1000)
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", raw):
        value = int(re.sub(r"[.,]", "", raw))
    else:
        value = round(float(raw.replace(",", ".")))
    return value if 0 < value <= 100_000_000 else None


def geo_ru(canonical: str) -> str:
    """The Russian name of a well-known city (``Madrid`` -> «Мадрид»), else the name as it is."""
    from bot.campaign.architect import GAZETTEER

    return next((p.aliases["ru"] for p in GAZETTEER if p.canonical == canonical), canonical)


def _city_ru(plan: CampaignPlan) -> str:
    return plan.location_aliases.get("ru") or geo_ru(plan.location)


def encode_place(place: dict[str, Any]) -> str:
    """The place as one token of the queued command (URL-safe base64 of its JSON, no spaces)."""
    import base64
    import json

    data = {k: v for k, v in place.items() if isinstance(v, str) and v}
    return base64.urlsafe_b64encode(json.dumps(data, ensure_ascii=False).encode()).decode().rstrip("=")


_VAGUE = re.compile(r"(где-?нибудь|где угодно|любо[йме]|неважно|не важно|не знаю|без разницы|anywhere|"
                    r"somewhere|де завгодно|будь-де)", re.IGNORECASE)


def typed_place(text: str) -> dict[str, Any] | None:
    """A reply that is only a place («Убуд, Бали», «в Дубае»): its name as typed, for every language."""
    name = re.sub(r"^(?:в|во|у|на|in|en)\s+", "", " ".join(text.split()).strip(" .!?"), flags=re.IGNORECASE)
    if not 2 <= len(name) <= 80 or re.search(r"\d", name) or len(name.split()) > 6 or _VAGUE.search(name):
        return None
    return {"en": name, "es": name, "ru": name, "uk": name}


def summary(draft: Draft, plan: CampaignPlan, technical: bool = False) -> Reply:
    """«Проверьте задачу»: what was understood, in Russian; owners also get the planner's details."""
    lines = [
        "Проверьте задачу:",
        f"Режим: {MODES[draft.mode] if draft.mode in MODES else MODES.get(plan.vertical, plan.vertical)}",
        f"Город: {_city_ru(plan)}",
    ]
    if plan.vertical == "investors":
        found = targets(draft.goal_text())
        lines.append(f"Кого ищем: {', '.join(found) if found else 'инвесторы и компании'}")
    else:
        deal = plan.constraints.get("deal")
        lines.append(f"Сделка: {DEAL_TEXT.get(str(deal), 'не важно')}")
        if kind := property_type(draft.task):
            lines.append(f"Тип: {kind}")
        price = plan.constraints.get("max_price")
        lines.append(f"Бюджет: {f'до {price} €' if price else 'не указан'}")
        if wishes := details(draft.task):
            lines.append(f"Пожелания: {', '.join(wishes)}")
    if technical:
        limits = plan.limits
        lines += [
            f"Цель: {plan.goal}",
            f"Языки поиска: {', '.join(lang.upper() for lang in plan.languages)}",
            f"Группы: до {limits.max_groups}, окнами по {limits.window_size}",
        ]
    lines += ["", "Всё верно? Нажмите «Запустить» внизу, чтобы начать поиск."]
    return Reply("\n".join(lines), keyboard=TASK_KEYBOARD)


def quotes(text: str, source: str, n: int = 7) -> bool:
    """``text`` repeats ``n`` words in a row from ``source``: a transcript being echoed."""
    src, out = _TARGET_WORD.findall(source.casefold()), _TARGET_WORD.findall(text.casefold())
    grams = {tuple(src[i:i + n]) for i in range(len(src) - n + 1)}
    return any(tuple(out[i:i + n]) in grams for i in range(len(out) - n + 1))


def _ai_body(draft: Draft, ai: TaskUnderstanding) -> str:
    """The AI's own summary; rebuilt from its short lists when it echoes the person's words."""
    if not quotes(ai.summary_ru, " ".join([draft.task, *draft.answers])):
        return ai.summary_ru
    lines = []
    if ai.primary:
        lines.append("Главное: " + ", ".join(ai.primary))
    if ai.secondary:
        lines.append("Дополнительно: " + ", ".join(ai.secondary))
    return "\n".join(lines) or "Задача понята."


def ai_summary(draft: Draft, ai: TaskUnderstanding, plan: CampaignPlan, technical: bool = False, model: str = "") -> Reply:
    """«Проверьте задачу» written by the AI; the city the search will use is always visible."""
    body = _ai_body(draft, ai)
    lines = ["Проверьте задачу:"]
    if not geo.mentions_place(body, geo.place_names(plan.location, dict(plan.location_aliases))):
        lines.append(f"Город: {_city_ru(plan)}")
    lines.append(body)
    if technical:
        limits = plan.limits
        lines += [
            "",
            f"Разбор задачи: ИИ ({model})" if model else "Разбор задачи: ИИ",
            f"Цель: {plan.goal}",
            f"Языки поиска: {', '.join(lang.upper() for lang in plan.languages)}",
            f"Группы: до {limits.max_groups}, окнами по {limits.window_size}",
        ]
    lines += ["", "Всё верно? Нажмите «Запустить» внизу, чтобы начать поиск."]
    return Reply("\n".join(lines), keyboard=TASK_KEYBOARD)


LAUNCHED = "Принято. Начинаю поиск. Найденные варианты пришлю сюда."
STOP_HINT = "Чтобы остановить поиск, нажмите «Остановить поиск» внизу или напишите «стоп»."
STOP_CALLBACK = "search:stop"
# Words that show an AI question already covers a critical slot.
_ASKS_ABOUT = {
    "city": ("город", "где", "район"),
    "deal": ("аренд", "покуп", "купить", "снять", "сделк"),
    "target": ("кого", "кто"),
}
AI_QUESTION_ROUNDS = 2  # the AI's own (optional) questions stop after two answers


def stop_button() -> Button:
    return Button("Остановить поиск", callback_data=STOP_CALLBACK)
NOT_UNDERSTOOD = {"city": "Не понял город. ", "budget": "Не понял сумму. "}


class TaskIntake:
    def __init__(self, store: IntakeStore, sink: CommandSink, clarifier: Clarifier = deterministic_clarifier,
                 notify_owners: OwnerNotice | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 technical: Callable[[int], bool] = lambda _user_id: False,
                 understander: Understander | None = None) -> None:
        """``technical(user_id)`` says who also sees planner details and queue ids (the owner).

        ``understander`` (optional) reads tasks with AI; without it, or when it
        fails, the deterministic ``clarifier`` is used.
        """
        self.store, self.sink, self.clarifier = store, sink, clarifier
        self.notify_owners, self.now, self.technical = notify_owners, now, technical
        self.understander = understander

    async def _draft(self, user_id: int, chat_id: int) -> Draft:
        draft = await self.store.get(user_id)
        if draft is None:
            return Draft(user_id, chat_id)
        if draft.expired(self.now()):  # untouched for a day: the task is gone, the mode stays
            draft = draft.fresh()
        draft.chat_id = chat_id
        return draft

    async def mode(self, user_id: int) -> str | None:
        draft = await self.store.get(user_id)
        return draft.mode if draft else None

    async def drafting(self, user_id: int) -> bool:
        """A task is being written (a question or the summary is on screen) and has not expired."""
        draft = await self.store.get(user_id)
        return draft is not None and draft.step != "idle" and not draft.expired(self.now())

    async def choose_mode(self, user_id: int, chat_id: int, mode: str) -> Reply:
        if mode not in MODES:
            return Reply("Эта кнопка устарела.")
        draft = await self._draft(user_id, chat_id)
        draft.mode = mode
        if draft.task:  # a task written before the mode was chosen, or a mode switch mid-task
            draft.asked, draft.pending, draft.answers, draft.ai = [], [], [], None
            return await self._start(draft)
        await self.store.save(draft)
        return Reply(f"Режим: {MODES[mode]}.\nОпишите задачу текстом или голосом, например {EXAMPLES[mode]}.",
                     keyboard=DRAFT_KEYBOARD)

    async def on_text(self, message: IncomingMessage, text: str) -> Reply:
        assert message.user_id is not None
        text = text.strip()
        draft = await self._draft(message.user_id, message.chat_id)
        if text.casefold().strip(".! ") in CANCEL_WORDS:
            return await self._cancel(draft)
        if draft.step in SLOT_QUESTIONS or draft.step == "ask":
            if draft.ai is not None:
                return await self._ai_answer(draft, text)
            return await self._answer(draft, text)
        # idle or summary: a new task replaces the old one
        if len(text) > MAX_TASK_CHARS:
            return Reply(f"Слишком длинная задача (больше {MAX_TASK_CHARS} символов). Сократите её.")
        draft = replace(draft.fresh(), task=text, message_id=message.message_id)
        if draft.mode is None:
            await self.store.save(draft)
            return mode_menu("Сначала выберите режим — задача сохранена:")
        return await self._start(draft)

    async def _answer(self, draft: Draft, text: str) -> Reply:
        """One typed reply may answer every question on screen ("Мадрид, аренда, до 1200 €")."""
        pending = draft.pending or [draft.step]
        skip = text.casefold().strip(".! ") in SKIP_WORDS
        filled: set[str] = set()
        places = find_places(text)
        if "city" in pending and len(places) == 1:
            draft.city, draft.place = places[0], None
            filled.add("city")
        elif pending == ["city"] and not skip and (typed := typed_place(text)) is not None:
            draft.city, draft.place = typed["en"], typed  # any place in the world, as typed
            filled.add("city")
        if "deal" in pending and (deal := parse_deal(text)) is not None:
            draft.deal = deal
            filled.add("deal")
        if "budget" in pending and not skip and (budget := parse_budget(text)) is not None:
            draft.budget = budget
            filled.add("budget")
        if "target" in pending and not skip and (targets(text) or pending == ["target"]):
            draft.target = text[:200]
            filled.add("target")
        if not filled:
            if not skip:
                self._reopen(draft, pending)
                return await self._advance(draft, prefix=NOT_UNDERSTOOD.get(pending[0], "Не понял ответ. "))
            # "Пропустить": the optional questions on screen are dropped; a missing city is asked again.
            return await self._advance(draft, prefix=NOT_UNDERSTOOD["city"] if "city" in pending else "")
        # Questions of the same message left unanswered come back.
        self._reopen(draft, [slot for slot in pending if slot not in filled])
        return await self._advance(draft)

    @staticmethod
    def _reopen(draft: Draft, slots: list[str]) -> None:
        draft.asked = [slot for slot in draft.asked if slot not in slots]

    async def on_button(self, user_id: int, chat_id: int, action: str, value: str, who: str | None = None) -> Reply:
        """``who`` (a label for owners) is given for the ``user`` role: owners hear about their launches."""
        if action == "launch":
            return await self._launch(user_id, who)
        draft = await self._draft(user_id, chat_id)
        if action == "cancel":
            return await self._cancel(draft)
        if action == "edit":
            await self.store.save(draft.fresh())
            return Reply("Хорошо. Опишите задачу заново" + (f", например {EXAMPLES[draft.mode]}." if draft.mode else "."))
        if action != draft.step:
            return Reply("Эта кнопка устарела.")
        if draft.ai is not None:
            return await self._ai_button(draft, action, value)
        if action == "deal":
            if value not in DEAL_TEXT:
                return Reply("Эта кнопка устарела.")
            draft.deal = value
        elif action == "budget":
            draft.budget = None  # skipped; "budget" is already in draft.asked
        else:
            return Reply("Эта кнопка устарела.")
        # The other questions of that message are still open.
        self._reopen(draft, [slot for slot in draft.pending if slot != action])
        return await self._advance(draft)

    # --- AI understanding -------------------------------------------------------------------

    async def _start(self, draft: Draft) -> Reply:
        """A new task: AI first, the deterministic rules when it is off or fails."""
        if await self._understand(draft):
            return await self._ai_advance(draft)
        return await self._advance(draft)

    async def _understand(self, draft: Draft) -> bool:
        """One AI call on the task and the answers so far; fills the draft. False = use the rules."""
        if self.understander is None or draft.mode not in MODES:
            draft.ai = None
            return False
        known = {"city": draft.city, "deal": draft.deal, "budget_max": draft.budget, "target": draft.target}
        try:
            ai = await self.understander.understand(mode=draft.mode, task=draft.task, answers=draft.answers, known=known)
        except Exception as exc:  # noqa: BLE001 - any failure falls back to the deterministic rules
            log.warning("telegram.intake.understanding_failed",
                        extra={"user_id": draft.user_id, "error": getattr(exc, "code", type(exc).__name__),
                               "status": getattr(exc, "status", None)})
            draft.ai = None
            return False
        if not isinstance(ai, TaskUnderstanding) or not ai.summary_ru:
            log.warning("telegram.intake.understanding_failed", extra={"user_id": draft.user_id, "error": "empty"})
            draft.ai = None
            return False
        draft.ai = ai.payload()
        # A newer answer may correct a field; a field the AI does not know keeps what was chosen.
        if ai.city:
            draft.city, draft.place = ai.city, ai.place
        draft.deal = (ai.deal or draft.deal) if draft.mode == "real_estate" else None
        draft.budget = ai.budget_max or draft.budget
        if draft.mode == "investors":
            draft.target = ai.target or draft.target
        log.info("telegram.intake.understood", extra={"user_id": draft.user_id, "questions": len(ai.questions),
                                                      "primary": len(ai.primary), "secondary": len(ai.secondary)})
        return True

    def _ai_missing(self, draft: Draft) -> list[str]:
        """Critical slots the code insists on, whatever the AI asked."""
        missing = ["city"] if draft.city is None else []
        if draft.mode == "real_estate" and draft.deal is None and "deal" not in draft.asked:
            missing.append("deal")
        if draft.mode == "investors" and draft.target is None and "target" not in draft.asked:
            missing.append("target")
        return missing

    async def _ai_answer(self, draft: Draft, text: str) -> Reply:
        was_missing_city = draft.city is None
        places = find_places(text)
        if draft.city is None and len(places) == 1:  # a plain city name needs no model to be read
            draft.city, draft.place = places[0], None
        self._record_answer(draft, text[:MAX_ANSWER_CHARS])
        if not await self._understand(draft):
            return await self._fallback_answer(draft, text)
        prefix = NOT_UNDERSTOOD["city"] if was_missing_city and draft.city is None else ""
        return await self._ai_advance(draft, prefix)

    @staticmethod
    def _record_answer(draft: Draft, text: str) -> None:
        draft.answers = [*draft.answers, text][-MAX_ANSWERS:]

    async def _fallback_answer(self, draft: Draft, text: str) -> Reply:
        """The AI failed mid-dialogue: the rules read this answer and carry on."""
        draft.ai, draft.answers = None, []
        draft.pending = ["city", "target"] if draft.mode == "investors" else ["city", "deal", "budget"]
        draft.step = draft.pending[0]
        return await self._answer(draft, text)

    async def _ai_button(self, draft: Draft, action: str, value: str) -> Reply:
        if action == "deal":
            if value not in DEAL_TEXT:
                return Reply("Эта кнопка устарела.")
            draft.deal = value
            answer = f"Сделка: {DEAL_TEXT[value]}"
        elif action == "ask" and value == "skip":
            # The optional questions are dropped; the summary the AI already wrote is shown.
            ai = self._ai(draft)
            ai.questions = []
            draft.ai = ai.payload()
            draft.answers = [*draft.answers, "пропустить"][-MAX_ANSWERS:]
            return await self._ai_advance(draft)
        else:
            return Reply("Эта кнопка устарела.")
        self._record_answer(draft, answer)
        if not await self._understand(draft):
            draft.ai, draft.answers = None, []
            return await self._advance(draft)
        return await self._ai_advance(draft)

    @staticmethod
    def _ai(draft: Draft) -> TaskUnderstanding:
        ai = draft.understanding()
        if ai is None:
            raise UnderstandingError("no_understanding")
        return ai

    async def _ai_advance(self, draft: Draft, prefix: str = "") -> Reply:
        """Ask what is missing (the AI's questions plus the critical ones), else show its summary."""
        ai = self._ai(draft)
        missing = self._ai_missing(draft)
        own = ai.questions if len(draft.answers) < AI_QUESTION_ROUNDS else []
        # The city question stays first so the cap below never drops it.
        own = sorted(own, key=lambda q: not ("city" in missing and any(w in q.casefold() for w in _ASKS_ABOUT["city"])))
        standard = [slot for slot in missing
                    if not any(word in q.casefold() for q in own for word in _ASKS_ABOUT[slot])]
        texts = ([SLOT_QUESTIONS[slot] for slot in standard] + own)[:MAX_QUESTIONS]
        if texts:
            first = missing[0] if missing and missing[0] in ("city", "deal") else "ask"
            draft.step, draft.pending = first, [*missing, *(["ask"] if own else [])]
            draft.asked += [slot for slot in missing if slot not in draft.asked]
            await self.store.save(draft)
            if first == "city":
                options = ()  # any place in the world: typed, never picked from a list
            elif first == "deal":
                options = (("Аренда", "rent"), ("Покупка", "sale"), ("Не важно", "any"))
            else:
                options = (("Пропустить", "skip"),)
            if len(texts) == 1:
                text = texts[0] + (SLOT_HINTS["city"] if first == "city" else "")
            else:
                text = "\n".join(["Уточните, пожалуйста:", *(f"{n}. {q}" for n, q in enumerate(texts, 1)),
                                  "Можно ответить одним сообщением."])
            buttons = tuple(Button(label, callback_data=f"task:{first}:{value}") for label, value in options)
            return Reply(prefix + text, buttons, keyboard=None if buttons else DRAFT_KEYBOARD)
        try:
            plan = draft.plan()
        except InvalidGoal as exc:
            await self.store.save(draft.fresh())
            return Reply(f"{prefix}{exc}\nОпишите задачу иначе, например {EXAMPLES.get(draft.mode or '', EXAMPLES['real_estate'])}.")
        draft.step, draft.pending = "summary", []
        await self.store.save(draft)
        model = getattr(self.understander, "model", "")
        reply = ai_summary(draft, ai, plan, self.technical(draft.user_id), model)
        return replace(reply, text=prefix + reply.text)

    async def _cancel(self, draft: Draft) -> Reply:
        await self.store.save(draft.fresh())
        return Reply("Черновик удалён. Опишите новую задачу, когда будете готовы.", keyboard=IDLE_KEYBOARD)

    async def _advance(self, draft: Draft, prefix: str = "") -> Reply:
        try:
            question = self.clarifier(draft)
            plan = None if question else draft.plan()
        except InvalidGoal as exc:
            await self.store.save(draft.fresh())
            return Reply(f"{prefix}{exc}\nОпишите задачу иначе, например {EXAMPLES.get(draft.mode or '', EXAMPLES['real_estate'])}.")
        if question is not None:
            slots = [question.slot, *question.also]
            draft.step, draft.pending = question.slot, slots
            draft.asked += [slot for slot in slots if slot not in draft.asked]
            await self.store.save(draft)
            buttons = tuple(Button(text, callback_data=f"task:{question.slot}:{value}") for text, value in question.options)
            return Reply(prefix + question.text, buttons, keyboard=None if buttons else DRAFT_KEYBOARD)
        assert plan is not None
        draft.step, draft.pending = "summary", []
        await self.store.save(draft)
        reply = summary(draft, plan, self.technical(draft.user_id))
        return replace(reply, text=prefix + reply.text)

    async def _launch(self, user_id: int, who: str | None) -> Reply:
        current = await self.store.get(user_id)
        if current is not None and current.step != "idle" and current.expired(self.now()):
            await self.store.save(current.fresh())
            return Reply("Черновик устарел, опишите задачу заново.")
        draft = await self.store.launch(user_id)
        if draft is None:
            return Reply("Эта задача уже запущена или устарела. Опишите новую задачу.")
        try:
            plan = draft.plan()
        except InvalidGoal as exc:
            return Reply(f"{exc}\nОпишите задачу заново.")
        draft.city = plan.location  # pin the city the summary showed, detected or chosen
        try:
            receipt = await self.sink(CommandEnvelope("campaign", draft.command_arguments(), draft.chat_id, user_id, draft.message_id))
        except Exception:
            log.exception("telegram.intake.enqueue_failed", extra={"user_id": user_id})
            await self.store.save(draft)  # back to the summary: the person may press again
            return Reply("Не удалось запустить поиск. Попробуйте нажать «Запустить» ещё раз чуть позже.")
        command_id = getattr(receipt, "command_id", None)
        log.info("telegram.intake.launched", extra={"user_id": user_id, "chat_id": draft.chat_id, "command_id": command_id})
        if getattr(receipt, "duplicate", False):
            return Reply("Эта задача уже запущена.")
        if who is not None and self.notify_owners is not None:
            try:
                await self.notify_owners(f"Пользователь {who} запустил кампанию: {plan.goal}")
            except Exception:  # noqa: BLE001 - the launch stands even if the notice fails
                log.warning("telegram.intake.owner_notice_failed", extra={"user_id": user_id})
        if self.technical(user_id):
            return Reply(f"{LAUNCHED}\nQueue id: {command_id}. Статус: /campaign status. Остановить: /campaign cancel <id>.",
                         keyboard=SEARCH_KEYBOARD)
        return Reply(f"{LAUNCHED}\n{STOP_HINT}", keyboard=SEARCH_KEYBOARD)
