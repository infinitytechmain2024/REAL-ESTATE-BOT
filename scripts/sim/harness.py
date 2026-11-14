"""One simulated run: the interview, «Проверьте задачу», the campaign with every stage, the cards and the report.

The production code runs as the services wire it (``bot/control_plane/main.py``, ``bot/campaign/runner.py:main``,
``bot/analysis_pipeline/main.py``, ``bot/facebook_collector/runner.py``), in one process, step by step instead of
in loops: each round calls one tick of every stage, then the runner's. Only the outside world is replaced
(``cassette.SimNet``): the LLM and Apify answers come from files, Telegram goes to a list, nothing reaches a
network. The runner's clock is virtual: an idle round moves it on, so the waits of the drain end without waiting.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import re
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import db
from .answer_user import NEAR_PREFIX, find_button, keyboard_label
from .cassette import _DATETIME, SimNet, SimStop, mark_now, normalize, offline

log = logging.getLogger("scripts.sim")
OWNER, USER = 1, 1001
CODE_DONE, CODE_WAITING, CODE_STALLED = 0, 3, 4
MAX_ROUNDS = 400
IDLE_JUMP = timedelta(minutes=20)  # an idle round moves the runner's clock past the drain's graces
MAX_JUMPS = 12
# rounds without a new call that still are no idle ones: the web stage of the campaign is ``searching`` (its pages
# and searches take a few ticks); a stage that never ends stops counting after this many and the run stalls
MAX_BUSY_ROUNDS = 60
_VOLATILE = re.compile(r"(_at|_until|_seen|^at)$")
_REAL_SLEEP = asyncio.sleep

# The bench's environment: the sim values of every key (never a real one), v3 sources on (ACQUISITION_MODE=apify),
# the deployment defaults of docker-compose.yml for the rest. A persona's ``env`` overrides any of them.
BASE_ENV: dict[str, str] = {
    "TELEGRAM_TOKEN": "sim-telegram-token",
    "TELEGRAM_OPERATOR_IDS": str(OWNER),
    "OPENROUTER_API_KEY": "sim-openrouter-key",
    "APIFY_TOKEN": "sim-apify-token",
    "ANTHROPIC_API_KEY": "sim-anthropic-key",
    "REDIS_URL": "",
    "BROWSER_SESSION_API_TOKEN": "",
    "WEB_SEARCH_IMPERSONATE": "off",  # curl_cffi would bypass httpx: plain httpx, answered by SimNet
    "WEB_SEARCH_RENDER_ENABLED": "false",
    "WEB_SEARCH_HOST_INTERVAL_SECONDS": "1",
    "ACQUISITION_MODE": "apify",
    "APIFY_IDEALISTA_ENABLED": "true",
    "APIFY_IDEALISTA_PROVIDER": "igolaizola",
    "APIFY_FOTOCASA_ENABLED": "true",
    "APIFY_HABITACLIA_ENABLED": "true",
    "APIFY_MILANUNCIOS_ENABLED": "true",
    "APIFY_FACEBOOK_COMMENTS_ENABLED": "true",
    # A source turned on needs a pinned build; the bench's runs are recorded, so any pinned value does.
    **{f"APIFY_{name}_ACTOR_BUILD": "0.0.1" for name in ("FACEBOOK", "FACEBOOK_COMMENTS", "FACEBOOK_REACTIONS",
                                                        "INSTAGRAM", "TIKTOK", "LINKEDIN_PEOPLE",
                                                        "LINKEDIN_COMPANIES")},
    "LEADS_PROVIDER": "apify",
    "INTAKE_BOTH_MODE": "true",
    "CAMPAIGN_INVESTOR_REPORT": "true",
    "CARD_EXTRA_FIELDS": "true",  # on in prod (docs/v2/CLIENT_DECISIONS.md:76): «Площадь», «Телефон», «Агентство»
    "CAMPAIGN_BUDGET_USD": "0.8",
    # Flags of Q4, Q5, Q8, Q9, Q10, Q11 (their own branches): ignored by the bot until those branches are merged.
    "DEDUP_WITHOUT_ADDRESS": "true",
    "IDEALISTA_SORT_BIGGEST": "true",
    "IDEALISTA_UNAMBIGUOUS_PLACE": "true",  # Q17; REDIS_URL is empty here: no geocoder, so the input stays the name
    "IDEALISTA_STATIC_CENTERS": "true",  # Q30: ...except for a city of the static list (its centre and circle)
    "RELEVANCE_SOFT_WISHES": "true",
    "SEARCH_QUERY_HYGIENE": "true",
    "ANALYSIS_LOCATION_FROM_TEXT": "true",
    "PORTAL_DEFAULT_POINT_GUARD": "true",  # Q12
    "PORTAL_FACTS_FROM_ROW": "true",  # Q16
    "PORTAL_DESCRIPTION_FULL": "true",  # Q18
    "PORTAL_RENTAL_TERM": "true",  # Q20
    "REVIEW_TOWNHOUSE_IS_HOUSE": "true",  # Q18
    "REVIEW_NON_RESIDENTIAL": "true",  # Q22
    "SEARCH_PLACE_LEVEL_FROM_SPEC": "true",  # Q11
    "ANALYSIS_AREA_TEXT_CHECK": "false",  # Q13: off until area_check review blockers (N4, N5) are fixed
    "DEDUP_DISTRICT_NOT_STREET": "true",  # Q15
    "OFFER_ALL_REASONS": "true",  # Q25
    "USER_PHRASE_PLACES": "true",  # Q28
    "PORTAL_DISTRICT_SEARCH": "true",  # Q26; REDIS_URL is empty here: no geocoder, so the input stays as before
    "DEDUP_HELD_FINDINGS": "true",  # Q27
    "OFFER_AFTER_SEARCH": "true",  # Q27
}
# Unset for the run whatever the shell says: no digest chat (the runner sends a campaign's cards).
UNSET = ("ANALYSIS_TELEGRAM_CHAT_ID", "SIM_DATABASE_URL")
# Set after the persona's env, whatever it says: curl_cffi would read pages past httpx (and past SimNet).
FORCED = {"WEB_SEARCH_IMPERSONATE": "off"}


def environment(persona: Persona, database_url: str) -> dict[str, str]:
    """The run's variables: ``BASE_ENV``, then the persona's ``env``, then ``FORCED`` and the database."""
    return {**BASE_ENV, **persona.env, **FORCED, "DATABASE_URL": database_url}


@contextlib.contextmanager
def sim_process(root: Path, env: dict[str, str]) -> Iterator[None]:
    """The process as a run needs it, and as it was afterwards: ``env`` set, ``UNSET`` and every ``*_PROXY`` /
    ``*_proxy`` removed (the bench has no network to reach through them), the working folder ``root`` (the services'
    settings read ``.env`` from it: never the repository's), ``random`` seeded, ``uuid.uuid4`` a counter, the
    ``costs`` ledger and budget of the caller kept."""
    from bot.utils import costs

    proxies = [k for k in os.environ if k.upper().endswith("_PROXY")]
    saved = {k: os.environ.get(k) for k in (*env, *UNSET, *proxies)}
    cwd, state = os.getcwd(), random.getstate()
    ledger, budget = costs.ledger(), costs.budget()
    real_uuid4 = uuid.uuid4
    counter = iter(range(1, 10**9))
    for key in (*UNSET, *proxies):
        os.environ.pop(key, None)
    os.environ.update(env)
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    random.seed(0)
    uuid.uuid4 = lambda: uuid.UUID(int=(0x5157 << 112) | next(counter), version=4)  # type: ignore[assignment]
    try:
        yield
    finally:
        uuid.uuid4 = real_uuid4  # type: ignore[assignment]
        costs.install(ledger, budget)
        random.setstate(state)
        os.chdir(cwd)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@dataclass
class Persona:
    """Who talks to the bot: the mode button, the first message, and how the card is handled."""

    name: str
    mode: str = "real_estate"
    task: str = ""
    voice: bool = False  # the first message arrives as a voice note (its transcript is ``task``)
    confirm_card: bool = False  # «Проверьте задачу» waits for an answer (else it is launched as shown)
    env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, value: str, mode: str | None = None) -> Persona:
        """A JSON / YAML file (``mode``, ``task``, ``voice``, ``confirm_card``, ``env``) or the first message itself."""
        path = Path(value)
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
            if path.suffix in (".yaml", ".yml"):
                import yaml

                data = yaml.safe_load(raw) or {}
            else:
                data = json.loads(raw)
            data.setdefault("name", path.stem)
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            known["env"] = {str(k): str(v) for k, v in (known.get("env") or {}).items()}
            persona = cls(**known)
        else:
            persona = cls(name="text", task=value)
        if mode:
            persona.mode = mode
        if persona.mode == "post_link":  # a post link is an investors' task (the link itself is the task)
            persona.mode = "investors"
        return persona


class Clock:
    def __init__(self) -> None:
        self.start = datetime.now(UTC)
        self.offset = timedelta()

    def now(self) -> datetime:
        return self.start + self.offset

    def advance(self, delta: timedelta) -> None:
        self.offset += delta
        mark_now(self.now())  # a day the clock reaches is «now» in the keys too


class LogTrap(logging.Handler):
    """The warnings and errors of the run, by stage (the bot logs what it swallows)."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[dict[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("bot."):
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken log call must not stop the run
            message = str(record.msg)
        item = {"stage": ".".join(record.name.split(".")[:2]), "level": record.levelname,
                "message": normalize(message)[:300]}
        if record.exc_info and record.exc_info[1] is not None:
            item["error"] = type(record.exc_info[1]).__name__
        self.records.append(item)


class AnswerStop(SimStop):
    pass


class PressError(ValueError):
    """An answer presses a button that is not on screen: ``answers/<run>.json`` is wrong."""


@dataclass
class Bench:
    root: Path
    run_id: str
    persona: Persona
    strict: bool = False
    database_url: str = ""
    net: SimNet = field(init=False)
    clock: Clock = field(default_factory=Clock)
    dialogue: list[dict[str, Any]] = field(default_factory=list)
    check_card: str | None = None
    keyboard: list[list[str]] = field(default_factory=list)  # the bottom keyboard on the user's screen now
    stage_errors: dict[str, list[str]] = field(default_factory=dict)
    status: str = "running"
    stop_reason: str = ""
    responder: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None  # tests: a synthetic model

    def __post_init__(self) -> None:
        self.net = SimNet(self.root, strict=self.strict, run_id=self.run_id, responder=self.responder)
        self.trap = LogTrap()
        self.answers_used = 0
        self.offers: list[dict[str, Any]] = []  # the runner's «Показать?» questions and what the user pressed
        self._offered: set[int] = set()  # their Telegram message ids, asked once

    # -- the run --

    async def run(self) -> tuple[int, dict[str, Any]]:
        self.clean_url, self.database_url = self.database_url, db.with_search_path(self.database_url)
        mark_now(self.clock.now())
        with sim_process(self.root, environment(self.persona, self.database_url)):
            bot_log = logging.getLogger("bot")
            level, propagate = bot_log.level, bot_log.propagate
            bot_log.addHandler(self.trap)
            if not bot_log.isEnabledFor(logging.WARNING):  # quiet run: the warnings go to the result only
                bot_log.setLevel(logging.WARNING)
                bot_log.propagate = False
            try:
                self._clear_pending()
                await db.reset(self.clean_url)
                with offline(self.net):
                    code = await self._run()
                return code, await self.result()
            finally:
                bot_log.removeHandler(self.trap)
                bot_log.setLevel(level)
                bot_log.propagate = propagate

    def _clear_pending(self) -> None:
        """This run's old pending files go: what is left after the pass is what it waits for now."""
        (self.root / "pending_answers" / f"{self.run_id}.json").unlink(missing_ok=True)
        for folder in ("pending", "pending_apify", "pending_web"):
            for path in (self.root / folder).glob("*.json"):
                with contextlib.suppress(ValueError, OSError):
                    if json.loads(path.read_text(encoding="utf-8")).get("run") == self.run_id:
                        path.unlink()

    async def _run(self) -> int:
        import asyncpg

        from bot.utils import costs

        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=8)
        await self.pool.execute("insert into browser_profiles(profile_name, platform, storage_locator, state) "
                                "values('facebook-main', 'facebook', 'volume:fb', 'ready')")
        costs.install(_SimLedger(self.pool), float(os.environ.get("CAMPAIGN_BUDGET_USD", "0.8")))
        try:
            self.campaign_id = await self._interview()
            if self.campaign_id is None:
                return self._finish()
            await self._campaign()
            return self._finish()
        except SimStop as stop:
            self.status, self.stop_reason = "waiting", f"{stop.kind}: {stop.path.relative_to(self.root.parent)}"
            return CODE_WAITING
        except Exception as exc:  # a setting or a stage that cannot start: the result says which
            log.exception("sim.run_failed")
            self.status, self.stop_reason = "error", normalize(f"{type(exc).__name__}: {exc}")[:500]
            return CODE_STALLED
        finally:
            for close in getattr(self, "_closers", []):  # the interview's stores, whatever stopped the run
                with contextlib.suppress(Exception):
                    await close()

    def _finish(self) -> int:
        if self.net.pending_llm or self.net.pending_apify or self.net.pending_web or self.net.blocked_apify:
            self.status = "waiting"
            return CODE_WAITING
        if self.status == "stalled":
            return CODE_STALLED
        self.status = "done"
        return CODE_DONE

    def _check(self) -> None:
        """A strict run's miss in a background task (an Apify run of the social stage) stops the run here."""
        for kind, folder, names in (("llm", "pending", self.net.pending_llm),
                                    ("apify", "pending_apify", self.net.pending_apify),
                                    ("web", "pending_web", self.net.pending_web)):
            if self.strict and names:
                raise SimStop(kind, self.root / folder / f"{names[0]}.json")

    # -- the interview (bot/control_plane/main.py) --

    async def _interview(self) -> str | None:
        from bot.campaign.offers import PostgresOfferDesk
        from bot.campaign.store import PostgresCampaignStore
        from bot.control_plane.access import AccessDesk, MemoryAccessStore
        from bot.control_plane.anthropic_interviewer import (
            AnthropicInterviewer,
            FallbackInterviewer,
        )
        from bot.control_plane.auto import PostgresSettingsStore
        from bot.control_plane.intake import PostgresIntakeStore
        from bot.control_plane.interviewer import Interviewer, OpenRouterInterviewer
        from bot.control_plane.models import IncomingMessage, Reply, TranscriptResult
        from bot.control_plane.service import ControlPlane
        from bot.control_plane.settings import ControlPlaneSettings
        from bot.control_plane.store import PostgresControlPlaneStore
        from bot.operators import OperatorSet
        from bot.orchestra.dispatcher import OrchestraDispatcher
        from bot.orchestra.models import ConfirmedCommand
        from bot.orchestra.store import PostgresOrchestraStore

        settings = ControlPlaneSettings.from_env()
        store = PostgresControlPlaneStore(settings.database_url)
        await store.connect()
        orchestra_store = PostgresOrchestraStore(settings.database_url, settings.safety_limits)
        await orchestra_store.connect()
        self._closers: list[Callable[[], Awaitable[Any]]] = [store.close, orchestra_store.close]
        operators = OperatorSet(settings.operator_user_ids, {USER: "user"})
        access_store = MemoryAccessStore()
        access_store.approved[USER] = (None, None, "user")

        async def send(chat_id: int, reply: Reply) -> None:
            self._say("bot", reply, chat_id=chat_id, source="notice")

        async def notify(chat_id: int, text: str) -> None:
            self._say("bot", Reply(text), chat_id=chat_id, source="orchestra")

        access = AccessDesk(access_store, operators, notify=send)
        orchestra = OrchestraDispatcher(orchestra_store, operator_ids=operators.controllers,
                                        notifier=notify, campaigns=PostgresCampaignStore(orchestra_store.pool),
                                        roles=operators)

        async def enqueue(envelope: Any) -> object:
            return await orchestra.enqueue(ConfirmedCommand(envelope.command, envelope.arguments, envelope.chat_id,
                                                            envelope.user_id, envelope.message_id,
                                                            envelope.confirmation_id, envelope.auto))

        interviewer: Interviewer | None = None
        if settings.openrouter_api_key:
            interviewer = OpenRouterInterviewer(api_key=settings.openrouter_api_key, model=settings.interview_model,
                                                timeout_seconds=settings.interview_timeout_seconds)
        if settings.architect_provider == "anthropic":
            claude = AnthropicInterviewer(api_key=settings.anthropic_api_key, model=settings.anthropic_interview_model,
                                          timeout_seconds=settings.anthropic_timeout_seconds)
            interviewer = FallbackInterviewer(claude, interviewer) if interviewer is not None else claude
        task = self.persona.task

        class Transcriber:  # the voice note's words are the persona's task (no audio in the bench)
            model, provider = "sim-transcript", "sim"

            async def transcribe(self, audio: bytes, *, filename: str) -> TranscriptResult:
                return TranscriptResult(task, "ru", 0.95, self.model)

        control = ControlPlane(settings, store, Transcriber(), enqueue, None, access, PostgresSettingsStore(store),
                               PostgresIntakeStore(store), offers=PostgresOfferDesk(orchestra_store.pool),
                               campaigns=PostgresCampaignStore(orchestra_store.pool), interviewer=interviewer)
        message_ids = iter(range(1, 10**6))

        def incoming(text: str | None = None, voice: bool = False) -> IncomingMessage:
            if voice:
                return IncomingMessage(USER, USER, next(message_ids), voice_file_id="v", voice_size=100,
                                       voice_duration_seconds=5)
            return IncomingMessage(USER, USER, next(message_ids), text=text)

        async def text(body: str) -> Reply | None:
            self._say("user", body)
            reply = await control.handle_text(incoming(body))
            if reply is not None:
                self._say("bot", reply)
            return reply

        async def press(data: str) -> Reply:
            self._say("user", f"[{data}]", press=data)
            reply = await control.handle_callback(USER, data, chat_id=USER)
            self._say("bot", reply)
            return reply

        async def tap(label: str) -> Reply | None:  # a bottom-keyboard tap: Telegram sends the label as a message
            self._say("user", label, press=label)
            reply = await control.handle_text(incoming(label))
            if reply is not None:
                self._say("bot", reply)
            return reply

        async def download() -> bytes:
            return b"OggS"

        reply = await text("/start")
        reply = await press(f"mode:{self.persona.mode}")
        if self.persona.voice:
            self._say("user", "[voice] " + task)
            reply = await control.handle_voice(incoming(voice=True), download)
            if reply is not None:
                self._say("bot", reply)
        else:
            reply = await text(task)
        launched = await self._talk(control, reply, text, press, tap)
        self._user_text = text
        if not launched:
            self.status, self.stop_reason = "stalled", "the interview did not reach «Запустить»"
            return None
        while await orchestra.process_once():
            pass
        campaign_id = await self.pool.fetchval("select id::text from campaigns order by created_at desc limit 1")
        if campaign_id is None:
            self.status, self.stop_reason = "stalled", "no campaign was created"
        self.control = control
        return campaign_id

    async def _talk(self, control: Any, reply: Any, text: Callable[[str], Awaitable[Any]],
                    press: Callable[[str], Awaitable[Any]], tap: Callable[[str], Awaitable[Any]]) -> bool:
        """The interview after the task: each question gets the next answer, up to «Проверьте задачу» and its launch
        (True when launched)."""
        from bot.control_plane.intake import key_command

        launched = False
        for _ in range(60):
            self._check()
            draft = await control.intake.store.get(USER)
            if draft is None:
                break
            if draft.step == "summary":
                self.check_card = reply.text if reply is not None else None
                if not self.persona.confirm_card:  # launched as shown: no answer is read, the next one is not its
                    reply = await press("task:launch")
                    launched = True
                    break
            answer = self._answer(reply, draft.step == "summary")
            if draft.step == "summary" and answer is None:  # confirmed: «Запустить» (or no answer left)
                reply = await press("task:launch")
                launched = True
                break
            if answer is None:
                self._ask(reply)
            if "press" in answer:
                wanted = str(answer["press"])
                label = None if _has_button(reply, wanted) else keyboard_label(self.keyboard, wanted)
                if label is None:
                    reply = await press(_callback(reply, wanted))
                    continue
                reply = await tap(label)
                if draft.step == "summary" and key_command(label) == "запустить":
                    launched = True
                    break
            else:
                reply = await text(str(answer.get("text", "")))
        return launched

    def _say(self, who: str, reply: Any, *, chat_id: int = USER, source: str = "intake", press: str | None = None
             ) -> None:
        if who == "user":
            item: dict[str, Any] = {"from": "user", "text": reply}
            if press:
                item["press"] = press
        else:
            item = {"from": "bot", "text": reply.text,
                    "buttons": [{"text": b.text, "data": b.callback_data} for b in reply.buttons if b.callback_data]}
            keyboard = getattr(reply, "keyboard", None)
            # None: the keyboard stays as it is; []: it is removed. Inline buttons win: prod sends only them
            # (bot/control_plane/main.py:_markup), the bottom keyboard of such a reply is never shown.
            if keyboard is not None and not reply.buttons:
                item["keyboard"] = [list(row) for row in keyboard]
                if chat_id == USER:
                    self.keyboard = item["keyboard"]
            if chat_id != USER:
                item["chat_id"] = chat_id
            item["source"] = source
        self.dialogue.append(item)

    def _answers(self) -> list[dict[str, Any]]:
        path = self.root / "answers" / f"{self.run_id}.json"
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("answers", data) if isinstance(data, dict) else data)

    def _answer(self, reply: Any, card: bool) -> dict[str, Any] | None:
        """The next answer of the agent-user (``sim/answers/<run>.json``), or None (the card: launch; a question:
        pending)."""
        answers = self._answers()
        if self.answers_used >= len(answers):
            if card and self.persona.confirm_card and not self._card_confirmed(answers):
                self._ask(reply, card=True)
            return None
        answer = answers[self.answers_used]
        self.answers_used += 1
        if isinstance(answer, str):
            answer = {"text": answer}
        if card and answer.get("press") in ("task:launch", "Запустить"):
            return None
        return answer

    @staticmethod
    def _card_confirmed(answers: list[Any]) -> bool:
        return any(isinstance(a, dict) and a.get("press") in ("task:launch", "Запустить") for a in answers)

    def _ask(self, reply: Any, card: bool = False, offer: bool = False) -> None:
        """Stop for the agent-user's answer (``AnswerStop``), or for the model's when one is missing: after a miss
        the interview went on with the rules, the question may be theirs. ``offer``: the runner's «Показать?»."""
        if self.net.pending_llm and not offer:
            raise SimStop("llm", self.root / "pending" / f"{self.net.pending_llm[0]}.json")
        path = self.root / "pending_answers" / f"{self.run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "kind": "answer", "run": self.run_id, "index": self.answers_used, "card": card, "offer": offer,
            "question": reply.text if reply is not None else "",
            "buttons": [{"text": b.text, "data": b.callback_data} for b in (reply.buttons if reply else ())
                        if b.callback_data],
            "keyboard": self.keyboard,
            "dialogue": self.dialogue, "persona": self.persona.task,
            "answer_to": f"sim/answers/{self.run_id}.json",
            "answer_format": '{"answers": [{"text": "..."} | {"press": "<button text or data>"}, ...]} '
                             '(append one; the card: {"press": "task:launch"})',
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        raise AnswerStop("answer", path)

    # -- the campaign (bot/campaign/runner.py:main and the workers beside it) --

    async def _campaign(self) -> None:
        from dataclasses import replace

        from bot.agents.recorder import PostgresRecorder
        from bot.analysis_pipeline import main as analysis
        from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer
        from bot.analysis_pipeline.pipeline import AnalysisPipeline
        from bot.analysis_pipeline.settings import AnalysisSettings
        from bot.analysis_pipeline.store import PostgresAnalysisStore
        from bot.campaign import runner as service
        from bot.campaign.discovery import FacebookDiscovery, PostgresDiscoveryStore
        from bot.campaign.runs import PostgresRunStore
        from bot.campaign.settings import CampaignRunnerSettings
        from bot.campaign.store import PostgresCampaignStore
        from bot.facebook_collector.reader import FacebookGroupReader

        pool = self.pool
        settings = CampaignRunnerSettings()
        campaigns = PostgresCampaignStore(pool)
        discovery: Any
        if os.environ.get("SIM_FACEBOOK", "none") == "browser":
            # The Browser Session Manager is the outside world too: a SimBrowser (pages from sim/browser/, else an
            # empty Facebook search) through the constructor's own browser and reader, as main() wires a token.
            browser = SimBrowser(self.root, self.net)
            reader = FacebookGroupReader(browser, max_posts=settings.discovery_max_posts,  # type: ignore[arg-type]
                                         timeout_seconds=settings.discovery_group_timeout_seconds)
            discovery = FacebookDiscovery(campaigns, PostgresDiscoveryStore(pool), browser,  # type: ignore[arg-type]
                                          reader, acquisition_mode=settings.acquisition_mode, sleep=_fast_sleep)
        else:
            discovery = NoSearchDiscovery(campaigns, FacebookDiscovery(campaigns, PostgresDiscoveryStore(pool), None,
                                                                       None, acquisition_mode="apify"))
        web = await service._web_stage(campaigns, pool, settings)
        judge = settings.relevance_judge()
        final_reporter = settings.final_reporter()
        comments, lead_judge = service._comment_worker(settings, pool)
        post_reads, post_closers = service._post_worker(settings, pool, lead_judge)
        apify_tasks = service._apify_tasks(settings)
        reach, reach_closers = service._reach_worker(settings, pool, fetcher=getattr(web[0], "fetcher", None)
                                                     if web else None, tasks=apify_tasks)
        config = settings.runner_config()
        if comments is None:
            config = replace(config, comment_leads="off")
        if post_reads is not None:
            config = replace(config, post_reads=True)
        store = PostgresRunStore(pool, settings.safety_limits(), dead_days=settings.facebook_group_dead_days,
                                 join_notices=settings.join_notices)
        runner = service.CampaignRunner(campaigns, store, service.TelegramMessenger(settings.telegram_token),
                                        discovery, config=config, now=self.clock.now, owner_ids=settings.owner_ids(),
                                        web=web[0].store if web else None, relevance=judge,
                                        recorder=PostgresRecorder(pool), final_report=final_reporter)
        _social, generator = service._social_worker(settings, pool, campaigns)
        apify_social, apify_generator = service._apify_social_worker(settings, pool, campaigns, apify_tasks)
        analysis_settings = AnalysisSettings()
        analysis_store = PostgresAnalysisStore(self.database_url, exclude_platforms=analysis_settings.excluded_platforms)
        await analysis_store.connect()
        pipeline = AnalysisPipeline(OpenRouterAnalyzer(
            analysis_settings.openrouter_api_key, analysis_settings.openrouter_model,
            timeout_seconds=analysis_settings.timeout_seconds, invalid_retry=analysis_settings.invalid_retry,
            fallback_model=analysis_settings.fallback_model))
        facebook = self._facebook_runner()
        for part in (web, judge, final_reporter, comments, post_reads, apify_tasks, reach, runner, apify_social,
                     pipeline, facebook):
            _fast_sleeps(part)

        async def analyse() -> None:
            await analysis.run_once(analysis_store, pipeline, settings=analysis_settings)

        stages: list[tuple[str, Callable[[], Awaitable[Any]]]] = []
        if web is not None:
            stages.append(("web", web[0].tick))
        if apify_social is not None:
            stages.append(("social_apify", apify_social.tick))
            await apify_social.recover()
        if facebook is not None:
            stages.append(("facebook", facebook.step))
        if comments is not None:
            stages.append(("leads", comments.step))
        if post_reads is not None:
            stages.append(("post_leads", post_reads.step))
        if reach is not None:
            stages.append(("reach", reach.tick))
        stages += [("analysis", analyse), ("runner", runner.tick)]
        self.stages = [name for name, _ in stages]
        await runner.recover()
        try:
            await self._loop(stages, campaigns, apify_tasks)
        finally:
            if apify_tasks is not None:
                await apify_tasks.aclose(timeout=1)
            for close in ([*(web[2] if web else []), *reach_closers, *post_closers]
                          + [c.aclose for c in (generator, apify_generator, lead_judge, judge, final_reporter) if c]):
                with contextlib.suppress(Exception):
                    await close()
            await analysis_store.close()

    def _facebook_runner(self) -> Any:
        """facebook-runner (bot/facebook_collector/runner.py) for the campaign's Facebook windows, when it builds."""
        from bot.facebook_collector.main import run_batch_with
        from bot.facebook_collector.runner import CollectorRunner, PostgresLaunchStore
        from bot.facebook_collector.settings import FacebookCollectorSettings

        if os.environ.get("FEATURE_APIFY_FACEBOOK", "").lower() not in ("1", "true", "yes", "on"):
            return None  # without Apify a batch needs the browser: no Facebook batches in the bench
        try:
            # The token is only checked for its length: the Apify provider never calls the browser.
            settings = FacebookCollectorSettings(BROWSER_SESSION_API_TOKEN="sim-browser-token-never-used")
        except Exception as exc:  # noqa: BLE001 - the bench goes on without Facebook batches, and says so
            self.stage_errors.setdefault("facebook", []).append(f"settings: {type(exc).__name__}")
            return None
        pool = self.pool
        return CollectorRunner(PostgresLaunchStore(pool), lambda batch_id: run_batch_with(pool, settings, batch_id),
                               sleep=_fast_sleep)

    async def _loop(self, stages: list[tuple[str, Callable[[], Awaitable[Any]]]], campaigns: Any,
                    apify_tasks: Any) -> None:
        from bot.campaign.models import TERMINAL_STATES

        idle = jumps = after_end = busy = 0
        for _ in range(MAX_ROUNDS):
            before = self._progress()
            state_before = await self.pool.fetchval("select state from campaigns where id = $1::uuid",
                                                    self.campaign_id)
            for name, step in stages:
                try:
                    await step()
                except Exception as exc:  # noqa: BLE001 - one stage's failure is a result, the others go on
                    self.stage_errors.setdefault(name, []).append(normalize(f"{type(exc).__name__}: {exc}")[:300])
                if apify_tasks is not None:
                    await apify_tasks.wait()
                self._check()
            if await self._offer():  # the user answered «Показать?»: the approved ones stream in the next rounds
                idle = after_end = 0
            campaign = await campaigns.get(self.campaign_id)
            if campaign is None:
                break
            if campaign.state in TERMINAL_STATES:
                after_end += 1
                if after_end >= 2:  # the terminal step (summary, report) ran once more for the edits
                    return
            moved = self._progress() != before or campaign.state != state_before
            if not moved and busy < MAX_BUSY_ROUNDS and await self._web_searching():
                busy += 1
                moved = True
            if moved:
                idle = 0
                await self._advance(timedelta(seconds=10))
                continue
            idle += 1
            if idle >= 2:
                if jumps >= MAX_JUMPS:
                    break
                await self._advance(IDLE_JUMP)
                jumps += 1
                idle = 0
        self.status, self.stop_reason = "stalled", "the campaign did not end"

    async def _web_searching(self) -> bool:
        """The campaign's web stage is still at work (``web_search_runs.state = 'searching'``)."""
        return bool(await self.pool.fetchval("select exists(select 1 from web_search_runs where campaign_id = $1::uuid "
                                             "and state = 'searching')", self.campaign_id))

    async def _advance(self, delta: timedelta) -> None:
        """The runner's clock and the database's ``now()`` (``db.set_clock``) move together: a ``finished_at`` the
        trigger stamps is on the runner's time, a report is not cut by ``SUMMARY_WINDOW`` after the jumps."""
        self.clock.advance(delta)
        await db.set_clock(self.pool, self.clock.offset)

    def _progress(self) -> tuple[int, ...]:
        net = self.net
        return net.calls, len(net.pending_llm), len(net.pending_apify), len(net.pending_web)

    async def _offer(self) -> bool:
        """The runner's «Есть ещё похожие варианты (…). Показать?» (``near:`` buttons, ``bot/campaign/offers.py``)
        is the user's question like the interview's: the next answer of ``sim/answers/<run>.json`` presses its
        button through the control plane, else the run stops for it (pending ``offer: true``, slot ``near``).
        While a model or an Apify answer is missing the pass goes on collecting them: the question waits for the
        next pass. True when an answer was given."""
        from bot.control_plane.models import Button, Reply

        answered = False
        for message in self.net.telegram.messages:
            buttons = message.get("buttons") or []
            if (message["chat_id"] != USER or message["message_id"] in self._offered or message.get("deleted")
                    or not any(str(b.get("data") or "").startswith(NEAR_PREFIX) for b in buttons)):
                continue
            if self.answers_used >= len(self._answers()) and (self.net.pending_llm or self.net.pending_apify
                                                               or self.net.pending_web):
                return answered
            self._offered.add(message["message_id"])
            reply = Reply(message["text"], tuple(Button(b["text"], callback_data=b["data"]) for b in buttons
                                                 if b.get("data")))
            self._say("bot", reply, source="offer")
            index = self.answers_used
            answer = self._answer(reply, False)
            if answer is None:
                self._ask(reply, offer=True)
            record: dict[str, Any] = {"question": reply.text, "index": index, "tag": self._logged_tag(index)}
            if "press" in answer:
                data = _callback(reply, str(answer["press"]))
                self._say("user", f"[{data}]", press=data)
                self._say("bot", await self.control.handle_callback(USER, data, chat_id=USER), source="offer")
                record["press"] = data
                record["bucket"] = data.split(":")[2] if data.count(":") >= 3 else None
            else:  # a typed answer goes to the chat as any message would
                await self._user_text(str(answer.get("text", "")))
                record["text"] = str(answer.get("text", ""))
            self.offers.append(record)
            answered = True
        return answered

    def _logged_tag(self, index: int) -> str | None:
        """The ``log`` tag of answer ``index`` (``default``: the persona had no ``near`` reply, «Нет» was pressed)."""
        path = self.root / "answers" / f"{self.run_id}.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        log_ = data.get("log", []) if isinstance(data, dict) else []
        return next((line.get("tag") for line in log_ if line.get("index") == index), None)

    # -- the result --

    async def result(self) -> dict[str, Any]:
        pool = getattr(self, "pool", None)
        campaign_id = getattr(self, "campaign_id", None)
        data: dict[str, Any] = {
            "run": self.run_id, "status": self.status, "stop_reason": self.stop_reason,
            "persona": {"name": self.persona.name, "mode": self.persona.mode, "task": self.persona.task,
                        "voice": self.persona.voice},
            "dialogue": self.dialogue,
            "questions": _questions(self.dialogue),
            "check_card": self.check_card,
            "offers": self.offers,  # «Показать?» after the cards: the press and its log tag (``default``: «Нет»)
        }
        if pool is not None and campaign_id is not None:
            data.update(await self._campaign_data(pool, campaign_id))
        data["pending"] = {"llm": sorted(set(self.net.pending_llm)), "apify": sorted(set(self.net.pending_apify)),
                           "apify_blocked": sorted(set(self.net.blocked_apify)),
                           "web": sorted(set(self.net.pending_web))}
        data["llm_calls"] = _count(c["stage"] for c in self.net.llm_calls)
        data["offline"] = _count(f"{o['method']} {o['host']}{o['path']}" for o in self.net.offline)
        data["unhandled"] = sorted(set(self.net.unhandled))
        data["errors"] = {"stages": self.stage_errors, "logged": _group(self.trap.records)}
        if pool is not None:
            await pool.close()
        return _stable(data)

    async def _campaign_data(self, pool: Any, campaign_id: str) -> dict[str, Any]:
        messages = [m for m in self.net.telegram.messages if m["chat_id"] == USER]
        by_id = {m["message_id"]: m for m in messages}
        campaign = _row(await pool.fetchval("select to_jsonb(c) from campaigns c where id = $1::uuid", campaign_id))
        findings = []
        for row in await pool.fetch(
                """select to_jsonb(cf) as cf, to_jsonb(f) as f from campaign_findings cf
                     left join findings f on f.id = cf.finding_id where cf.campaign_id = $1::uuid
                    order by cf.card_number nulls last, cf.created_at, cf.finding_id""", campaign_id):
            link, finding = _row(row["cf"]), _row(row["f"]) or {}
            payload = finding.get("structured_payload") or {}
            message = by_id.get(link.get("telegram_message_id"))
            findings.append({"bucket": link.get("bucket"), "state": link.get("state"), "why": link.get("why"),
                             "hold_reason": link.get("hold_reason"), "card_number": link.get("card_number"),
                             "duplicate_of": link.get("duplicate_of"),
                             "url": payload.get("original_post_link") or payload.get("url"),
                             "card": message["text"] if message else None, "payload": payload})
        leads = [_row(r) for r in _list(await pool.fetchval(
            "select coalesce(jsonb_agg(to_jsonb(l) order by l.id), '[]') from investor_leads l "
            "where l.campaign_id = $1::uuid", campaign_id))]
        delivered = {r["profile_key"]: by_id.get(r["telegram_message_id"], {}).get("text") for r in await pool.fetch(
            "select profile_key, telegram_message_id from campaign_lead_deliveries where campaign_id = $1::uuid",
            campaign_id)}
        for lead in leads:
            lead["card"] = delivered.get(lead.get("profile_key"))
        reach = [_row(r) for r in _list(await pool.fetchval(
            "select coalesce(jsonb_agg(to_jsonb(r) order by r.platform, r.url), '[]') from reach_contacts r "
            "where r.campaign_id = $1::uuid", campaign_id))]
        for contact in reach:  # a reach contact sent to the chat is a lead card too (key ``reach:<url_key>``)
            contact["card"] = delivered.get(f"reach:{contact.get('url_key')}")
        costs = [_row(r) for r in _list(await pool.fetchval(
            "select coalesce(jsonb_agg(to_jsonb(c) order by c.id), '[]') from campaign_costs c "
            "where c.campaign_id = $1::uuid or c.campaign_id is null", campaign_id))]
        for cost in costs:
            cost.pop("id", None)
        cards = ({f["card"] for f in findings if f["card"]} | {lead["card"] for lead in leads if lead.get("card")}
                 | {c["card"] for c in reach if c.get("card")})
        return {
            "campaign": {k: campaign.get(k) for k in ("id", "state", "stop_reason", "source_text", "plan", "spec",
                                                      "acquisition_provider")},
            "user_saw": [{k: m[k] for k in ("message_id", "text", "buttons", "edits", "deleted")} for m in messages],
            "cards": [f for f in findings if f["card"]],
            "findings": findings,
            "leads": leads,  # investor_leads (comments under posts) with the card when it was sent
            "reach_contacts": reach,  # investors found across platforms (reach), with the card when it was sent
            "report": [m["text"] for m in messages if "Отчёт" in m["text"] and m["text"] not in cards],
            "campaign_costs": costs,
            "costs_total_usd": round(sum(float(c.get("cost_usd") or 0) for c in costs if c.get("kind") == "cost"), 6),
        }


class NoSearchDiscovery:
    """The Facebook discovery without a browser (``SIM_FACEBOOK=none``, the default): the groups the task names are
    queued as in apify mode; with none, Facebook adds nothing and the campaign goes on with its other sources (the
    runner finds no window and drains), instead of waiting at «поиск групп не настроен» for ever."""

    def __init__(self, campaigns: Any, real: Any) -> None:
        self.campaigns, self.real = campaigns, real

    async def run(self, campaign_id: str) -> Any:
        from bot.campaign.discovery import DiscoveryRefused

        try:
            return await self.real.run(campaign_id)
        except DiscoveryRefused as exc:
            if str(exc) != "no_groups_no_browser":
                raise
        campaign = await self.campaigns.get(campaign_id)
        if campaign is not None and campaign.state != "running":
            await self.campaigns.set_state(campaign_id, "running", "campaign_discovery")
        return None


class SimBrowser:
    """The Browser Session Manager for the campaign's Facebook discovery: a page recorded in
    ``sim/browser/<hash of the url>.json`` (``{"url", "title", "text", "group_links": [{"url", "name"}], ...}``), else
    an empty page (Facebook found nothing), listed in ``offline``."""

    def __init__(self, root: Path, net: SimNet) -> None:
        self.root, self.net = root, net

    async def acquire(self, profile_id: str, profile_name: str, persisted_state: str, *, platform: str = "facebook"
                      ) -> Any:
        from bot.facebook_collector.browser import BrowserLease

        return BrowserLease(profile_id, "sim-lease")

    async def snapshot(self, lease: Any, url: str, timeout_ms: int, *, scrolls: int = 0) -> dict[str, Any]:
        from .cassette import digest

        key = digest(url, 16)
        path = self.root / "browser" / f"{key}.json"
        if path.is_file():
            self.net.hits["http"] += 1
            return dict(json.loads(path.read_text(encoding="utf-8")))
        self.net.offline.append({"host": "browser", "method": "SNAPSHOT", "path": url, "key": key})
        return {"url": url, "title": "", "text": "", "group_links": [], "articles": []}

    async def release(self, lease: Any, next_state: str = "READY") -> None:
        return None

    async def health(self) -> bool:
        return True


class _SimLedger:
    """``PostgresLedger`` that books every LLM answer under the ``claude_cassette`` provider (cost 0)."""

    def __init__(self, pool: Any) -> None:
        from bot.utils.costs import PostgresLedger

        self._ledger = PostgresLedger(pool)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ledger, name)

    async def add(self, entry: Any) -> None:
        from dataclasses import replace

        if entry.stage == "llm" and entry.kind == "cost":
            entry = replace(entry, provider="claude_cassette", cost_usd=0.0)
        await self._ledger.add(entry)


async def _fast_sleep(_seconds: float = 0, *_args: Any, **_kwargs: Any) -> None:
    await _REAL_SLEEP(0)


def _fast_sleeps(root: Any) -> None:
    """Every ``sleep`` / ``_sleep`` of the stage objects that is ``asyncio.sleep`` becomes an instant one: Apify's
    10 s billing pause and the backoffs pass at once (the bench has no clock to wait on)."""
    seen: set[int] = set()

    def walk(obj: Any, depth: int) -> None:
        if obj is None or depth > 6 or id(obj) in seen:
            return
        seen.add(id(obj))
        if isinstance(obj, list | tuple | set):
            for item in obj:
                walk(item, depth + 1)
            return
        if isinstance(obj, dict):
            for item in obj.values():
                walk(item, depth + 1)
            return
        if not type(obj).__module__.startswith(("bot.", "scripts.")):
            return
        names = list(getattr(obj, "__dict__", {}))
        for klass in type(obj).__mro__:
            names += [n for n in getattr(klass, "__slots__", ()) if isinstance(n, str)]
        for name in names:
            try:
                value = getattr(obj, name)
            except AttributeError:
                continue
            if name in ("sleep", "_sleep") and value is _REAL_SLEEP:
                with contextlib.suppress(AttributeError, TypeError):
                    object.__setattr__(obj, name, _fast_sleep)
            else:
                walk(value, depth + 1)

    walk(root, 0)


def _has_button(reply: Any, wanted: str) -> bool:
    return any(wanted in (b.text, b.callback_data) for b in getattr(reply, "buttons", ()))


def _callback(reply: Any, wanted: str) -> str:
    """A button by its text or its data (the agent-user may write either), or by meaning. A press of no button on
    screen is the run's error (code 4), never a callback the user could not send."""
    buttons = getattr(reply, "buttons", ())
    for button in buttons:
        if wanted in (button.text, button.callback_data):
            return button.callback_data or wanted
    # a persona's data or words for a button the AI interviewer labelled itself (``answer_user.find_button``)
    found = find_button([{"text": b.text, "data": b.callback_data} for b in buttons], wanted)
    if found is not None:
        return found["data"] or wanted
    raise PressError(f"the answer presses {wanted!r}: no such button under {getattr(reply, 'text', '')[:80]!r}")


def _questions(dialogue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pairs, question = [], None
    for item in dialogue:
        if item["from"] == "bot" and item.get("source") in ("intake", "offer"):
            question = item
        elif item["from"] == "user" and question is not None:
            pairs.append({"question": question["text"], "answer": item["text"]})
            question = None
    return pairs


def _row(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    data = json.loads(value) if isinstance(value, str) else dict(value)
    return {k: v for k, v in data.items() if not _VOLATILE.search(k)}


def _list(value: Any) -> list[Any]:
    return list(json.loads(value) if isinstance(value, str) else value or [])


def _count(items: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return dict(sorted(counts.items()))


def _group(records: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for record in records:
        grouped.setdefault(record["stage"], []).append({k: v for k, v in record.items() if k != "stage"})
    return grouped


def _stable(value: Any) -> Any:
    """Timestamps out of the result (a replay must give the same file); ids stay, they come from a sequence."""
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if not _VOLATILE.search(str(k))}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    if isinstance(value, str):
        return _DATETIME.sub("<time>", value)
    return value
