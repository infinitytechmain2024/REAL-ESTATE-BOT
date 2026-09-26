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

The default clarifier is deterministic (``plan_campaign`` and its gazetteer);
an LLM clarifier can replace it later as any ``Clarifier`` callable. The
Orchestra still re-plans the goal and applies its quotas and breakers.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from bot.campaign.architect import (
    GAZETTEER,
    MAX_TEXT_CHARS,
    InvalidGoal,
    find_places,
    plan_campaign,
)
from bot.campaign.models import CampaignPlan
from bot.control_plane.models import Button, CommandEnvelope, IncomingMessage, Reply

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

    def copy(self, **changes: Any) -> Draft:
        return replace(self, asked=list(self.asked), options=list(self.options), pending=list(self.pending), **changes)

    def goal_text(self) -> str:
        """The task plus the answers given in words (who, deal, budget)."""
        parts = [self.task]
        if self.target:
            parts.append(self.target)
        if self.deal in ("rent", "sale"):
            parts.append(DEAL_TEXT[self.deal])
        if self.budget:
            parts.append(f"до {self.budget} €")
        return " ".join(p for p in parts if p)

    def plan(self) -> CampaignPlan:
        """What the Orchestra will plan; raises ``InvalidGoal``."""
        return plan_campaign(self.goal_text(), vertical=self.mode, location=self.city)  # type: ignore[arg-type]

    def command_arguments(self) -> str:
        """``mode=<vertical> [city=<name>] <goal>``: the choices travel with the queued command."""
        tokens = [f"mode={self.mode}"] if self.mode else []
        if self.city:
            tokens.append(f"city={self.city}")
        return " ".join([*tokens, self.goal_text()])

    def fresh(self) -> Draft:
        """Same person, chat and mode; no task."""
        return Draft(self.user_id, self.chat_id, self.mode)

    def payload(self) -> dict[str, Any]:
        return {"task": self.task, "message_id": self.message_id, "city": self.city, "deal": self.deal,
                "budget": self.budget, "asked": self.asked, "options": self.options, "target": self.target,
                "pending": self.pending}

    @classmethod
    def load(cls, user_id: int, chat_id: int, mode: str | None, step: str, payload: dict[str, Any],
             updated_at: datetime | None = None) -> Draft:
        return cls(user_id, chat_id, mode, step, str(payload.get("task") or ""), int(payload.get("message_id") or 0),
                   payload.get("city"), payload.get("deal"), payload.get("budget"),
                   list(payload.get("asked") or []), list(payload.get("options") or []), updated_at,
                   payload.get("target"), list(payload.get("pending") or []))

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
SLOT_HINTS = {"city": " Выберите или напишите.", "budget": " Или нажмите «Пропустить».", "target": ""}


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
    # Without a city the planner cannot run; any gazetteer city stands in to read deal and budget.
    location = draft.city or (GAZETTEER[0].canonical if city_missing else None)
    plan = plan_campaign(draft.goal_text(), vertical=draft.mode, location=location)  # type: ignore[arg-type]
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
            indexes = [i for i, p in enumerate(GAZETTEER) if p.canonical in places]
            names = ", ".join(GAZETTEER[i].aliases["ru"] for i in indexes)
            city_text = f"Указано несколько городов ({names}). Одна задача — один город. Какой выбрать?"
            options = tuple((GAZETTEER[i].aliases["ru"], str(i)) for i in indexes)
        else:
            options = tuple((p.aliases["ru"], str(i)) for i, p in enumerate(GAZETTEER))
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


def _cancel_button() -> Button:
    return Button("Отмена", callback_data="task:cancel")


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


def _city_ru(canonical: str) -> str:
    return next((p.aliases["ru"] for p in GAZETTEER if p.canonical == canonical), canonical)


def summary(draft: Draft, plan: CampaignPlan, technical: bool = False) -> Reply:
    """«Проверьте задачу»: what was understood, in Russian; owners also get the planner's details."""
    lines = [
        "Проверьте задачу:",
        f"Режим: {MODES[draft.mode] if draft.mode in MODES else MODES.get(plan.vertical, plan.vertical)}",
        f"Город: {_city_ru(plan.location)}",
    ]
    if plan.vertical == "investors":
        found = targets(draft.goal_text())
        lines.append(f"Кого ищем: {', '.join(found) if found else 'инвесторы и компании'}")
    else:
        deal = plan.constraints.get("deal")
        lines.append(f"Сделка: {DEAL_TEXT.get(str(deal), 'не важно')}")
        price = plan.constraints.get("max_price")
        lines.append(f"Бюджет: {f'до {price} €' if price else 'не указан'}")
    if technical:
        limits = plan.limits
        lines += [
            f"Цель: {plan.goal}",
            f"Языки поиска: {', '.join(lang.upper() for lang in plan.languages)}",
            f"Группы: до {limits.max_groups}, окнами по {limits.window_size}",
        ]
    lines += ["", "Всё верно? Нажмите «Запустить», чтобы начать поиск."]
    return Reply("\n".join(lines), (
        Button("Запустить", callback_data="task:launch"),
        Button("Изменить", callback_data="task:edit"),
        _cancel_button(),
    ))


LAUNCHED = "Принято. Начинаю поиск. Найденные варианты пришлю сюда."
STOP_HINT = "Чтобы остановить поиск, напишите «стоп»."
NOT_UNDERSTOOD = {"city": "Не понял город. ", "budget": "Не понял сумму. "}


class TaskIntake:
    def __init__(self, store: IntakeStore, sink: CommandSink, clarifier: Clarifier = deterministic_clarifier,
                 notify_owners: OwnerNotice | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 technical: Callable[[int], bool] = lambda _user_id: False) -> None:
        """``technical(user_id)`` says who also sees planner details and queue ids (the owner)."""
        self.store, self.sink, self.clarifier = store, sink, clarifier
        self.notify_owners, self.now, self.technical = notify_owners, now, technical

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
            draft.asked, draft.pending = [], []
            return await self._advance(draft)
        await self.store.save(draft)
        return Reply(f"Режим: {MODES[mode]}.\nОпишите задачу текстом или голосом, например {EXAMPLES[mode]}.")

    async def on_text(self, message: IncomingMessage, text: str) -> Reply:
        assert message.user_id is not None
        text = text.strip()
        draft = await self._draft(message.user_id, message.chat_id)
        if text.casefold().strip(".! ") in CANCEL_WORDS:
            return await self._cancel(draft)
        if draft.step in SLOT_QUESTIONS:
            return await self._answer(draft, text)
        # idle or summary: a new task replaces the old one
        if len(text) > MAX_TASK_CHARS:
            return Reply(f"Слишком длинная задача (больше {MAX_TASK_CHARS} символов). Сократите её.")
        draft = replace(draft.fresh(), task=text, message_id=message.message_id)
        if draft.mode is None:
            await self.store.save(draft)
            return mode_menu("Сначала выберите режим — задача сохранена:")
        return await self._advance(draft)

    async def _answer(self, draft: Draft, text: str) -> Reply:
        """One typed reply may answer every question on screen ("Мадрид, аренда, до 1200 €")."""
        pending = draft.pending or [draft.step]
        skip = text.casefold().strip(".! ") in SKIP_WORDS
        filled: set[str] = set()
        places = find_places(text)
        if "city" in pending and len(places) == 1:
            draft.city = places[0]
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
        if action == "city":
            if not value.isdigit() or int(value) >= len(GAZETTEER):
                return Reply("Эта кнопка устарела.")
            draft.city = GAZETTEER[int(value)].canonical
        elif action == "deal":
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

    async def _cancel(self, draft: Draft) -> Reply:
        await self.store.save(draft.fresh())
        return Reply("Черновик удалён. Опишите новую задачу, когда будете готовы.")

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
            return Reply(prefix + question.text, (*buttons, _cancel_button()))
        assert plan is not None
        draft.step, draft.pending = "summary", []
        await self.store.save(draft)
        reply = summary(draft, plan, self.technical(draft.user_id))
        return Reply(prefix + reply.text, reply.buttons)

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
            return Reply(f"{LAUNCHED}\nQueue id: {command_id}. Статус: /campaign status. Остановить: /campaign cancel <id>.")
        return Reply(f"{LAUNCHED}\n{STOP_HINT}")
