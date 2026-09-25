"""Task intake for people with the ``user`` role (also open to operators and owners).

The person picks a mode like the old bot did (🏡 objects -> ``real_estate``,
💼 investors -> ``investors``), then writes or says the task. A clarifier
asks what the planner still needs -- the city (required), rent or purchase,
an optional budget -- at most three questions per task, with buttons and
typed answers alike. Then comes a short summary with [Запустить] [Изменить]
[Отмена]; only "Запустить" queues an Orchestra ``campaign`` command, and the
draft is reset in the same statement, so a double tap queues one campaign.

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
from bot.models.enums import Mode

log = logging.getLogger(__name__)
CommandSink = Callable[[CommandEnvelope], Awaitable[object]]
OwnerNotice = Callable[[str], Awaitable[None]]

# mode (the plan's vertical) -> button title from the old bot
MODES: dict[str, str] = {
    "real_estate": Mode.LAND.title,
    "investors": Mode.INVESTORS.title,
}
DRAFT_TTL = timedelta(hours=24)
MAX_QUESTIONS = 3
MAX_TASK_CHARS = MAX_TEXT_CHARS - 100  # room for the mode word and the answers
CANCEL_WORDS = frozenset({"отмена", "отменить", "cancel", "скасувати"})
SKIP_WORDS = frozenset({"пропустить", "пропуск", "нет", "не важно", "неважно", "skip", "no", "-"})
DEAL_TEXT = {"rent": "аренда", "sale": "покупка", "any": "не важно"}
EXAMPLES = {"real_estate": "«квартиры в аренду в Мадриде до 1200 €»", "investors": "«инвесторы для стартапа в Барселоне»"}
_NUMBER = re.compile(r"(\d{1,3}(?:[ .,]\d{3})+|\d+(?:[.,]\d+)?)\s*(k|к|тыс\w*|тис\w*)?", re.IGNORECASE)


@dataclass(slots=True)
class Draft:
    user_id: int
    chat_id: int
    mode: str | None = None
    step: str = "idle"  # idle | city | deal | budget | summary
    task: str = ""
    message_id: int = 0  # the task's Telegram message: the Orchestra idempotency key
    city: str | None = None
    deal: str | None = None  # rent | sale | any
    budget: int | None = None
    asked: list[str] = field(default_factory=list)  # slots asked about, in order
    options: list[str] = field(default_factory=list)  # cities offered when the task named several
    updated_at: datetime | None = None

    def goal_text(self) -> str:
        """The task plus the answers given in words (deal, budget)."""
        parts = [self.task]
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
                "budget": self.budget, "asked": self.asked, "options": self.options}

    @classmethod
    def load(cls, user_id: int, chat_id: int, mode: str | None, step: str, payload: dict[str, Any],
             updated_at: datetime | None = None) -> Draft:
        return cls(user_id, chat_id, mode, step, str(payload.get("task") or ""), int(payload.get("message_id") or 0),
                   payload.get("city"), payload.get("deal"), payload.get("budget"),
                   list(payload.get("asked") or []), list(payload.get("options") or []), updated_at)

    def expired(self, now: datetime) -> bool:
        return self.updated_at is not None and now - self.updated_at > DRAFT_TTL


@dataclass(frozen=True, slots=True)
class Question:
    slot: str  # city | deal | budget
    text: str
    options: tuple[tuple[str, str], ...] = ()  # (button text, value)


Clarifier = Callable[[Draft], Question | None]


def deterministic_clarifier(draft: Draft) -> Question | None:
    """The next thing to ask, or None when the task is ready; raises ``InvalidGoal``."""
    if draft.city is None:
        places = find_places(draft.task)
        if len(places) > 1:
            indexes = [i for i, p in enumerate(GAZETTEER) if p.canonical in places]
            return Question("city", f"Указано несколько городов ({', '.join(places)}). Одна задача — один город. Какой выбрать?",
                            tuple((GAZETTEER[i].aliases["ru"], str(i)) for i in indexes))
        if not places:
            return Question("city", "В каком городе искать? Выберите или напишите.",
                            tuple((p.aliases["ru"], str(i)) for i, p in enumerate(GAZETTEER)))
    plan = draft.plan()

    def may_ask(slot: str) -> bool:
        return slot not in draft.asked and len(draft.asked) < MAX_QUESTIONS

    if plan.vertical != "investors" and plan.constraints.get("deal") is None and draft.deal is None and may_ask("deal"):
        return Question("deal", "Аренда или покупка?", (("Аренда", "rent"), ("Покупка", "sale"), ("Не важно", "any")))
    if plan.vertical != "investors" and plan.constraints.get("max_price") is None and may_ask("budget"):
        return Question("budget", "Бюджет? (например, до 1200 €) или Пропустить", (("Пропустить", "skip"),))
    return None


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
        return replace(draft, asked=list(draft.asked), options=list(draft.options)) if draft else None

    async def save(self, draft: Draft) -> None:
        self.drafts[draft.user_id] = replace(draft, asked=list(draft.asked), options=list(draft.options),
                                             updated_at=datetime.now(UTC))

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


def summary(draft: Draft, plan: CampaignPlan) -> Reply:
    limits = plan.limits
    lines = [
        "Проверьте задачу:",
        f"Цель: {plan.goal}",
        f"Режим: {MODES[draft.mode] if draft.mode else plan.vertical}",
        f"Город: {plan.location}",
    ]
    if plan.vertical != "investors":
        deal = plan.constraints.get("deal")
        lines.append(f"Сделка: {DEAL_TEXT.get(str(deal), 'не важно')}")
        price = plan.constraints.get("max_price")
        lines.append(f"Бюджет: {f'до {price} €' if price else 'не указан'}")
    lines += [
        f"Языки поиска: {', '.join(lang.upper() for lang in plan.languages)}",
        f"Группы: до {limits.max_groups}, окнами по {limits.window_size}",
        "",
        "Нажмите «Запустить», чтобы начать поиск.",
    ]
    return Reply("\n".join(lines), (
        Button("Запустить", callback_data="task:launch"),
        Button("Изменить", callback_data="task:edit"),
        _cancel_button(),
    ))


class TaskIntake:
    def __init__(self, store: IntakeStore, sink: CommandSink, clarifier: Clarifier = deterministic_clarifier,
                 notify_owners: OwnerNotice | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.store, self.sink, self.clarifier = store, sink, clarifier
        self.notify_owners, self.now = notify_owners, now

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

    async def choose_mode(self, user_id: int, chat_id: int, mode: str) -> Reply:
        if mode not in MODES:
            return Reply("Эта кнопка устарела.")
        draft = await self._draft(user_id, chat_id)
        draft.mode = mode
        if draft.task:  # a task written before the mode was chosen, or a mode switch mid-task
            return await self._advance(draft, prefix=f"Режим: {MODES[mode]}.\n")
        await self.store.save(draft)
        return Reply(f"Режим: {MODES[mode]}.\nОпишите задачу текстом или голосом, например {EXAMPLES[mode]}.")

    async def on_text(self, message: IncomingMessage, text: str) -> Reply:
        assert message.user_id is not None
        text = text.strip()
        draft = await self._draft(message.user_id, message.chat_id)
        if text.casefold().strip(".! ") in CANCEL_WORDS:
            return await self._cancel(draft)
        if draft.step == "city":
            places = find_places(text)
            if len(places) != 1:
                return await self._advance(draft, prefix="Не понял город. ")
            draft.city = places[0]
        elif draft.step == "deal":
            deal = parse_deal(text)
            if deal is None:
                return await self._advance(draft, prefix="Не понял ответ. ")
            draft.deal = deal
        elif draft.step == "budget":
            if text.casefold().strip(".! ") not in SKIP_WORDS:
                budget = parse_budget(text)
                if budget is None:
                    return await self._advance(draft, prefix="Не понял сумму. ")
                draft.budget = budget
        else:  # idle or summary: a new task replaces the old one
            if len(text) > MAX_TASK_CHARS:
                return Reply(f"Слишком длинная задача (больше {MAX_TASK_CHARS} символов). Сократите её.")
            draft = replace(draft.fresh(), task=text, message_id=message.message_id)
            if draft.mode is None:
                await self.store.save(draft)
                return mode_menu("Сначала выберите режим — задача сохранена:")
        return await self._advance(draft)

    async def on_button(self, user_id: int, chat_id: int, action: str, value: str, who: str | None = None) -> Reply:
        """``who`` (a label for owners) is given for the ``user`` role: owners hear about their launches."""
        if action == "launch":
            return await self._launch(user_id, who)
        draft = await self._draft(user_id, chat_id)
        if action == "cancel":
            return await self._cancel(draft)
        if action == "edit":
            await self.store.save(draft.fresh())
            return Reply("Опишите задачу заново" + (f", например {EXAMPLES[draft.mode]}." if draft.mode else "."))
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
            draft.step = question.slot
            if question.slot not in draft.asked:
                draft.asked.append(question.slot)
            await self.store.save(draft)
            buttons = tuple(Button(text, callback_data=f"task:{question.slot}:{value}") for text, value in question.options)
            return Reply(prefix + question.text, (*buttons, _cancel_button()))
        assert plan is not None
        draft.step = "summary"
        await self.store.save(draft)
        reply = summary(draft, plan)
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
            return Reply("Не удалось поставить задачу в очередь. Попробуйте нажать «Запустить» ещё раз чуть позже.")
        log.info("telegram.intake.launched", extra={"user_id": user_id, "chat_id": draft.chat_id,
                                                     "command_id": getattr(receipt, "command_id", None)})
        if getattr(receipt, "duplicate", False):
            return Reply("Эта задача уже в очереди.")
        if who is not None and self.notify_owners is not None:
            try:
                await self.notify_owners(f"Пользователь {who} запустил кампанию: {plan.goal}")
            except Exception:  # noqa: BLE001 - the launch stands even if the notice fails
                log.warning("telegram.intake.owner_notice_failed", extra={"user_id": user_id})
        return Reply("Задача принята и поставлена в очередь. Найденное будет приходить сюда.\n"
                     "Статус: /campaign status. Остановить: /campaign cancel <id>.")
