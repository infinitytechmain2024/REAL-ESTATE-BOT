"""Task intake for people with the ``user`` role (also open to operators and owners).

The person picks a mode like the old bot did (🏡 objects -> ``real_estate``, 💼 investors -> ``investors``),
then writes or says the task. The bot then interviews them like a good agent ("grill me") until the task is
fully specified: every user message goes to the interviewer (``interviewer.py``, one AI call; the rules in
``rules.py`` take over when there is no key or a call fails), which fills a ``TaskSpec``
(``bot/campaign/spec.py``) and asks exactly ONE next question: the most important missing hard field first.
The interview has three parts (docs/INTERVIEW_TREE.md): the hard fields, then a round of questions specific to
THIS task (floor and lift for a flat, buildable class for land, stage and raise for investors ...), then always
one deviation question («если точных вариантов не будет, что допустимо?»); even a fully described task is asked
the last two before the card appears, unless «Хватит, ищи» is pressed. Each question carries «Не важно» (the asked field is left open on purpose),
«Хватит, ищи» (stop asking) and «Отмена»; after ``MAX_ROUNDS`` questions the interview ends anyway. The place
is the one thing that cannot be skipped: a search needs somewhere to look.

Then comes «Проверьте задачу»: the structured card (``TaskSpec.summary_ru``) with «Запустить» «Изменить»
«Отмена». «Изменить» lists the fields; changing one re-runs the interviewer with "<field>: <value>" and
returns to the card, the rest of the task stays. Typing on the card is a correction, not a new task. Only
«Запустить» queues an Orchestra ``campaign`` command, and the draft is reset in the same statement, so a
double tap queues one campaign.

The command keeps the free-text goal for the planner (``goal_text``) and carries the confirmed requirements as
leading tokens: ``mode=<vertical> city=<name>|place=<names> spec=<TaskSpec JSON>``; the Orchestra plans from
the spec (constraints) and stores it on the campaign. A voice task is shown back once: «Я услышал: «…»».
Owners also see the technical details: the planner's goal line, search languages, group windows, queue id.
A draft untouched for 24 hours is treated as gone. Owners get a one-line notice when a user launches.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import ValidationError

from bot.campaign.architect import MAX_TEXT_CHARS, InvalidGoal, find_places, plan_campaign
from bot.campaign.models import CampaignPlan
from bot.campaign.spec import DEAL_RU, INVESTOR_WHO_RU, MODE_TITLES, PROPERTY_RU, TaskSpec
from bot.control_plane.interviewer import Interviewer, InterviewTurn
from bot.control_plane.models import Button, CommandEnvelope, IncomingMessage, Reply
from bot.control_plane.rules import (
    AI_SKIP_WORDS,
    DEVIATION_OPTIONS,
    DEVIATION_PATH,
    SKIP_WORDS,
    TASK_PATH,
    RuleInterviewer,
    gazetteer_place,
    geo_ru,
    options_for,
    parse_budget,
    parse_deal,
    question_for,
    read_deviations,
)

__all__ = ["geo_ru", "parse_budget", "parse_deal"]  # re-exported: the answer parsers live in rules.py

log = logging.getLogger(__name__)
CommandSink = Callable[[CommandEnvelope], Awaitable[object]]
OwnerNotice = Callable[[str], Awaitable[None]]

# mode (the plan's vertical) -> button title from the old bot
MODES: dict[str, str] = dict(MODE_TITLES)
DRAFT_TTL = timedelta(hours=24)
MAX_ROUNDS = 14  # questions per task; after that the card is shown with what is known (setting INTERVIEW_MAX_ROUNDS)
MAX_TASK_CHARS = MAX_TEXT_CHARS - 100  # room for the mode word and the answers
MAX_DIALOGUE = 40
MAX_HEARD_CHARS = 300
CANCEL_WORDS = frozenset({"отмена", "отменить", "cancel", "скасувати"})
ENOUGH_WORDS = frozenset({"хватит", "хватит, ищи", "хватит ищи", "ищи", "достаточно", "хватит вопросов", "enough"})
DEAL_TEXT = dict(DEAL_RU)
EXAMPLES = {"real_estate": "«квартиры в аренду в Мадриде до 1200 €»", "investors": "«инвесторы для стартапа в Барселоне»"}
# «Изменить»: callback code -> (button title, the TaskSpec field the interviewer is pointed at)
FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    "real_estate": {
        "place": ("Место", "place"), "deal": ("Сделка", "deal"), "type": ("Тип", "property_type"),
        "budget": ("Бюджет", "budget.max"), "rooms": ("Комнаты", "rooms.min"), "area": ("Площадь", "area_m2.min"),
        "districts": ("Районы", "place.districts"), "must_have": ("Обязательно", "must_have"),
        "wishes": ("Пожелания", "wishes"), "exclude": ("Исключить", "exclude"), "sources": ("Источники", "sources.required"),
    },
    "investors": {
        "who": ("Кого ищем", "investor.who"), "ticket": ("Тикет", "investor.ticket"),
        "role": ("Роль", "investor.user_role"), "geo": ("География", "investor.geography"),
        "place": ("Место", "place"), "must_have": ("Обязательно", "must_have"),
        "wishes": ("Пожелания", "wishes"), "exclude": ("Исключить", "exclude"),
    },
}
_WORD = re.compile(r"\w+")


@dataclass(slots=True)
class Draft:
    user_id: int
    chat_id: int
    mode: str | None = None
    step: str = "idle"  # idle | ask (a question or a field edit is on screen) | summary (the card is on screen)
    task: str = ""  # the first message of the task
    message_id: int = 0  # the task's Telegram message: the Orchestra idempotency key
    updated_at: datetime | None = None
    spec: TaskSpec | None = None  # what is known so far
    dialogue: list[dict[str, str]] = field(default_factory=list)  # {"role": "user"|"assistant", "text"}
    rounds: int = 0  # questions asked so far
    asking: str | None = None  # the spec field of the question on screen
    editing: str | None = None  # the spec field being changed from «Изменить»
    forced: bool = False  # «Хватит, ищи» was pressed: no more questions for this task (except a missing place)
    choices: list[str] = field(default_factory=list)  # the model's suggested answers on screen (buttons «task:opt:<i>»)

    def copy(self, **changes: Any) -> Draft:
        return replace(self, dialogue=[dict(t) for t in self.dialogue], choices=list(self.choices),
                       spec=self.spec.model_copy(deep=True) if self.spec is not None else None, **changes)

    def goal_text(self) -> str:
        """The task in words for the planner and the owner: the deal and budget first (the planner reads the
        first price it finds), the type, the task, then the must-haves and the wishes, within the length limit."""
        spec = self.spec or TaskSpec()
        head: list[str] = []
        if self.mode == "investors":
            if who := ", ".join(INVESTOR_WHO_RU.get(w, w) for w in spec.investor.who):
                head.append(f"Кого ищем: {who}.")
        else:
            if spec.deal in ("rent", "sale"):
                head.append(DEAL_TEXT[spec.deal])
            if spec.budget.max:
                currency = spec.budget.currency or "EUR"
                amount = int(spec.budget.max)
                head.append(f"до {amount} €" if currency == "EUR" else f"бюджет {amount} {currency}")
            if spec.property_type not in (None, "any", "other"):
                head.append(f"Тип: {PROPERTY_RU[spec.property_type]}.")  # type: ignore[index]
        tail: list[str] = []
        if spec.must_have:
            tail.append("Главное: " + "; ".join(spec.must_have) + ".")
        if spec.wishes:
            tail.append("Дополнительно: " + "; ".join(w.text for w in spec.wishes) + ".")
        head_text, tail_text = " ".join(head), " ".join(tail)
        room = MAX_TEXT_CHARS - len(head_text) - len(tail_text) - 12
        task = self.task if len(self.task) <= room else self.task[:max(room, 0)].rstrip() + "…"
        return " ".join(p for p in (head_text, f"Задача: {task}" if task else "", tail_text) if p)[:MAX_TEXT_CHARS]

    def place_args(self) -> tuple[str | None, dict[str, Any] | None]:
        """(``location``, ``place``) for the planner: a well-known city by name, any other place with its names."""
        spec = self.spec
        name = spec.place_name() if spec else None
        if spec is None or name is None:
            return None, None
        if find_places(name) == [name]:
            return name, None
        place: dict[str, Any] = {"en": name, **spec.place.names}
        if spec.place.country:
            place["country"] = spec.place.country
        return None, place

    def plan(self) -> CampaignPlan:
        """What the Orchestra will plan; raises ``InvalidGoal``."""
        location, place = self.place_args()
        return plan_campaign(self.goal_text(), vertical=self.mode,  # type: ignore[arg-type]
                             location=location, place=place, spec=self.spec)

    def command_arguments(self) -> str:
        """``mode=<vertical> [place=<names>|city=<name>] [spec=<JSON>] <goal>``: the choices travel with the command."""
        tokens = [f"mode={self.mode}"] if self.mode else []
        location, place = self.place_args()
        if place:
            tokens.append(f"place={encode_place(place)}")
        elif location:
            tokens.append(f"city={location.replace(' ', '_')}")
        if self.spec is not None:
            tokens.append(f"spec={encode_spec(self.spec)}")
        return " ".join([*tokens, self.goal_text()])

    def fresh(self) -> Draft:
        """Same person, chat and mode; no task."""
        return Draft(self.user_id, self.chat_id, self.mode)

    def payload(self) -> dict[str, Any]:
        return {"task": self.task, "message_id": self.message_id, "dialogue": self.dialogue, "rounds": self.rounds,
                "asking": self.asking, "editing": self.editing, "forced": bool(self.forced),
                "choices": list(self.choices)}

    def spec_json(self) -> str | None:
        return self.spec.model_dump_json() if self.spec is not None else None

    @classmethod
    def load(cls, user_id: int, chat_id: int, mode: str | None, step: str, payload: dict[str, Any],
             updated_at: datetime | None = None, spec: dict[str, Any] | None = None) -> Draft:
        dialogue = [{"role": str(t.get("role")), "text": str(t.get("text"))}
                    for t in payload.get("dialogue") or [] if isinstance(t, dict)]
        try:
            loaded = TaskSpec.model_validate(spec) if isinstance(spec, dict) else None
        except ValidationError:
            log.warning("telegram.intake.draft_spec_corrupt", extra={"user_id": user_id})
            return cls(user_id, chat_id, mode)  # a fresh draft: the person describes the task again
        return cls(user_id, chat_id, mode, step, str(payload.get("task") or ""), int(payload.get("message_id") or 0),
                   updated_at, loaded, dialogue,
                   int(payload.get("rounds") or 0), payload.get("asking"), payload.get("editing"),
                   bool(payload.get("forced")),
                   [str(c) for c in payload.get("choices") or [] if isinstance(c, str)])

    def expired(self, now: datetime) -> bool:
        return self.updated_at is not None and now - self.updated_at > DRAFT_TTL


class IntakeStore(Protocol):
    async def get(self, user_id: int) -> Draft | None: ...
    async def save(self, draft: Draft) -> None: ...
    async def launch(self, user_id: int) -> Draft | None:
        """Atomically take a draft that reached the card and reset it; None if there is none."""
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
    """Shares the control plane's pool (migrations 018, 031, 035)."""

    def __init__(self, pool_owner: Any) -> None:
        self._owner = pool_owner

    def _pool(self) -> Any:
        return self._owner._pool()

    async def get(self, user_id: int) -> Draft | None:
        row = await self._pool().fetchrow(
            """select telegram_chat_id, mode, step, draft::text, updated_at, spec::text
                 from public.user_task_drafts where telegram_user_id = $1""", user_id)
        return Draft.load(user_id, row[0], row[1], row[2], json.loads(row[3]), row[4],
                          json.loads(row[5]) if row[5] else None) if row else None

    async def save(self, draft: Draft) -> None:
        await self._pool().execute(
            """insert into public.user_task_drafts (telegram_user_id, telegram_chat_id, mode, step, draft, spec)
               values ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
               on conflict (telegram_user_id) do update set telegram_chat_id = excluded.telegram_chat_id,
                 mode = excluded.mode, step = excluded.step, draft = excluded.draft, spec = excluded.spec,
                 updated_at = now()""",
            draft.user_id, draft.chat_id, draft.mode, draft.step, json.dumps(draft.payload(), ensure_ascii=False),
            draft.spec_json(),
        )

    async def launch(self, user_id: int) -> Draft | None:
        # The row lock makes a concurrent second tap see step = 'idle' and update nothing.
        row = await self._pool().fetchrow(
            """update public.user_task_drafts d
                  set step = 'idle', draft = '{}'::jsonb, spec = null, updated_at = now(), launched_at = now()
                 from (select telegram_user_id, draft, spec from public.user_task_drafts
                        where telegram_user_id = $1 and step = 'summary' for update) old
                where d.telegram_user_id = old.telegram_user_id and d.step = 'summary'
            returning d.telegram_chat_id, d.mode, old.draft::text, old.spec::text""",
            user_id,
        )
        return Draft.load(user_id, row[0], row[1], "summary", json.loads(row[2]), None,
                          json.loads(row[3]) if row[3] else None) if row else None


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


def encode_place(place: dict[str, Any]) -> str:
    """The place as one token of the queued command (URL-safe base64 of its JSON, no spaces)."""
    data = {k: v for k, v in place.items() if isinstance(v, str) and v}
    return base64.urlsafe_b64encode(json.dumps(data, ensure_ascii=False).encode()).decode().rstrip("=")


def encode_spec(spec: TaskSpec) -> str:
    """The spec as one token of the queued command (URL-safe base64 of its JSON without defaults)."""
    raw = spec.model_dump_json(exclude_defaults=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _norm(text: str) -> str:
    return text.casefold().strip(".! ")


LAUNCHED = "Принято. Начинаю поиск. Найденные варианты пришлю сюда."
STOP_HINT = "Чтобы остановить поиск, нажмите «Остановить поиск» внизу или напишите «стоп»."
STOP_CALLBACK = "search:stop"
STALE = "Эта кнопка устарела."


def stop_button() -> Button:
    return Button("Остановить поиск", callback_data=STOP_CALLBACK)


class TaskIntake:
    def __init__(self, store: IntakeStore, sink: CommandSink, notify_owners: OwnerNotice | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC),
                 technical: Callable[[int], bool] = lambda _user_id: False,
                 interviewer: Interviewer | None = None, max_rounds: int = MAX_ROUNDS) -> None:
        """``technical(user_id)`` says who also sees planner details and queue ids (the owner).

        ``interviewer`` (optional) reads every message with AI; without it, or when it fails, the rules of
        ``rules.py`` interview instead. ``max_rounds`` caps the questions per task.
        """
        self.store, self.sink = store, sink
        self.notify_owners, self.now, self.technical = notify_owners, now, technical
        self.interviewer = interviewer
        self.rules = RuleInterviewer()
        self.max_rounds = max_rounds

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
        """A task is being written (a question or the card is on screen) and has not expired."""
        draft = await self.store.get(user_id)
        return draft is not None and draft.step != "idle" and not draft.expired(self.now())

    async def choose_mode(self, user_id: int, chat_id: int, mode: str) -> Reply:
        if mode not in MODES:
            return Reply(STALE)
        draft = await self._draft(user_id, chat_id)
        draft.mode = mode
        if draft.task:  # a task written before the mode was chosen, or a mode switch mid-task
            task, message_id = draft.task, draft.message_id
            draft = replace(draft.fresh(), task=task, message_id=message_id)
            return await self._interview(draft, task)
        await self.store.save(draft)
        return Reply(f"Режим: {MODES[mode]}.\nОпишите задачу текстом или голосом, например {EXAMPLES[mode]}.",
                     keyboard=DRAFT_KEYBOARD)

    # --- text -------------------------------------------------------------------------------

    async def on_text(self, message: IncomingMessage, text: str) -> Reply:
        assert message.user_id is not None
        text = text.strip()
        draft = await self._draft(message.user_id, message.chat_id)
        if _norm(text) in CANCEL_WORDS:
            return await self._cancel(draft)
        reply = await self._on_text(draft, message, text)
        if message.transcript and not self.technical(message.user_id):
            heard = " ".join(message.transcript.split())
            heard = heard if len(heard) <= MAX_HEARD_CHARS else heard[:MAX_HEARD_CHARS].rstrip() + "…"
            reply = replace(reply, text=f"Я услышал: «{heard}»\n\n{reply.text}")
        return reply

    async def _on_text(self, draft: Draft, message: IncomingMessage, text: str) -> Reply:
        if draft.spec is not None and draft.step == "ask":
            word = _norm(text)
            if draft.editing:
                return await self._edit_value(draft, text)
            if word in ENOUGH_WORDS:
                return await self._enough(draft)
            if word in self._skip_words() and draft.asking and draft.asking != "place":
                return await self._skip(draft)
            return await self._interview(draft, text)
        if draft.spec is not None and draft.step == "summary":
            return await self._interview(draft, text)  # a correction typed under the card
        # idle (or a stale step from an older version): a new task
        if len(text) > MAX_TASK_CHARS:
            return Reply(f"Слишком длинная задача (больше {MAX_TASK_CHARS} символов). Сократите её.")
        draft = replace(draft.fresh(), task=text, message_id=message.message_id)
        if draft.mode is None:
            await self.store.save(draft)
            return mode_menu("Сначала выберите режим — задача сохранена:")
        return await self._interview(draft, text)

    # --- buttons ----------------------------------------------------------------------------

    async def on_button(self, user_id: int, chat_id: int, action: str, value: str, who: str | None = None) -> Reply:
        """``who`` (a label for owners) is given for the ``user`` role: owners hear about their launches."""
        if action == "launch":
            return await self._launch(user_id, who)
        draft = await self._draft(user_id, chat_id)
        if action == "cancel":
            return await self._cancel(draft)
        if draft.spec is None or draft.mode not in MODES:
            return Reply(STALE)
        if action == "edit":
            return self._edit_menu(draft)
        if action == "field":
            return await self._pick_field(draft, value)
        if action == "back":  # from the field list or an edit prompt: to the card if it was on screen, else the question
            was_card = draft.step == "summary" or draft.forced
            draft.editing = None
            return await self._card(draft) if was_card and draft.spec.place_name() else await self._carry_on(draft)
        if draft.step != "ask":
            return Reply(STALE)
        if action == "skip":
            return await self._skip(draft)
        if action == "enough":
            return await self._enough(draft)
        if action == "dev" and value in ("0", "10", "20") and draft.asking == DEVIATION_PATH:
            return await self._interview(draft, "Только точные" if value == "0" else f"±{value} %")
        if action == "opt" and value.isdigit() and int(value) < len(draft.choices):
            return await self._interview(draft, draft.choices[int(value)])
        if action == "deal" and value in ("rent", "sale") and draft.asking == "deal":
            return await self._interview(draft, f"Сделка: {DEAL_TEXT[value]}")
        if action == "role" and value in ("raising", "deploying") and draft.asking == "investor.user_role":
            return await self._interview(draft, "ищу деньги для проекта" if value == "raising" else "хочу вкладывать")
        return Reply(STALE)

    def _edit_menu(self, draft: Draft) -> Reply:
        fields = FIELDS[draft.mode or "real_estate"]
        buttons = tuple(Button(title, callback_data=f"task:field:{code}") for code, (title, _path) in fields.items())
        return Reply("Что изменить?", (*buttons, Button("Назад", callback_data="task:back")), keyboard=DRAFT_KEYBOARD)

    async def _pick_field(self, draft: Draft, code: str) -> Reply:
        entry = FIELDS[draft.mode or "real_estate"].get(code)
        if entry is None:
            return Reply(STALE)
        title, path = entry
        draft.step, draft.editing, draft.asking = "ask", path, path
        await self.store.save(draft)
        hint = question_for(draft.spec or TaskSpec(), path)
        return Reply(f"{title}: напишите новое значение. {hint}", (Button("Назад", callback_data="task:back"),),
                     keyboard=DRAFT_KEYBOARD)

    async def _edit_value(self, draft: Draft, text: str) -> Reply:
        """The new value of the field picked in «Изменить»: the interviewer applies it; the card comes back."""
        path = draft.editing or ""
        title = next((t for t, p in FIELDS[draft.mode or "real_estate"].values() if p == path), path)
        spec = draft.spec or TaskSpec(mode=draft.mode)  # type: ignore[arg-type]
        if _norm(text) in self._skip_words() and path != "place":
            draft.spec = _cleared(spec, path).mark_unspecified(path)
            text = "не важно"
        return await self._interview(draft, f"{title.lower()}: {text}", editing=True)

    def _skip_words(self) -> frozenset[str]:
        """What counts as «doesn't matter»: the explicit words with the AI interviewer, the wider set for the rules."""
        return AI_SKIP_WORDS if self.interviewer is not None else SKIP_WORDS

    async def _skip(self, draft: Draft) -> Reply:
        """«Не важно»: the asked field stays open on purpose; the interviewer asks the next one."""
        spec = draft.spec or TaskSpec(mode=draft.mode)  # type: ignore[arg-type]
        if draft.asking and draft.asking != "place":
            draft.spec = spec.mark_unspecified(draft.asking)
        return await self._interview(draft, "Не важно")

    async def _enough(self, draft: Draft) -> Reply:
        """«Хватит, ищи»: no more questions; only a missing place is still asked."""
        draft.forced = True
        return await self._carry_on(draft, force=True)

    # --- the interview ----------------------------------------------------------------------

    async def _interview(self, draft: Draft, message: str, *, editing: bool = False) -> Reply:
        """One user message: the interviewer updates the spec and says what to ask next, or the card is shown."""
        mode = draft.mode or "real_estate"
        spec = draft.spec or TaskSpec(mode=mode)  # type: ignore[arg-type]
        turn = await self._turn(draft, mode, spec, message, editing)
        draft.spec = _enrich(self._record_answer(draft, spec, turn.spec, message, editing), mode)
        draft.dialogue = [*draft.dialogue, {"role": "user", "text": message[:600]}][-MAX_DIALOGUE:]
        draft.editing = None
        return await self._carry_on(draft, turn, editing=editing)

    def _record_answer(self, draft: Draft, before: TaskSpec, after: TaskSpec, message: str, editing: bool) -> TaskSpec:
        """Safety net: the answer to a task-specific or deviation question is kept even if the model dropped it."""
        if editing or draft.asking not in (TASK_PATH, DEVIATION_PATH) or _norm(message) in self._skip_words():
            return after
        if draft.asking == DEVIATION_PATH:
            return after if after.deviations.asked else read_deviations(after, message)
        if len(after.context.answers) > len(before.context.answers):
            return after
        question = next((t["text"] for t in reversed(draft.dialogue) if t.get("role") == "assistant"), "")
        return after.merged({"context": {"answers": [{"question": question, "answer": message}]}})

    async def _turn(self, draft: Draft, mode: str, spec: TaskSpec, message: str, editing: bool) -> InterviewTurn:
        """One AI call; the rules answer instead when there is no interviewer or it fails."""
        if self.interviewer is not None:
            try:
                turn = await self.interviewer.interview(mode=mode, spec=spec, dialogue=draft.dialogue, message=message,
                                                        asking=draft.asking, editing=editing)
                log.info("telegram.intake.interviewed", extra={"user_id": draft.user_id, "done": turn.done,
                                                                "round": draft.rounds})
                return turn
            except Exception as exc:  # noqa: BLE001 - any failure falls back to the rules
                log.warning("telegram.intake.interview_failed",
                            extra={"user_id": draft.user_id, "error": getattr(exc, "code", type(exc).__name__),
                                   "status": getattr(exc, "status", None)})
        return await self.rules.interview(mode=mode, spec=spec, dialogue=draft.dialogue, message=message,
                                          asking=draft.asking, editing=editing)

    async def _carry_on(self, draft: Draft, turn: InterviewTurn | None = None, *, force: bool = False,
                        editing: bool = False) -> Reply:
        """Ask the next question, or show the card when nothing is left (or the person or the round cap says stop)."""
        mode = draft.mode or "real_estate"
        spec = draft.spec or TaskSpec(mode=mode)  # type: ignore[arg-type]
        missing = spec.missing_hard(mode)
        capped = force or draft.forced or draft.rounds >= self.max_rounds
        question = turn.question if turn else None
        asking = (turn.asking if turn else None) or (missing[0] if missing else None)
        options = turn.options if turn else ()
        choices = turn.choices if turn else ()
        if question is None and missing and not capped:
            asking, question = missing[0], question_for(spec, missing[0])
            options = options_for(missing[0])
        elif (question is None and not missing and not capped and not editing and mode == "real_estate"
              and not spec.answered(DEVIATION_PATH)):
            # Whatever the model said, the deviation question is always asked before the card.
            asking, question, options, choices = DEVIATION_PATH, question_for(spec, DEVIATION_PATH), DEVIATION_OPTIONS, ()
        if capped and "place" in missing:  # the one field that cannot be waved away
            asking, question, options = "place", question_for(spec, "place"), ()
            if force:
                question = "Место нужно в любом случае. " + question
        elif capped:
            question = None
        if question is not None:
            return await self._ask(draft, turn.understood_ru if turn else "", question, asking, options, choices)
        return await self._card(draft)

    async def _ask(self, draft: Draft, understood: str, question: str, asking: str | None,
                   options: tuple[tuple[str, str], ...], choices: tuple[str, ...] = ()) -> Reply:
        draft.step, draft.asking, draft.rounds = "ask", asking, draft.rounds + 1
        draft.choices = list(choices)
        draft.dialogue = [*draft.dialogue, {"role": "assistant", "text": question[:600]}][-MAX_DIALOGUE:]
        await self.store.save(draft)
        buttons = [Button(label, callback_data=f"task:{code}") for label, code in options]
        buttons += [Button(text, callback_data=f"task:opt:{i}") for i, text in enumerate(choices)]
        if asking and asking != "place":
            buttons.append(Button("Не важно", callback_data="task:skip"))
        buttons += [Button("Хватит, ищи", callback_data="task:enough"), Button("Отмена", callback_data="task:cancel")]
        text = f"Понял: {understood}\n\n{question}" if understood else question
        return Reply(text, tuple(buttons), keyboard=DRAFT_KEYBOARD)

    async def _card(self, draft: Draft) -> Reply:
        """«Проверьте задачу»: the structured card with «Запустить» «Изменить» «Отмена» (the bottom keyboard)."""
        assert draft.spec is not None
        try:
            plan = draft.plan()
        except InvalidGoal as exc:
            await self.store.save(draft.fresh())
            return Reply(f"{exc}\nОпишите задачу иначе, например {EXAMPLES.get(draft.mode or '', EXAMPLES['real_estate'])}.")
        draft.step, draft.asking, draft.editing = "summary", None, None
        await self.store.save(draft)
        lines = ["Проверьте задачу:", draft.spec.summary_ru()]
        if self.technical(draft.user_id):
            limits = plan.limits
            model = getattr(self.interviewer, "model", "")
            lines += [
                "",
                f"Разбор задачи: интервьюер ({model})" if model else "Разбор задачи: правила",
                f"Цель: {plan.goal}",
                f"Языки поиска: {', '.join(lang.upper() for lang in plan.languages)}",
                f"Группы: до {limits.max_groups}, окнами по {limits.window_size}",
            ]
        lines += ["", "Всё верно? Нажмите «Запустить» внизу, чтобы начать поиск."]
        return Reply("\n".join(lines), keyboard=TASK_KEYBOARD)

    async def _cancel(self, draft: Draft) -> Reply:
        await self.store.save(draft.fresh())
        return Reply("Черновик удалён. Опишите новую задачу, когда будете готовы.", keyboard=IDLE_KEYBOARD)

    # --- launch -----------------------------------------------------------------------------

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
        try:
            receipt = await self.sink(CommandEnvelope("campaign", draft.command_arguments(), draft.chat_id, user_id, draft.message_id))
        except Exception:
            log.exception("telegram.intake.enqueue_failed", extra={"user_id": user_id})
            await self.store.save(draft)  # back to the card: the person may press again
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


def _enrich(spec: TaskSpec, mode: str) -> TaskSpec:
    """The mode is the person's choice; a well-known city gets its country and names in every language."""
    spec = spec.model_copy(deep=True)
    spec.mode = mode  # type: ignore[assignment]
    name = spec.place.name
    if name and not spec.place.names and (found := find_places(name)) == [name] and name == found[0]:
        known = gazetteer_place(name)
        spec.place = spec.place.model_copy(update={"names": known.get("names", {}),
                                                   "country": spec.place.country or known.get("country")})
    return spec


def _cleared(spec: TaskSpec, path: str) -> TaskSpec:
    """A field the person says does not matter any more: its value is dropped (deal and type become «any»)."""
    spec = spec.model_copy(deep=True)
    match path:
        case "deal":
            spec.deal = "any"
        case "property_type":
            spec.property_type = "any"
        case "budget.max":
            spec.budget = spec.budget.model_copy(update={"min": None, "max": None})
        case "rooms.min":
            spec.rooms = spec.rooms.model_copy(update={"min": None, "max": None})
        case "area_m2.min":
            spec.area_m2 = spec.area_m2.model_copy(update={"min": None, "max": None})
        case "place.districts":
            spec.place = spec.place.model_copy(update={"districts": []})
        case "must_have":
            spec.must_have = []
        case "exclude":
            spec.exclude = []
        case "wishes":
            spec.wishes = []
        case "sources.required":
            spec.sources = spec.sources.model_copy(update={"required": [], "extra": []})
        case "investor.ticket":
            spec.investor.ticket = spec.investor.ticket.model_copy(update={"min": None, "max": None})
        case "investor.who":
            spec.investor.who = []
        case "investor.user_role":
            spec.investor.user_role = None
        case "investor.geography":
            spec.investor.geography = []
    return spec
