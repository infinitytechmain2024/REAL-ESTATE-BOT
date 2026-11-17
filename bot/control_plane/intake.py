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
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

from pydantic import ValidationError

from bot.campaign.architect import MAX_TEXT_CHARS, InvalidGoal, find_places, plan_campaign
from bot.campaign.fblinks import is_facebook_post
from bot.campaign.models import CampaignPlan
from bot.campaign.provinces import is_province
from bot.campaign.spec import (
    DEAL_RU,
    INVESTOR_WHO_RU,
    MODE_TITLES,
    PROPERTY_RU,
    Place,
    Sources,
    TaskSpec,
)
from bot.control_plane.interviewer import (
    Interviewer,
    InterviewTurn,
    explicit_must,
    strict_question,
    understood_lists,
)
from bot.control_plane.models import Button, CommandEnvelope, IncomingMessage, Reply
from bot.control_plane.rules import (
    AI_SKIP_WORDS,
    DEVIATION_OPTIONS,
    DEVIATION_PATH,
    SELLER_ROLE_OPTION,
    SKIP_WORDS,
    TASK_PATH,
    RuleInterviewer,
    declined_deviations,
    gazetteer_place,
    geo_ru,
    next_path,
    options_for,
    parse_budget,
    parse_deal,
    question_for,
    read_deviations,
    selling_in_text,
    still_declined,
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
EXAMPLES = {"real_estate": "«квартиры в аренду в Мадриде до 1200 €»", "investors": "«инвесторы для стартапа в Барселоне»",
            "both": "«квартиры на продажу в Валенсии до 300 тыс. € и инвесторы в недвижимость там же»"}
_SUBURB = re.compile(r"пригород|передміст|окрестност|околиц|afueras|alrededores|suburb|outskirts", re.IGNORECASE)
# INTAKE_SKIP_WIDENS_PLACE (Q42): the scope question names the city and offers its province (whole words: no
# «огород», «пригород»).
_PROVINCE_WORD = re.compile(r"(?<!\w)(?:провинци|област)|\bprovinc", re.IGNORECASE)
_CITY_WORD = re.compile(r"(?<!\w)город|\bciudad\b|\bcity\b", re.IGNORECASE)
# INTAKE_ASK_CITY_OR_PROVINCE (Q62): a province capital the person named bare («участок в Мадриде») is asked about
# once: only the city or the whole province. Not asked when the person said which already (city, capital, province,
# region, coast, suburbs) or named a district, a street or another town.
SCOPE_PATH = "place.level"
SCOPE_QUESTION = "Искать только в самом городе {city} или по всей провинции?"
SCOPE_OPTIONS: tuple[tuple[str, str], ...] = (("Только в городе", "scope:city"), ("Вся провинция", "scope:province"))
_SCOPE_CITY = re.compile(r"(?<!\w)(?:столиц|міст[оіау]?\b)|\bcapital\b", re.IGNORECASE)
_SCOPE_CENTRE = re.compile(r"(?<!\w)центр|\b(?:centro|centre|center|downtown)\b", re.IGNORECASE)
_SCOPE_EVERYWHERE = re.compile(r"(?<!\w)(?:везде|повсюду|скрізь)(?!\w)|\b(?:everywhere|anywhere)\b|"
                               r"\btodas\s+partes\b", re.IGNORECASE)
_SCOPE_NEGATED = re.compile(r"(?<!\w)(?:не|ні|no|not|без|sin|ni)\s+(?:\w+\s+){0,3}$", re.IGNORECASE)
# Q62: no scope question for these (no province around them that the person could mean)
_NO_SCOPE_PLACES = frozenset({"ceuta", "melilla"})
# Q62: capitalised words that are no other place: the country, portals and brands
_NOT_PLACE_STEMS = ("испани", "іспані", "espan", "spain", "idealist", "идеалист", "fotocas", "фотокас", "airbnb",
                    "habitacli", "milanunci", "pisos", "kyero", "thinkspain", "yaencontre", "wallapop", "booking",
                    "facebook", "instagram", "telegram", "whatsapp", "tiktok", "linkedin", "google", "avito",
                    "ватсап", "телеграм", "фейсбук", "инстаграм")
_METRO_WORDS = frozenset({"метро", "metro", "станция", "станции", "estacion", "station"})
_SCOPE_COAST = re.compile(r"(?<!\w)(?:побережь|узбережж|коста(?!\w))|\b(?:costa|coast\w*)\b", re.IGNORECASE)
_SCOPE_SAID = re.compile(r"(?<!\w)(?:город|провинци|провінці|област|регион|окрестност|пригород)|"
                         r"\b(?:ciudad|city|provinc\w*|comunidad|region)\b", re.IGNORECASE)
_STREET_WORD = re.compile(r"(?<!\w)(?:улиц|ул\.|проспект|переул|бульвар|район|квартал|микрорайон|вулиц|c/)|"
                          r"\b(?:calle|avenida|avda|plaza|paseo|barrio|distrito|street|avenue|district)\b",
                          re.IGNORECASE)
_WHOLE_WORD = re.compile(r"(?<!\w)(?:вс[яеию]|всей|весь|любо\w*)(?!\w)|\b(?:toda|todo|whole|entire|all|any)\b",
                         re.IGNORECASE)
_ONLY_WORD = re.compile(r"(?<!\w)только(?!\w)|\b(?:solo|sólo|only)\b", re.IGNORECASE)
_RADIUS = re.compile(r"(?<!\d)(\d{1,3}(?:[.,]\d+)?)\s*(?:км|km|kilomet(?:er|re)s?)(?!\w)", re.IGNORECASE)
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
FIELDS["both"] = {**FIELDS["real_estate"], "who": FIELDS["investors"]["who"], "ticket": FIELDS["investors"]["ticket"],
                  "role": FIELDS["investors"]["role"], "geo": FIELDS["investors"]["geo"]}
_WORD = re.compile(r"\w+")
# INTAKE_ROLE_CONFLICT_CHECK (Q52): who the task's own words say the person is. «ищу соинвесторов в свои проекты»:
# raising money; «хочу вложить», «quiero invertir», «busco proyectos»: deploying it. Both or neither: unknown.
_RAISING_WORDS = re.compile(
    r"(?:ищу|ищем|шукаю|шукаємо|нужн\w*|нужен|требу\w*)\s+(?:\w+\s+){0,2}(?:со-?)?инвестор|соинвестор|співінвестор|"
    r"(?:шукаю|шукаємо)\s+(?:\w+\s+){0,2}інвестор|привлеч\w*\s+(?:\w+\s+){0,2}(?:инвест|денег|деньги|капитал)|"
    r"финансирован|фінансуванн|ищу деньги|ищем деньги|шукаю гроші|"
    r"(?:в|для)\s+(?:сво\w+|мо\w+|наш\w+)\s+(?:\w+\s+)?(?:проект|стартап|бизнес)|"
    r"\bsocios?\b|\bbusco\s+(?:\w+\s+){0,2}inversor|inversor\w*\s+para\s+(?:mi|nuestro)s?\s+proyecto|"
    r"financiaci[oó]n|looking for (?:co-?)?investors|\bco-?investors? for (?:my|our)|\braise\b|\braising\b",
    re.IGNORECASE)
_DEPLOYING_WORDS = re.compile(
    r"(?:хочу|хотим|готов\w*|планиру\w*)\s+(?:\w+\s+){0,2}(?:инвестир|вложи|вкладыва|вкласт|вкладат|інвестува)|"
    r"(?:ищу|ищем|шукаю)\s+(?:(?:нов|хорош|интересн|перспективн|инвестиционн)\w*\s+)?(?:проект|объект\w* для инвест)|"
    r"куда вложи|куди вкласти|"
    r"сво\w+ деньги|"
    r"quiero invertir|queremos invertir|\bbusco\s+(?:nuevos\s+|buenos\s+)?proyectos|"
    r"\b(?:i|we) want to invest|looking for (?:\w+\s+)?projects to invest",
    re.IGNORECASE)
ROLE_CONFLICT_QUESTIONS = {
    "raising": "Вы писали, что ищете инвесторов или деньги в свой проект, а сейчас — что хотите вкладывать сами. "
               "Что верно?",
    "deploying": "Вы писали, что хотите вкладывать сами, а сейчас — что ищете деньги в свой проект. Что верно?",
    # INVESTOR_SELLER_ROLE (Q63): only with the flag a task reads as selling
    "selling": "Вы писали, что продаёте объект и ищете покупателя, а сейчас — другое. Что верно?",
}
ROLE_CONFLICT_OPTIONS = (("Ищу деньги в свой проект", "role:raising"), ("Хочу вложить свои деньги", "role:deploying"))


def role_in_text(text: str) -> str | None:
    """INTAKE_ROLE_CONFLICT_CHECK (Q52): «raising» or «deploying» when the words say only one of them, else None."""
    raising, deploying = _said(_RAISING_WORDS, text or ""), _said(_DEPLOYING_WORDS, text or "")
    return "raising" if raising and not deploying else "deploying" if deploying and not raising else None


# Q52: «не ищу инвесторов», «no busco socios», «not looking for investors»: a negation up to 2 words before the match.
_NEGATION = re.compile(r"(?<!\w)(?:не|ні|нет|no|not|don'?t|doesn'?t|nunca)(?!\w)", re.IGNORECASE)
_NEGATED_BEFORE = re.compile(r"(?<!\w)(?:не|ні|no|not|don'?t)\s+(?:\w+\s+)?$", re.IGNORECASE)


def _said(pattern: re.Pattern[str], text: str) -> bool:
    return any(not _NEGATED_BEFORE.search(text[:m.start()]) for m in pattern.finditer(text))


# The labels ``Draft.goal_text`` writes around the person's words (keep in step with it): what reads the campaign's
# ``source_text`` for the person's intent strips them first (Q23: «Районы или пригороды: Centro.» is no suburb request).
INTAKE_LABELS = ("Кого ищем", "Тип", "Задача", "Районы или пригороды", "Радиус поиска", "Главное", "Дополнительно")
INTAKE_LABEL_RE = re.compile(r"(?<!\w)(?:" + "|".join(map(re.escape, INTAKE_LABELS)) + r"):")


def user_words(source_text: str) -> str:
    """The campaign text without the intake labels (their values stay)."""
    return INTAKE_LABEL_RE.sub(" ", source_text)


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
    widened: bool = False  # INTAKE_SKIP_WIDENS_PLACE (Q42): «Не важно» widened the city to its province
    role_check: str = ""  # INTAKE_ROLE_CONFLICT_CHECK (Q52): "" | asking (the question is on screen) | done (asked once)
    scope_check: str = ""  # INTAKE_ASK_CITY_OR_PROVINCE (Q62): "" | asking (the question is on screen) | done

    def copy(self, **changes: Any) -> Draft:
        return replace(self, dialogue=[dict(t) for t in self.dialogue], choices=list(self.choices),
                       spec=self.spec.model_copy(deep=True) if self.spec is not None else None, **changes)

    def goal_text(self) -> str:
        """The task in words for the planner and the owner: the deal and budget first (the planner reads the
        first price it finds), the type, the task, then the must-haves and the wishes, within the length limit."""
        spec = self.spec or TaskSpec()
        head: list[str] = []
        if self.mode in ("investors", "both") and (who := ", ".join(INVESTOR_WHO_RU.get(w, w) for w in spec.investor.who)):
            head.append(f"Кого ищем: {who}.")
        if self.mode != "investors":
            if spec.deal in ("rent", "sale"):
                head.append(DEAL_TEXT[spec.deal])
            if spec.budget.max:
                currency = spec.budget.currency or "EUR"
                amount = int(spec.budget.max)
                head.append(f"до {amount} €" if currency == "EUR" else f"бюджет {amount} {currency}")
            if spec.property_type not in (None, "any", "other"):
                head.append(f"Тип: {PROPERTY_RU[spec.property_type]}.")  # type: ignore[index]
        tail: list[str] = []
        if spec.place.districts:
            tail.append("Районы или пригороды: " + ", ".join(spec.place.districts) + ".")
        if spec.place.radius_km:
            tail.append(f"Радиус поиска: {spec.place.radius_km:g} км.")
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
                "choices": list(self.choices), "widened": bool(self.widened), "role_check": self.role_check,
                "scope_check": self.scope_check}

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
                   [str(c) for c in payload.get("choices") or [] if isinstance(c, str)],
                   bool(payload.get("widened")), str(payload.get("role_check") or ""),
                   str(payload.get("scope_check") or ""))

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


def mode_menu(text: str = "Выберите режим:", *, both: bool = False) -> Reply:
    """The mode buttons; «Недвижимость + инвесторы» only with ``both`` (INTAKE_BOTH_MODE)."""
    return Reply(text, tuple(Button(title, callback_data=f"mode:{mode}") for mode, title in MODES.items()
                             if both or mode != "both"))


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


POST_TASK = "лиды из комментариев под постом"
MAX_POST_LINK_CHARS = 120  # ``TaskSpec.sources`` keeps a link this long at most


def post_link(text: str) -> str | None:
    """The text as one Facebook post link without tracking parameters (kept in ``sources.required``), else None."""
    text = text.strip()
    if not text or any(ch.isspace() for ch in text) or not is_facebook_post(text):
        return None
    parts = urlsplit(text)
    link = f"https://{parts.hostname}{parts.path}"
    return link if len(link) <= MAX_POST_LINK_CHARS else None


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
                 interviewer: Interviewer | None = None, max_rounds: int = MAX_ROUNDS,
                 post_links: bool = False, both_mode: bool = False, skip_widens_place: bool = False,
                 role_conflict_check: bool = False, ask_city_or_province: bool = False,
                 seller_role: bool = False, must_explicit: bool = False, strict_question_must: bool = False,
                 skip_declined: bool = False) -> None:
        """``technical(user_id)`` says who also sees planner details and queue ids (the owner).

        ``post_links`` (``APIFY_FACEBOOK_COMMENTS_ENABLED``): a task that is one Facebook post link becomes an
        investor search over that post's comments (no interview); off, the link is an ordinary task text.

        ``both_mode`` (``INTAKE_BOTH_MODE``): the mode «Недвижимость + инвесторы» is offered and accepted; off,
        the menu has no such button and the choice is refused.

        ``skip_widens_place`` (``INTAKE_SKIP_WIDENS_PLACE``, Q42): «Не важно» to «only the city or its province»
        widens a province capital to its province (see ``_widened_place``); off, the place stays the city.

        ``role_conflict_check`` (``INTAKE_ROLE_CONFLICT_CHECK``, Q52): an answer that sets the investor role against the
        task's own words («ищу соинвесторов в свои проекты» and then «хочу вкладывать») is asked about once with
        buttons; the answer to it sets the role. Off, the answer is taken as it is.

        ``ask_city_or_province`` (``INTAKE_ASK_CITY_OR_PROVINCE``, Q62): a bare province capital («в Мадриде») is
        asked about once after the hard fields: «Искать только в самом городе Мадрид или по всей провинции?» with
        buttons; «Вся провинция» and «Не важно» make the place the province, «Только в городе» keeps the city (see
        ``_needs_scope``). Off, nothing is asked and the place stays the city.

        ``seller_role`` (``INVESTOR_SELLER_ROLE``, Q63): the investor role has a third value, selling (the person has a
        property to sell and seeks its buyers or investors): words like «have a villa … need buyers», «продаю, ищу
        покупателя», «vendo …, busco comprador» in the task (or in the answer to the role question) set it, and the role
        buttons get «Продаю объект, ищу покупателя». Off, the role is raising or deploying, as before.

        ``must_explicit`` (``INTAKE_MUST_HAVE_EXPLICIT``, Q70): a must-have the AI interviewer added from an answer
        without a word of obligation («обязательно», «только», «imprescindible» …) becomes a wish of weight 2, and a
        new wish of weight 3 without «очень важно» gets weight 2; the must-haves there before are never lost or lowered
        (``interviewer.explicit_must``). Off, as the model returned it. The rules of ``rules.py`` ask «Что обязательно должно быть?» and are not changed.

        ``strict_question_must`` (``INTAKE_STRICT_QUESTION_MUST``, Q73; only with ``must_explicit``): the answer to a
        question that itself asks for the required or strict items («Есть ли строгие требования…?», «Что обязательно…?»,
        ``interviewer.strict_question``) counts as said required; and when the safety net still lowered a must-have of
        the model, «Понял: …» is rebuilt from the final spec (``interviewer.understood_lists``), so it never says
        «обязателен» of a wish. Off, as with ``must_explicit`` alone.

        ``skip_declined`` (``FAR_VARIANTS_SKIP_DECLINED``, Q75): the answer to the deviation question whose suggested
        answers (buttons «task:opt:<i>») offered compromises records those it did not take in
        ``spec.deviations.declined`` (``rules.declined_deviations``); the runner never offers a farther finding that has
        one. Off, nothing is recorded.

        ``interviewer`` (optional) reads every message with AI; without it, or when it fails, the rules of
        ``rules.py`` interview instead. ``max_rounds`` caps the questions per task.
        """
        self.store, self.sink = store, sink
        self.notify_owners, self.now, self.technical = notify_owners, now, technical
        self.interviewer = interviewer
        self.seller_role = seller_role
        self.rules = RuleInterviewer(seller_role=seller_role)
        self.max_rounds, self.post_links, self.both_mode = max_rounds, post_links, both_mode
        self.skip_widens_place = skip_widens_place
        self.role_conflict_check = role_conflict_check
        self.ask_city_or_province = ask_city_or_province
        self.must_explicit = must_explicit
        self.strict_question_must = strict_question_must  # INTAKE_STRICT_QUESTION_MUST (Q73)
        self.skip_declined = skip_declined  # FAR_VARIANTS_SKIP_DECLINED (Q75)

    def menu(self, text: str = "Выберите режим:") -> Reply:
        return mode_menu(text, both=self.both_mode)

    async def _draft(self, user_id: int, chat_id: int) -> Draft:
        draft = await self._stored(user_id)
        if draft is None:
            return Draft(user_id, chat_id)
        if draft.expired(self.now()):  # untouched for a day: the task is gone, the mode stays
            draft = draft.fresh()
        draft.chat_id = chat_id
        return draft

    async def _stored(self, user_id: int) -> Draft | None:
        """The saved draft; with the flag off a «both» draft counts as no mode chosen (the menu comes back)."""
        draft = await self.store.get(user_id)
        if draft is not None and draft.mode == "both" and not self.both_mode:
            draft = replace(draft.fresh(), mode=None)
        return draft

    async def mode(self, user_id: int) -> str | None:
        draft = await self._stored(user_id)
        return draft.mode if draft else None

    async def drafting(self, user_id: int) -> bool:
        """A task is being written (a question or the card is on screen) and has not expired."""
        draft = await self._stored(user_id)
        return draft is not None and draft.step != "idle" and not draft.expired(self.now())

    async def choose_mode(self, user_id: int, chat_id: int, mode: str) -> Reply:
        if mode not in MODES or (mode == "both" and not self.both_mode):
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
            if draft.asking == SCOPE_PATH and draft.scope_check == "asking" and word not in AI_SKIP_WORDS:
                # only an explicit «не важно» widens; the rules' «нет», «-», «любой» keep the city
                level = _scope_in_text(text, draft.spec.place) or ("city" if word in SKIP_WORDS else None)
                return await self._scope_answer(draft, level, text)
            if word in self._skip_words() and draft.asking and draft.asking != "place":
                return await self._skip(draft)
            return await self._interview(draft, text)
        if draft.spec is not None and draft.step == "summary":
            return await self._interview(draft, text)  # a correction typed under the card
        # idle (or a stale step from an older version): a new task
        if len(text) > MAX_TASK_CHARS:
            return Reply(f"Слишком длинная задача (больше {MAX_TASK_CHARS} символов). Сократите её.")
        if self.post_links and (url := post_link(text)) is not None:
            return await self._post_task(draft, message, url)
        draft = replace(draft.fresh(), task=text, message_id=message.message_id)
        if draft.mode is None:
            await self.store.save(draft)
            return self.menu("Сначала выберите режим — задача сохранена:")
        return await self._interview(draft, text)

    async def _post_task(self, draft: Draft, message: IncomingMessage, url: str) -> Reply:
        """A Facebook post link as the whole task: an investor search over its comments, straight to the card.

        The campaign needs a place; the post has none, so the country stands in (the post itself is the source).
        """
        spec = TaskSpec(mode="investors", sources=Sources(required=[url]), post_link=True,
                        place=Place(name="Spain", country="ES", level="region",
                                    names={"es": "España", "ru": "Испания", "uk": "Іспанія"}))
        draft = replace(draft.fresh(), mode="investors", task=POST_TASK, message_id=message.message_id, spec=spec)
        return await self._card(draft)

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
        if action == "scope" and value in ("city", "province") and draft.asking == SCOPE_PATH:
            return await self._scope_answer(draft, value)
        if action == "role" and value in self._roles() and draft.asking == "investor.user_role":
            said = {"raising": "ищу деньги для проекта", "deploying": "хочу вкладывать",
                    "selling": "продаю объект, ищу покупателя"}[value]
            return await self._interview(draft, said, role=value if draft.role_check == "asking" else None)
        return Reply(STALE)

    def _roles(self) -> tuple[str, ...]:
        """The investor roles a button may set; selling only with INVESTOR_SELLER_ROLE (Q63)."""
        return ("raising", "deploying", "selling") if self.seller_role else ("raising", "deploying")

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
        hint = question_for(draft.spec or TaskSpec(), path, seller=self.seller_role)
        return Reply(f"{title}: напишите новое значение. {hint}", (Button("Назад", callback_data="task:back"),),
                     keyboard=DRAFT_KEYBOARD)

    async def _edit_value(self, draft: Draft, text: str) -> Reply:
        """The new value of the field picked in «Изменить»: the interviewer applies it; the card comes back."""
        path = draft.editing or ""
        title = next((t for t, p in FIELDS[draft.mode or "real_estate"].values() if p == path), path)
        spec = draft.spec or TaskSpec(mode=draft.mode)  # type: ignore[arg-type]
        if path == "place":
            draft.widened = False  # Q42: a new place is no longer the widened one
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
        if draft.asking == SCOPE_PATH:  # Q62: «Не важно» to «only the city or the whole province»: the province
            return await self._scope_answer(draft, "province")
        if draft.asking and draft.asking != "place":
            draft.spec = spec.mark_unspecified(draft.asking)
        widen = _widened_place(draft, spec) if self.skip_widens_place else None
        return await self._interview(draft, "Не важно", widen=widen)

    async def _scope_answer(self, draft: Draft, level: str | None, text: str = "") -> Reply:
        """INTAKE_ASK_CITY_OR_PROVINCE (Q62): the answer to «only the city or the whole province» (asked once).

        ``level`` «city» or «province» sets the place's level whatever the model reads; None (a typed answer that
        says neither, e.g. «север: Алькобендас») goes to the interviewer as the answer about the districts.
        """
        draft.scope_check = "done"
        if level is None:
            draft.asking = "place.districts"
            return await self._interview(draft, text)
        city = (draft.spec.place.names.get("ru") or draft.spec.place.name) if draft.spec else ""
        message = f"По всей провинции {city}" if level == "province" else f"Только в самом городе {city}"
        return await self._interview(draft, message, place_level=level)

    def _needs_scope(self, draft: Draft, spec: TaskSpec, question: str | None) -> bool:
        """INTAKE_ASK_CITY_OR_PROVINCE (Q62): ask «only the city or the whole province» for this place.

        Only a city of Spain whose name is its province's too (Madrid, Málaga, Alicante, Valencia, Sevilla; not
        Marbella), without districts or a radius, not asked before. Not when the person's words already say which
        («в городе», «capital», «в провинции», «побережье», «пригороды»), name a street or a district word, or name
        another place (a capitalised word inside a sentence of the task: «в Чамбери, Мадрид»), nor when the model has
        asked the same already (a question with «город» and «провинция»).
        """
        place = spec.place
        if (draft.scope_check or not place.name or place.level != "city" or place.districts or place.radius_km
                or place.country not in (None, "ES") or not is_province(place.name)
                or _fold_text(place.name).strip() in _NO_SCOPE_PLACES):
            return False
        said = [draft.task, *(t["text"] for t in draft.dialogue if t.get("role") == "user")]
        if any(p.search(t) for t in said for p in (_SCOPE_SAID, _SCOPE_CITY, _SCOPE_COAST, _SCOPE_CENTRE,
                                                                _SCOPE_EVERYWHERE, _STREET_WORD)) or _other_place(draft.task, place):
            return False
        asked = [question or "", *(t["text"] for t in draft.dialogue if t.get("role") == "assistant")]
        return not any(_CITY_WORD.search(q) and _PROVINCE_WORD.search(q) for q in asked)

    async def _enough(self, draft: Draft) -> Reply:
        """«Хватит, ищи»: no more questions; only a missing place is still asked."""
        draft.forced = True
        return await self._carry_on(draft, force=True)

    # --- the interview ----------------------------------------------------------------------

    async def _interview(self, draft: Draft, message: str, *, editing: bool = False,
                         widen: dict[str, object] | None = None, role: str | None = None,
                         place_level: str | None = None) -> Reply:
        """One user message: the interviewer updates the spec and says what to ask next, or the card is shown.

        ``widen`` (Q42): the place fields «Не важно» widened; applied after the model, which may resend the city.
        ``role`` (Q52): the button pressed under the role-conflict question; it sets the role whatever the model read.
        ``place_level`` (Q62): «city» or «province» answered to «only the city or the whole province»; applied last.
        """
        mode = draft.mode or "real_estate"
        spec = draft.spec or TaskSpec(mode=mode)  # type: ignore[arg-type]
        turn = await self._turn(draft, mode, spec, message, editing)
        draft.spec = _enrich(self._record_answer(draft, spec, turn.spec, message, editing,
                                                 next_question=turn.question), mode)
        if self.skip_declined and draft.spec.deviations.declined and (editing or draft.step == "summary"):
            # Q75: a correction under the card («студия тоже подойдёт») or a «Изменить» takes back what it now allows;
            # a field of «Изменить» («комнаты: от 2») only by the spec's values, not by its words
            draft.spec.deviations.declined = still_declined(draft.spec.deviations.declined,
                                                            "" if editing else message, draft.spec)
        elif (self.skip_declined and not editing and draft.asking == DEVIATION_PATH and draft.choices
                and _norm(message) not in self._skip_words()):
            # Q75: the compromises the suggested answers offered and this answer did not take
            draft.spec.deviations.declined = declined_deviations(draft.choices, message, draft.spec)
            if draft.spec.deviations.declined:
                log.info("telegram.intake.deviations_declined",
                         extra={"user_id": draft.user_id, "declined": draft.spec.deviations.declined})
        if widen:
            draft.spec.place = draft.spec.place.model_copy(update=widen)
            draft.widened = True
            log.info("telegram.intake.place_widened", extra={"user_id": draft.user_id, **widen})
        if place_level and draft.spec.place.name:
            draft.spec.place = draft.spec.place.model_copy(update={"level": place_level})
            draft.widened = place_level == "province"
            if draft.widened and not draft.spec.place.districts:  # the whole province: no «which district?» next
                draft.spec = draft.spec.mark_unspecified("place.districts")
            log.info("telegram.intake.place_scope", extra={"user_id": draft.user_id, "level": place_level})
        if draft.asking == "place.districts" and _SUBURB.search(draft.task):
            if draft.spec.place.radius_km and draft.spec.place.radius_km != spec.place.radius_km:
                turn.understood_ru = f"радиус {draft.spec.place.radius_km:g} км"
            elif draft.spec.place.districts != spec.place.districts:
                turn.understood_ru = "пригороды: " + ", ".join(draft.spec.place.districts)
        if (self.seller_role and mode in ("investors", "both") and not editing and draft.role_check != "asking"
                and (message == draft.task or draft.asking == "investor.user_role") and selling_in_text(message)
                and draft.spec.investor.user_role in (None, "raising")):
            # INVESTOR_SELLER_ROLE (Q63): «have a villa … need buyers» is selling where the model read no role or raising
            # (b5-n2-s6); a model's «deploying» and the role-conflict answer (Q52) are never overridden
            draft.spec.investor.user_role = "selling"
            log.info("telegram.intake.role_selling", extra={"user_id": draft.user_id})
        draft.dialogue = [*draft.dialogue, {"role": "user", "text": message[:600]}][-MAX_DIALOGUE:]
        draft.editing = None
        if (self.role_conflict_check and mode in ("investors", "both") and not editing
                and (task_role := self._role_conflict(draft, spec, message, role)) is not None):
            options = (*ROLE_CONFLICT_OPTIONS, SELLER_ROLE_OPTION) if self.seller_role else ROLE_CONFLICT_OPTIONS
            return await self._ask(draft, "", ROLE_CONFLICT_QUESTIONS[task_role], "investor.user_role",
                                   options, only_options=True)
        return await self._carry_on(draft, turn, editing=editing)

    def _role_conflict(self, draft: Draft, before: TaskSpec, message: str, pressed: str | None = None) -> str | None:
        """INTAKE_ROLE_CONFLICT_CHECK (Q52): the task's role when this answer set the other one (asked once), else None.

        The answer to that question: a pressed button sets the role; typed words set it only where the model set
        none (or the same), and never with a negation («не хочу вкладывать» is no «хочу вкладывать»).
        """
        assert draft.spec is not None
        if draft.role_check == "asking":
            draft.role_check = "done"
            model_role = draft.spec.investor.user_role
            if pressed in self._roles():
                draft.spec.investor.user_role = pressed  # type: ignore[assignment]
            elif (draft.asking == "investor.user_role" and not _NEGATION.search(message)
                  and (said := role_in_text(message)) is not None and model_role in (None, said)):
                draft.spec.investor.user_role = said  # type: ignore[assignment]
            return None
        role = draft.spec.investor.user_role
        if draft.role_check or not role or role == before.investor.user_role or message == draft.task:
            return None
        task_role = "selling" if self.seller_role and selling_in_text(draft.task) else role_in_text(draft.task)
        if task_role is None or task_role == role:
            return None
        draft.role_check = "asking"
        draft.spec.investor.user_role = before.investor.user_role
        log.info("telegram.intake.role_conflict", extra={"user_id": draft.user_id, "task_role": task_role,
                                                         "answer_role": role})
        return task_role

    def _record_answer(self, draft: Draft, before: TaskSpec, after: TaskSpec, message: str, editing: bool,
                       *, next_question: str | None = None) -> TaskSpec:
        """Safety net: the answer to a task-specific or deviation question is kept even if the model dropped it."""
        if draft.asking == "place.districts" and _SUBURB.search(draft.task) and not editing:
            if match := _RADIUS.search(message):
                radius = float(match.group(1).replace(",", "."))
                if 0 < radius <= 1000:
                    after.place = before.place.model_copy(update={"radius_km": radius})
                    return after
            if message.strip() and _norm(message) not in self._skip_words():
                names = [p.strip(" .") for p in re.split(r"[,;\n]|\s+и\s+", message) if p.strip(" .")]
                if names:
                    after.place = before.place.model_copy(update={"districts": names[:12]})
                    return after
        if editing or draft.asking not in (TASK_PATH, DEVIATION_PATH) or _norm(message) in self._skip_words():
            return after
        if draft.asking == DEVIATION_PATH:
            return after if after.deviations.asked else read_deviations(after, message)
        question = next((t["text"] for t in reversed(draft.dialogue) if t.get("role") == "assistant"), "")
        if next_question and _norm(next_question) == _norm(question):
            # The interviewer kept this question open. An unrelated answer may
            # still add useful criteria, but it must not count as answering it.
            after.context.answers = list(before.context.answers)
            return after
        if len(after.context.answers) > len(before.context.answers):
            return after
        return after.merged({"context": {"answers": [{"question": question, "answer": message}]}})

    async def _turn(self, draft: Draft, mode: str, spec: TaskSpec, message: str, editing: bool) -> InterviewTurn:
        """One AI call; the rules answer instead when there is no interviewer or it fails."""
        if self.interviewer is not None:
            try:
                turn = await self.interviewer.interview(mode=mode, spec=spec, dialogue=draft.dialogue, message=message,
                                                        asking=draft.asking, editing=editing)
                if self.must_explicit and not editing:  # INTAKE_MUST_HAVE_EXPLICIT (Q70): must only when said
                    question = next((t["text"] for t in reversed(draft.dialogue) if t.get("role") == "assistant"), "")
                    asked = self.strict_question_must and strict_question(question)  # Q73
                    model_musts = list(turn.spec.must_have)
                    turn.spec = explicit_must(spec, turn.spec, message, asked_must=asked)
                    if self.strict_question_must and any(m not in turn.spec.must_have for m in model_musts):
                        turn.understood_ru = understood_lists(spec, turn.spec)  # Q73: never «обязателен» of a wish
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
        if asking == "place.districts" and (spec.place.radius_km or spec.place.districts or (
                draft.scope_check == "done" and draft.widened and spec.is_unspecified("place.districts"))):
            asking = next_path(spec, mode)
            question = question_for(spec, asking, seller=self.seller_role) if asking else None
            options = options_for(asking, seller=self.seller_role) if asking else ()
            choices = ()
        suburb_scope = (mode in ("real_estate", "both") and bool(_SUBURB.search(draft.task))
                        and not spec.place.radius_km and not spec.place.districts
                        and not spec.is_unspecified("place.districts"))
        if not missing and not capped and suburb_scope:
            asking = "place.districts"
            question = "Какие пригороды или населённые пункты искать? Можно вместо названий указать радиус в км от города."
            options, choices = (), ()
        elif (not missing and not capped and not editing and self.ask_city_or_province
              and self._needs_scope(draft, spec, question)):
            draft.scope_check = "asking"
            asking = SCOPE_PATH
            question = SCOPE_QUESTION.format(city=spec.place.names.get("ru") or spec.place.name)
            options, choices = SCOPE_OPTIONS, ()
        elif question is None and missing and not capped:
            asking, question = missing[0], question_for(spec, missing[0], seller=self.seller_role)
            options = options_for(missing[0], seller=self.seller_role)
        elif (question is None and not missing and not capped and not editing and mode in ("real_estate", "both")
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
                   options: tuple[tuple[str, str], ...], choices: tuple[str, ...] = (), *,
                   only_options: bool = False) -> Reply:
        """``only_options`` (Q52, the role-conflict question): the option buttons and «Отмена», no skip or «enough»."""
        draft.step, draft.asking, draft.rounds = "ask", asking, draft.rounds + 1
        draft.choices = list(choices)
        draft.dialogue = [*draft.dialogue, {"role": "assistant", "text": question[:600]}][-MAX_DIALOGUE:]
        await self.store.save(draft)
        buttons = [Button(label, callback_data=f"task:{code}") for label, code in options]
        buttons += [Button(text, callback_data=f"task:opt:{i}") for i, text in enumerate(choices)]
        if asking and asking != "place" and not only_options:
            buttons.append(Button("Не важно", callback_data="task:skip"))
        if not only_options:
            buttons.append(Button("Хватит, ищи", callback_data="task:enough"))
        buttons.append(Button("Отмена", callback_data="task:cancel"))
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
        lines = ["Проверьте задачу:", draft.spec.summary_ru(
            province_scope=(self.skip_widens_place or self.ask_city_or_province) and draft.widened)]
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


def _widened_place(draft: Draft, spec: TaskSpec) -> dict[str, object] | None:
    """INTAKE_SKIP_WIDENS_PLACE (Q42): what «Не важно» to «only the city or its province» widens the place to.

    The question on screen is the scope question when it was asked for ``place.districts`` or the task-specific
    round and its text names the city, the word «город» and the province («Искать только в самом городе Мадрид или
    по провинции?»); the rules' «Район или вся Валенсия?», «огород вокруг дома» or a province question without the
    city are not. Only a city whose name is a province of Spain too (Madrid, Valencia, Málaga; a one-province
    community) becomes that province («Мадрид и провинция»): Marbella or Elche have no province of their name and
    stay as they are. A place that is wider already, or has districts or a radius, is left too. None: no change.
    """
    place = spec.place
    if (not place.name or place.level != "city" or place.districts or place.radius_km
            or place.country not in (None, "ES") or not is_province(place.name)
            or draft.asking not in ("place.districts", TASK_PATH)):
        return None
    question = next((t["text"] for t in reversed(draft.dialogue) if t.get("role") == "assistant"), "")
    if not (_CITY_WORD.search(question) and _PROVINCE_WORD.search(question) and _names_place(question, place)):
        return None
    return {"level": "province"}


def _scope_in_text(text: str, place: Place) -> str | None:
    """INTAKE_ASK_CITY_OR_PROVINCE (Q62): «city», «province» or None (neither) in a typed answer to the scope question.

    The province only by explicit words: «провинция», «область», «везде», «побережье», «Коста-дель-Соль», «и город, и
    провинция», a negated city («не город», «не только город»). The city: «город», «только город», «solo la ciudad»,
    «центр», «весь Мадрид», «весь город», «в любом районе», a negated province («не провинция», «без области»).
    """
    def hits(*patterns: re.Pattern[str]) -> list[bool]:  # one flag per match: negated or not
        return [bool(_SCOPE_NEGATED.search(text[:m.start()])) for p in patterns for m in p.finditer(text)]

    province = hits(_PROVINCE_WORD, _SCOPE_COAST, _SCOPE_EVERYWHERE)
    city = hits(_CITY_WORD, _SCOPE_CITY, _SCOPE_CENTRE)
    if (False in province) or (True in city):
        return "province"
    if (True in province) or (False in city):
        return "city"
    if (_ONLY_WORD.search(text) or _WHOLE_WORD.search(text) or re.search(r"(?<!\w)район", text, re.IGNORECASE)) and (
            _names_place(text, place) or re.search(r"(?<!\w)район|\bbarrio|\bzona\b", text, re.IGNORECASE)):
        return "city"
    return None


def _other_place(text: str, place: Place) -> bool:
    """Q62: the task names a place besides ``place``: a capitalised word inside a sentence that is not the place
    («в Чамбери, Мадрид», «Мадрид, Las Rozas»). The first word of a sentence, acronyms («IBI»), the country
    («Испания», «España»), portals and brands («Idealista», «Airbnb») and a metro station («рядом с метро Sol») do
    not count."""
    for sentence in re.split(r"[.!?\n:;]", text):
        words = re.findall(r"[^\W\d_][\w'-]*", sentence)
        station = False
        for i, w in enumerate(words):
            folded = _fold_text(w)
            if folded in _METRO_WORDS:
                station = True
                continue
            capital = w[0].isupper() and not w.isupper()
            if station and capital:
                continue  # the station's name, one or more words
            station = False
            if (i and capital and not _names_place(w, place)
                    and not any(folded.startswith(stem) for stem in _NOT_PLACE_STEMS)):
                return True
    return False


def _names_place(question: str, place: Place) -> bool:
    """The question names the place: its name in any language, also in another case («Мадриде», «Валенсии»)."""
    folded = _fold_text(question)
    for name in {place.name or "", *place.names.values()}:
        stem = _fold_text(name)
        stem = stem[:-1] if len(stem) > 4 else stem
        if stem and re.search(rf"(?<!\w){re.escape(stem)}", folded):
            return True
    return False


def _fold_text(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold()) if not unicodedata.combining(c))


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
