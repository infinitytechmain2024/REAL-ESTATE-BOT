"""Campaign runner: drive every campaign from plan to the last window, durably.

``python -m bot.campaign.runner`` polls the database; each tick advances every
open campaign by one step, oldest first, and all state lives in PostgreSQL
(campaigns, campaign_groups, campaign_runs, campaign_windows,
campaign_findings), so a restart continues where it stopped.

* ``planned`` (or paused by a discovery challenge, once the Facebook profile is
  ``ready`` again): the challenge breaker is checked, then
  ``FacebookDiscovery.run`` finds and queues groups.
* ``running``: the next window of at most 20 queued groups becomes ONE ordinary
  Facebook batch through ``plan_facebook_batch`` (the same quotas, breakers and
  profile/source checks as ``/run``) with a launch request, so facebook-runner
  -- the only process reading Facebook groups -- executes it. The runner only
  watches the batch. When it ends, its groups become collected (or skipped),
  a cooldown passes, and the next window follows, up to ``limits.max_windows``.
* A batch that stops at a challenge pauses the campaign (``paused_verification``)
  until the verification flow requeues it; nothing is bypassed or retried here.
* Findings from the campaign's batches are sent to the campaign chat one by
  one, exactly once (``campaign_findings``); the analysis digest skips them.
  Each finding is filed in one bucket (``tolerance``): exact ones are sent at
  once; similar and other ones are held and sent only after the requester
  answers «Одобрить» to the once-per-bucket question (``offers``), which the
  Telegram control plane records in ``campaign_offers``.
* One Telegram message per campaign shows the live status; it is edited only
  when the text changes. Owners (TELEGRAM_OPERATOR_IDS) see the technical line;
  anyone else sees only the short labels of ``status_text`` (no ids, windows or
  group counts).
* The website stage (``bot.web_search``) runs beside all of this in the same
  service; while it is still searching the campaign does not complete, and
  its site shows in the status («Ищу на сайте <host>…» for non-owners).
* Each sent Facebook post is queued for one read of its comments (``leads``):
  the people who show interest as an investor or buyer are only stored. An
  investor search (vertical investors or both) sends each stored person of its
  city once, with their profile link, a preview and a recommendation.
* After the last window (and the web stage) the runner waits (bounded) for
  analysis, then completes the campaign. A campaign cancelled from outside has its in-flight
  batch cancelled through the ordinary batch cancel.

Only one campaign uses Facebook at a time: no discovery or window starts
while another campaign has an open window or is discovering.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from collections.abc import Awaitable, Callable, Collection, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from bot.agents.recorder import PostgresRecorder, Recorder, record_of
from bot.analysis_pipeline.cards import CardTask, also_on, render_card
from bot.utils import costs

from . import offers, people
from .dedup import Listing, listing_of, same_object, site_of
from .final_report import FinalReporter, task_title
from .leads import MODES as COMMENT_MODES
from .leads import is_facebook_post, person_card
from .models import TERMINAL_STATES, WINDOW_SIZE, Campaign
from .reach import contact_card, contact_extras
from .relevance import (
    Relevance,
    RelevanceJudge,
    finding_data,
    reason_category,
    review_match,
    task_data,
)
from .runs import (
    TERMINAL_BATCH_STATES,
    ClusterHead,
    RunState,
    RunStore,
    SentFinding,
    StreamFinding,
    Window,
)
from .status_text import (
    LAYER_NAMES,
    LIMIT_REASONS,
    campaign_label,
    checking_line,
    group_line,
    is_checking_line,
    is_live_line,
    reach_line,
    site_line,
    social_line,
)
from .status_text import (
    SEARCHING as USER_SEARCHING,
)
from .store import CampaignStore
from .summary import summary_text
from .tolerance import (
    HELD_BUCKETS,
    Match,
    Request,
    area_text,
    classify,
    money,
    price_of,
    request_for,
)

log = logging.getLogger(__name__)
RELEVANCE_PAUSE_SECONDS = 60
MAX_TRACKED_MISSES = 5000

ACTOR = "campaign:runner"
SEARCHING = "Сейчас: поиск групп Facebook"
ANALYSIS = "Сейчас: анализ"
VERIFY = "Нужна verification"
PROFILE_BUSY = "Ожидание: профиль Facebook занят"
QUEUE_BUSY = "Ожидание: Facebook занят другой кампанией"
STOPPED = "Кампания остановлена"
MAX_MESSAGE_CHARS = 3900
SUMMARY_STATES = frozenset({"completed", "cancelled"})
SUMMARY_WINDOW = timedelta(hours=2)


# --- Telegram -------------------------------------------------------------------------


class MessageGone(RuntimeError):
    """The status message can no longer be edited (deleted, too old): send a new one."""


class TelegramError(RuntimeError):
    def __init__(self, status: int, description: str) -> None:
        super().__init__(f"telegram {status}: {description}")
        self.status, self.description = status, description


class Messenger(Protocol):
    """``parse_mode`` ("HTML") only for the status message, whose links are HTML; every other message is plain text."""

    async def send(self, chat_id: int, text: str, *, parse_mode: str | None = None) -> int: ...
    async def edit(self, chat_id: int, message_id: int, text: str, *, parse_mode: str | None = None) -> None: ...
    async def send_buttons(self, chat_id: int, text: str, buttons: Sequence[tuple[str, str]]) -> int:
        """A message with one row of inline callback buttons: (label, callback data)."""
        ...
    async def delete(self, chat_id: int, message_id: int) -> None: ...


def _fit(text: str, parse_mode: str | None) -> tuple[str, str | None]:
    """The text Telegram accepts (4096 chars). HTML is never cut mid-tag: a too-long one is sent as plain text."""
    if len(text) <= 4000:
        return text, parse_mode
    if parse_mode:
        text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return text[:4000], None


class TelegramMessenger:
    """Bot API calls: plain text (no HTML parsing of finding text) unless ``parse_mode`` is given."""

    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=20)

    async def send(self, chat_id: int, text: str, *, parse_mode: str | None = None) -> int:
        text, parse_mode = _fit(text, parse_mode)
        body: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if parse_mode:
            body["parse_mode"] = parse_mode
        data = await self._call("sendMessage", body)
        return int(data["result"]["message_id"])

    async def send_buttons(self, chat_id: int, text: str, buttons: Sequence[tuple[str, str]]) -> int:
        markup = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]}
        data = await self._call("sendMessage", {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True,
                                                "reply_markup": markup})
        return int(data["result"]["message_id"])

    async def edit(self, chat_id: int, message_id: int, text: str, *, parse_mode: str | None = None) -> None:
        text, parse_mode = _fit(text, parse_mode)
        body: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text,
                                "disable_web_page_preview": True}
        if parse_mode:
            body["parse_mode"] = parse_mode
        try:
            await self._call("editMessageText", body)
        except TelegramError as exc:
            detail = exc.description.lower()
            if "message is not modified" in detail:
                return
            if any(k in detail for k in ("message to edit not found", "message can't be edited", "message_id_invalid")):
                raise MessageGone(exc.description) from exc
            raise

    async def delete(self, chat_id: int, message_id: int) -> None:
        """Delete a message; one that is already gone (or too old to delete) is fine."""
        try:
            await self._call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})
        except TelegramError as exc:
            detail = exc.description.lower()
            if not any(k in detail for k in ("message to delete not found", "message can't be deleted")):
                raise

    async def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(f"{self._base}/{method}", json=body)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400 or not data.get("ok"):
            raise TelegramError(response.status_code, str(data.get("description", "")))
        return data

    async def aclose(self) -> None:
        await self._client.aclose()


# --- the runner -------------------------------------------------------------------------


class Discovery(Protocol):
    async def run(self, campaign_id: str) -> object: ...


class WebProgress(Protocol):
    """The website stage's progress (``bot.web_search.store``): ``active``, ``host``, owners' ``line``."""

    async def web_status(self, campaign_id: str) -> Any: ...


UNVERIFIED_CAP = "Не проверено ИИ: лимит проверок исчерпан"
UNVERIFIED_FAILED = "Не проверено ИИ: сбой проверки"
UNVERIFIED_BUDGET = "Не проверено ИИ: бюджет прогона исчерпан"
# Held findings whose owner-facing note is stored (``hold_reason``): the AI check could not run or could not confirm.
NOTED_WHYS = frozenset({"unverified", "ai_failed", "cost_cap"})


@dataclass(frozen=True)
class RunnerConfig:
    window_cooldown_seconds: float = 120
    analysis_grace_seconds: float = 600
    refusal_retry_seconds: float = 300
    web_status_seconds: float = 10  # the web progress line is edited at most this often
    repost_cards: int = 5  # the status message moves below the cards after this many cards sent since it was posted
    repost_seconds: float = 20  # ...and a phase change or those cards re-post it at most this often
    max_stream_per_step: int = 20
    # After the last window, how long the campaign waits for social network searches still to come.
    social_grace_seconds: float = 1800
    # AI relevance checks per campaign (``relevance``); past the cap the deterministic rules decide alone.
    max_relevance_calls: int = 2000
    # No AI verdict (cap reached, paused after an error, no judge, failed call): an exact finding becomes similar.
    relevance_fail_closed: bool = True
    # A failed AI call (not a pause) is retried on a later step; after this many misses for one finding it is held.
    relevance_retry_limit: int = 5
    # Which campaigns queue their sent Facebook posts for a comment read (``leads``): all | investors | off,
    # at most ``comment_max_posts`` per campaign. Off unless a comment worker runs beside the runner (``main``).
    comment_leads: str = "off"
    comment_max_posts: int = 15
    # An investor search sends at most ``max_people`` stored people and reach contacts of its city,
    # seen in the last ``lead_days``.
    max_people: int = 60
    lead_days: int = 90
    # The campaign's metrics row (``campaign_metrics``) is recomputed at most this often while it runs.
    metrics_seconds: float = 30

    def __post_init__(self) -> None:
        if (self.window_cooldown_seconds < 0 or self.analysis_grace_seconds < 0 or self.refusal_retry_seconds < 0
                or self.social_grace_seconds < 0 or not 1 <= self.max_stream_per_step <= 100
                or not 0 <= self.max_relevance_calls <= 10_000 or not 1 <= self.relevance_retry_limit <= 100 or self.comment_leads not in COMMENT_MODES
                or not 0 <= self.comment_max_posts <= 100 or not 0 <= self.max_people <= 500
                or not 1 <= self.lead_days <= 3650 or self.metrics_seconds < 0):
            raise ValueError("unsafe campaign runner settings")


@dataclass(slots=True)
class _Head:
    """A sent exact card as a dedup candidate: its fields and its precomputed ``Listing``."""

    sent: SentFinding
    listing: Listing  # with the body's shingle set and phones, computed once when the head enters the index


def _head_of(sent: SentFinding) -> _Head:
    """A dedup head: the listing fields plus the post text (the stored excerpt, or the whole body of a card sent in
    this step), fingerprinted once."""
    finding = sent.finding
    return _Head(sent, listing_of(finding.payload, url=finding.url, text=finding.text,
                                  body=sent.text_excerpt or finding.original))


@dataclass(slots=True)
class _HeadIndex:
    """The campaign's cluster heads, fetched once per ``_stream`` (on the first exact finding) and kept up to date
    with the cards sent during the step."""

    loaded: bool = False
    heads: list[_Head] = field(default_factory=list)


class CampaignRunner:
    def __init__(
        self,
        campaigns: CampaignStore,
        store: RunStore,
        messenger: Messenger,
        discovery: Discovery | None = None,
        *,
        config: RunnerConfig | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        owner_ids: Collection[int] = frozenset(),
        web: WebProgress | None = None,
        relevance: RelevanceJudge | None = None,
        recorder: Recorder | None = None,
        final_report: FinalReporter | None = None,
    ) -> None:
        """``owner_ids`` (TELEGRAM_OPERATOR_IDS): campaigns they requested show the technical
        status; everyone else sees only the user-safe labels of ``status_text``. ``web``: the
        website stage's progress, when that stage runs. ``relevance``: the AI check of each
        finding against the task (None: the deterministic rules alone). ``recorder`` (SA-3): stores
        every finding before it is sent, and held/excluded ones (None: nothing extra is stored)."""
        self.campaigns, self.store, self.messenger, self.discovery = campaigns, store, messenger, discovery
        self.config, self.now = config or RunnerConfig(), now
        self.owner_ids = frozenset(owner_ids)
        self.web = web
        self.relevance = relevance
        self.final_report = final_report  # the user's «Отчёт по поиску» (None: not sent)
        self.recorder = recorder
        # After a failed relevance call the model is left alone for a minute: one slow or broken
        # provider must not hold a step for 20 findings x the timeout (the rules decide meanwhile).
        self._relevance_paused_until: datetime | None = None
        # Finding id -> failed AI calls so far (in memory, bounded); ids here are skipped while the judge is paused.
        self._relevance_misses: dict[str, int] = {}
        # Campaigns whose chat got a card or a question below the status message: the status moves down.
        self._below_status: set[str] = set()
        self._cards_since: dict[str, int] = {}  # campaign id -> cards sent since its status message was posted
        self._phase: dict[str, str] = {}  # campaign id -> the user line's phase now: searching | checking | done
        self._posted_phase: dict[str, str] = {}  # ... and the phase the posted status message was sent in
        self._posted_at: dict[str, datetime] = {}  # campaign id -> when its status message was last posted
        self._web_edit_at: dict[str, datetime] = {}  # campaign id -> when a «Сейчас ищу» line was last shown
        # campaign id -> {stage: (what it shows, when that last changed)}: the newest change is the one on screen
        self._stage_seen: dict[str, dict[str, tuple[str, datetime]]] = {}
        self._group_now: dict[str, tuple[str, str | None]] = {}  # campaign id -> (name, link) of the group read now
        self._metrics_at: dict[str, datetime] = {}  # campaign id -> when its metrics were last recomputed
        self._metrics_final: set[str] = set()  # ended campaigns whose metrics were recomputed by this process

    async def tick(self) -> int:
        """Advance every open (or just finished) campaign by one step; returns how many were seen."""
        ids = await self.store.active_campaigns()
        for campaign_id in ids:
            try:
                await self.step(campaign_id)
            except Exception:  # one campaign must never stop the others
                log.exception("campaign.runner.step_failed", extra={"campaign_id": campaign_id})
        return len(ids)

    async def recover(self) -> int:
        """Startup only: free the Facebook profile a crashed discovery left ``in_use``.

        The campaign stays ``discovering`` and resumes from its saved progress on
        the next tick. The crashed process's browser lease is not ours to close:
        the Browser Session Manager expires it after its idle time.
        """
        freed = await self.store.recover_discovery_profile()
        if freed:
            log.warning("campaign.runner.recovered_discovery_profile", extra={"profiles": freed})
        return freed

    async def serve(self, poll_seconds: float, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                await self.recover()
                break
            except Exception:  # database not reachable yet: retry before the first tick
                log.exception("campaign.runner.recovery_failed")
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=poll_seconds)
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:  # a database outage delays campaigns, it never ends the loop
                log.exception("campaign.runner.tick_failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)

    async def step(self, campaign_id: str) -> None:
        campaign = await self.campaigns.get(campaign_id)
        if campaign is None:
            return
        with costs.scope(campaign_id):  # the AI checks and the final report are booked on the campaign
            await self._step(campaign_id, campaign)

    async def _step(self, campaign_id: str, campaign: Campaign) -> None:
        line = ""
        if campaign.state not in TERMINAL_STATES:
            await self._stream(campaign)
            try:
                line = await self._advance(campaign)
            except Exception as exc:
                log.exception("campaign.runner.failed", extra={"campaign_id": campaign_id})
                await self.campaigns.set_state(campaign_id, "failed", ACTOR, reason=f"runner_error:{type(exc).__name__}"[:500])
            campaign = await self.campaigns.get(campaign_id) or campaign
            if campaign.state not in TERMINAL_STATES:
                await self._metrics(campaign)
        if campaign.state in TERMINAL_STATES:
            await self._close(campaign)
            # Final: a finding whose AI check is still missing is held as unverified, never lost.
            await self._stream(campaign, final=True)
            await self._metrics(campaign, final=True)
            await self._summary(campaign)
            await self._final_report(campaign)
            line = self._final_line(campaign, await self.store.streamed_count(campaign_id))
        await self._show(campaign, line)

    # -- phases --

    async def _advance(self, campaign: Campaign) -> str:
        if campaign.state in ("planned", "discovering"):
            return await self._discover(campaign)
        window = await self.store.open_window(campaign.id)
        if window is not None:
            return await self._follow_window(campaign, window)
        if campaign.state == "paused_verification":  # paused by a discovery challenge
            return await self._discover(campaign)
        return await self._next_window(campaign)

    async def _discover(self, campaign: Campaign) -> str:
        from .discovery import DiscoveryRefused

        if self.discovery is None:
            return "Пауза: поиск групп не настроен (BROWSER_SESSION_API_TOKEN)"
        if (waiting := await self._facebook_wait()) is not None:
            return waiting
        if await self.store.busy_elsewhere(campaign.id):
            return QUEUE_BUSY
        if (reason := await self.store.breaker_reason()) is not None:
            if campaign.state in ("planned", "discovering"):
                # Nothing to read on Facebook until the breaker closes: the other sources finish the search.
                await self.campaigns.set_state(campaign.id, "running", ACTOR)  # planned -> completed is not a transition
                return await self._drain(campaign, await self.store.get_run(campaign.id), "facebook_breaker")
            return f"Пауза: {reason}"
        await self._show(campaign, SEARCHING)  # discovery takes minutes; say so first
        try:
            await self.discovery.run(campaign.id)
        except DiscoveryRefused as exc:
            return PROFILE_BUSY if "profile" in str(exc) else f"Пауза: {exc}"
        after = await self.campaigns.get(campaign.id)
        if after is not None and after.state == "paused_verification":
            return VERIFY
        if after is not None and after.state == "running":
            return await self._next_window(after)
        return SEARCHING

    async def _follow_window(self, campaign: Campaign, window: Window) -> str:
        if window.batch_state == "human_verification_required":
            if campaign.state == "running":
                await self.campaigns.set_state(campaign.id, "paused_verification", ACTOR,
                                               reason=f"batch_verification:{window.batch_id}")
            return VERIFY
        if window.batch_state not in TERMINAL_BATCH_STATES:
            if campaign.state == "paused_verification":  # the verification flow requeued it
                await self.campaigns.set_state(campaign.id, "running", ACTOR)
            if await self.store.profile_state() == "human_verification_required":
                return VERIFY
            if window.batch_state == "running" and window.current_group:
                self._group_now[campaign.id] = (window.current_group, window.current_group_url)
                return f"Сейчас: Facebook · {window.current_group} · ищу дальше"
            return f"Сейчас: Facebook · окно {window.window_no} ждёт запуска"
        await self.store.close_window(campaign.id, window, ACTOR)
        run = await self.store.get_run(campaign.id)
        cooldown = timedelta(seconds=self.config.window_cooldown_seconds)
        await self.store.save_run(campaign.id, replace(run, next_window_at=self.now() + cooldown))
        if campaign.state == "paused_verification":  # e.g. verification expired and the batch failed
            await self.campaigns.set_state(campaign.id, "running", ACTOR)
        log.info("campaign.window_finished", extra={"campaign_id": campaign.id, "window": window.window_no,
                                                    "batch_state": window.batch_state})
        return ANALYSIS

    async def _next_window(self, campaign: Campaign) -> str:
        limits, now = campaign.plan.limits, self.now()
        run = await self.store.get_run(campaign.id)
        started = await self.store.windows_started(campaign.id)
        groups = await self.store.next_groups(campaign.id, min(limits.window_size, WINDOW_SIZE))
        if not groups:
            return await self._drain(campaign, run, "queue_exhausted")
        if started >= limits.max_windows:
            return await self._drain(campaign, run, "max_windows")
        if run.next_window_at is not None and run.next_window_at > now:
            return ANALYSIS
        if (waiting := await self._facebook_wait()) is not None:
            return waiting
        if await self.store.busy_elsewhere(campaign.id):
            return QUEUE_BUSY
        try:
            start = await self.store.start_window(campaign.id, groups[:WINDOW_SIZE], vertical=campaign.plan.vertical, actor=ACTOR)
        except ValueError as exc:
            # Quota, breaker or profile refusal: nothing was planned; try again later.
            retry = timedelta(seconds=self.config.refusal_retry_seconds)
            run = replace(run, next_window_at=now + retry)
            await self.store.save_run(campaign.id, run)
            log.warning("campaign.window_refused", extra={"campaign_id": campaign.id, "reason": str(exc)})
            if (limit := facebook_limit(str(exc))) is not None:
                # Facebook is done for today: finish once the sites, networks and analysis are, never wait a day.
                return await self._drain(campaign, run, limit)
            return f"Пауза: {exc}"
        if start is None:  # every group's source is paused or blocked; they were skipped
            return ANALYSIS
        log.info("campaign.window_started", extra={"campaign_id": campaign.id, "window": start.window_no,
                                                   "batch_id": start.batch_id, "groups": start.groups})
        return f"Сейчас: Facebook · окно {start.window_no} ждёт запуска"

    async def _drain(self, campaign: Campaign, run: RunState, reason: str) -> str:
        """Wait for the web stage, give the analysis worker a bounded chance to finish, then complete."""
        web = await self._web(campaign.id)
        if web is not None and web.active:
            return web.line or "сайты: поиск"
        now = self.now()
        if run.drain_started_at is None:
            run = replace(run, drain_started_at=now)
            await self.store.save_run(campaign.id, run)
        assert run.drain_started_at is not None
        waited = (now - run.drain_started_at).total_seconds()
        if await self.store.pending_analysis(campaign.id) and waited < self.config.analysis_grace_seconds:
            return ANALYSIS
        if waited < self.config.social_grace_seconds and (await self.store.social_activity(campaign.id)).pending:
            return ANALYSIS  # TikTok / Instagram / LinkedIn queries are still to run (bot.social_search)
        if waited < self.config.social_grace_seconds and await self.store.reach_pending(campaign.id):
            return ANALYSIS  # an investor search's reach across platforms still runs (bot.campaign.reach)
        final = waited >= self.config.analysis_grace_seconds
        if await self._stream(campaign, final=final, skip_misses=False) and not final:
            return ANALYSIS  # findings still wait for the AI check: do not complete (and lose them) yet
        await self.campaigns.set_state(campaign.id, "completed", ACTOR, reason=reason)
        return ANALYSIS

    async def _close(self, campaign: Campaign) -> None:
        """A cancelled or failed campaign stops its in-flight window through the ordinary batch cancel."""
        window = await self.store.open_window(campaign.id)
        if window is None:
            return
        if window.batch_state not in TERMINAL_BATCH_STATES:
            await self.store.cancel_window_batch(window.batch_id, ACTOR)
            window = await self.store.open_window(campaign.id)
        if window is not None and window.batch_state in TERMINAL_BATCH_STATES:
            await self.store.close_window(campaign.id, window, ACTOR)

    async def _facebook_wait(self) -> str | None:
        profile = await self.store.profile_state()
        if profile == "ready":
            return None
        if profile == "human_verification_required":
            return VERIFY
        if profile == "in_use":
            return PROFILE_BUSY
        return f"Пауза: профиль Facebook {profile or 'не создан'}"

    # -- Telegram --

    async def _stream(self, campaign: Campaign, *, final: bool = False, skip_misses: bool = True) -> int:
        """Send each new exact finding once, oldest first; hold the rest and ask about them once.

        Returns how many findings wait for a later step (the AI check missed transiently); ``final``
        (the campaign is ending) treats such a miss as permanent. ``skip_misses=False`` (the drain) does not
        skip remembered misses, so they are counted as deferred instead of being hidden from the completion check.
        """
        active = campaign.state not in TERMINAL_STATES
        request = campaign_request(campaign)
        deferred = 0
        paused = self._relevance_paused_until is not None and self.now() < self._relevance_paused_until
        skip = set(self._relevance_misses) if paused and not final and skip_misses else set()
        index = _HeadIndex()  # the dedup candidates: one query per step, not one per finding
        for finding in await self.store.unstreamed_findings(campaign.id, self.config.max_stream_per_step, skip=skip):
            match = await self._judge(campaign, request, finding, final=final)
            if match is None:  # transient miss: neither held nor streamed, retried on a later step
                deferred += 1
                continue
            if match.bucket != "exact":
                reason = match.note if match.why in NOTED_WHYS else None
                if await self.store.hold_finding(campaign.id, finding.id, match.bucket, match.distance, reason,
                                                 why=reason_category(match.why)):
                    if reason:
                        log.info("campaign.finding_unverified %s", reason,
                                 extra={"campaign_id": campaign.id, "finding_id": finding.id})
                    await self._record(campaign, finding, match.bucket)
                continue
            if await self._deduplicate(campaign, finding, active, index):
                continue
            count = await self.store.claim_finding(campaign.id, finding.id)
            if count is None:
                continue
            note = f"{APPROVED_LINE}{match.note}" if match.why == "approved_deviation" and match.note else None
            if not await self._send_card(campaign, finding, count, active, "exact", note=note):
                return deferred
            if index.loaded:  # the card just sent is a head for the findings still to come in this step
                index.heads.insert(0, _head_of(SentFinding(finding, None, count)))
        await self._near_matches(campaign, request, active)
        await self._people(campaign)
        return deferred

    async def _deduplicate(self, campaign: Campaign, finding: StreamFinding, active: bool,
                           index: _HeadIndex | None = None) -> bool:
        """The same object as an exact card already sent: store the finding as its duplicate and add its link.

        No second card is sent; the head card is edited to show «Также на: ...». Conservative (``dedup.same_object``):
        when in doubt the finding is sent as its own card. A finding without a link is never merged.
        The heads come from ``index`` (fetched once per step, see ``_stream``). The duplicate is recorded as excluded
        (``duplicate_of:<head>``) and, in the store, delivered like a sent finding.
        Returns True when the finding was attached (nothing is left to send).
        """
        if not finding.url:
            return False
        mine = listing_of(finding.payload, url=finding.url, text=finding.text, body=finding.original)
        if mine.price is None and mine.area is None and not mine.shingles:
            return False
        index = index if index is not None else _HeadIndex()
        try:
            if not index.loaded:
                index.loaded = True
                index.heads = [_head_of(h) for h in await self.store.recent_sent_findings(campaign.id)]
            for position, head in enumerate(index.heads):
                if head.sent.finding.id == finding.id or not same_object(mine, head.listing):
                    continue
                link = {"url": finding.url, "site": await self._site_name(campaign, finding.url)}
                if finding.url == head.sent.finding.url:
                    link = {}  # the same post again: nothing to add
                attached = await self.store.attach_to_cluster(finding.id, head.sent.finding.id, link)
                if attached is None:
                    continue
                log.info("campaign.finding_deduplicated", extra={
                    "campaign_id": campaign.id, "finding_id": finding.id, "head_finding_id": head.sent.finding.id})
                await self._record(campaign, finding, "excluded", reason=f"duplicate_of:{head.sent.finding.id}")
                if len(attached.links) > len(head.sent.links):
                    index.heads[position] = replace(head, sent=replace(head.sent, links=attached.links))
                    await self._edit_head(campaign, head.sent, attached, active)
                return True
        except Exception:  # noqa: BLE001 - dedup is an optimisation: on any failure send the card as usual
            log.warning("campaign.dedup_failed", extra={"campaign_id": campaign.id, "finding_id": finding.id})
        return False

    async def _site_name(self, campaign: Campaign, url: str) -> str:
        """The «Также на» name of a sighting: a Facebook post's group name when it is known, else the host."""
        site = site_of(url)
        if site.removeprefix("web.") == "facebook.com":
            try:
                name = await self.store.group_name(campaign.id, url)
            except Exception:  # noqa: BLE001 - the host is a fine name
                name = None
            if name and name.strip():
                return name.strip()[:80]
        return site

    async def _edit_head(self, campaign: Campaign, head: SentFinding, attached: ClusterHead, active: bool) -> None:
        """Re-render the head card with «Также на: ...» and edit its Telegram message (best effort).

        The card keeps the number it was sent with (stored when it was claimed). The head's post text is loaded only
        here, for the render (the dedup candidates are fetched without it)."""
        if attached.message_id is None:
            return
        try:
            full = await self.store.stream_finding(head.finding.id) or head.finding
            tail = f"🔎 Найдено: {head.number}" + (" · ищу дальше" if active else "")
            note = approved_note(campaign, full)
            if note is None and full.payload is not None and head.bucket != "exact":  # a held card sent after approval
                note = offers.card_line(await self._deviation(campaign, campaign_request(campaign), full))
            card = f"{finding_card(campaign, full, cluster_links=attached.links, note=note)}\n\n{tail}"
            await self.messenger.edit(campaign.chat_id, attached.message_id, card[:MAX_MESSAGE_CHARS])
        except Exception:  # noqa: BLE001 - the link is stored; a lost edit only hides it from the card
            log.warning("campaign.cluster_edit_failed",
                        extra={"campaign_id": campaign.id, "finding_id": head.finding.id})

    async def _judge(self, campaign: Campaign, request: Request, finding: StreamFinding, *,
                     final: bool = False) -> Match | None:
        """The finding's bucket: the deterministic rules, then the AI verdict (stored once) on top.

        reject -> excluded; near -> at least similar; match -> the rules' bucket. Anything the
        rules exclude is never sent to the model. ``None``: the AI check missed for a transient reason
        (judge paused, one call failed): the finding is retried on a later step, up to ``relevance_retry_limit``.
        """
        match = classify(finding.payload, request, vertical=finding.vertical)
        if match.bucket == "excluded":
            log.info("campaign.finding_excluded", extra={"campaign_id": campaign.id, "finding_id": finding.id,
                                                         "why": match.why})
            return match
        verdict, note, transient = await self._relevance(campaign, finding)
        if verdict is not None and verdict.verdict is not None:
            self._relevance_misses.pop(finding.id, None)
        if verdict is None or verdict.verdict is None:
            if (self.config.relevance_fail_closed and match.bucket == "exact" and finding.vertical != "investors"
                    and campaign.plan.vertical != "investors"):
                if transient and not final:
                    self._miss(finding.id, 0)  # remembered, so a paused judge does not block the batch
                    return None
                self._relevance_misses.pop(finding.id, None)
                # Not «unverified by the data» but «the check never ran»: counted apart, so the report names the cause.
                why = "cost_cap" if note == UNVERIFIED_BUDGET else "ai_failed"
                return Match("similar", match.distance, why, note=note or UNVERIFIED_FAILED)
            return match
        if verdict.review:  # the reviewer's criteria matrix decides (see ``relevance.review_match``)
            return review_match(match, verdict.review)
        if verdict.verdict == "match":
            return match
        if verdict.verdict == "reject":
            return Match("excluded", float("inf"), "ai")
        return match if match.bucket != "exact" else Match("similar", match.distance, "ai")

    def _miss(self, finding_id: str, add: int) -> int:
        """Failed AI calls of one finding so far (``add`` more); the table is bounded."""
        if finding_id not in self._relevance_misses and len(self._relevance_misses) >= MAX_TRACKED_MISSES:
            del self._relevance_misses[next(iter(self._relevance_misses))]
        count = self._relevance_misses[finding_id] = self._relevance_misses.get(finding_id, 0) + add
        return count

    async def _relevance(self, campaign: Campaign,
                         finding: StreamFinding) -> tuple[Relevance | None, str | None, bool]:
        """The stored verdict, or one new AI call (within the cap); the owner-facing note and whether a miss is transient.

        No verdict: the rules decide alone, and an exact finding is held as unverified (``_judge``) --
        at once for a permanent cause (no judge, cap reached, stored null verdict), after a later retry
        for a transient one (judge paused, a failed call that stored nothing).
        """
        stored = await self.store.relevance(campaign.id, finding.id)
        if stored is not None:
            return stored, (UNVERIFIED_FAILED if stored.verdict is None else None), False
        if self.relevance is None:
            return None, UNVERIFIED_FAILED, False
        if await costs.over_budget(campaign.id):  # CAMPAIGN_BUDGET_USD spent: no more paid checks for this campaign
            return None, UNVERIFIED_BUDGET, False
        if self._relevance_paused_until is not None and self.now() < self._relevance_paused_until:
            return None, UNVERIFIED_FAILED, True
        if await self.store.relevance_calls(campaign.id) >= self.config.max_relevance_calls:
            return None, UNVERIFIED_CAP, False
        try:
            review = bool(getattr(self.relevance, "reviews", False))  # the reviewer also gets the hard criteria
            verdict = await self.relevance.judge(
                task_data(campaign, review=review),
                finding_data(finding.payload, fallback_text=finding.text, original=finding.original, review=review,
                             vertical=finding.vertical))
        except Exception as exc:  # noqa: BLE001 - fail open to the deterministic rules (or closed, see ``_judge``)
            self._relevance_paused_until = self.now() + timedelta(seconds=RELEVANCE_PAUSE_SECONDS)
            code = getattr(exc, "code", type(exc).__name__)
            log.warning("campaign.relevance_failed %s", code, extra={"campaign_id": campaign.id, "finding_id": finding.id})
            if self.config.relevance_fail_closed and self._miss(finding.id, 1) < self.config.relevance_retry_limit:
                return None, UNVERIFIED_FAILED, True  # nothing stored: the finding is judged again later
            verdict = Relevance(None, f"error:{code}"[:300], None, getattr(self.relevance, "model", None))
        await self.store.save_relevance(campaign.id, finding.id, verdict)
        # The reason is for owners (logs); users never see it.
        log.info("campaign.relevance %s: %s", verdict.verdict, verdict.reason,
                 extra={"campaign_id": campaign.id, "finding_id": finding.id})
        return verdict, (UNVERIFIED_FAILED if verdict.verdict is None else None), False

    async def _deviation(self, campaign: Campaign, request: Request, closest: StreamFinding) -> offers.Deviation:
        """What the closest held listing has outside the criteria: price, area, or the AI's phrase."""
        match = classify(closest.payload, request, vertical=closest.vertical)
        land = (closest.payload or {}).get("property_type") == "land"
        price = price_of(closest.payload)
        if match.why == "price" and price is not None and request.amount is not None and price > request.amount:
            return offers.Deviation("price", money(price, request.currency), money(request.amount, request.currency),
                                    land=land)
        if match.why == "area" and match.area is not None and request.min_area:
            requested = f"от {round(request.min_area):,} м²".replace(",", " ")
            return offers.Deviation("area", area_text(match.area), requested, land=land)
        if match.why == "area_unknown":
            return offers.Deviation("area_unknown", phrase="площадь не указана", land=land)
        if self.config.relevance_fail_closed and closest.hold_reason:  # held unverified (stored note: owners only)
            return offers.Deviation("unverified", land=land)
        stored = await self.store.relevance(campaign.id, closest.id)
        return offers.Deviation("other", phrase=stored.deviation if stored is not None else None, land=land)

    async def _send_card(self, campaign: Campaign, finding: StreamFinding, count: int, active: bool, bucket: str, *,
                         note: str | None = None) -> bool:
        tail = f"🔎 Найдено: {count} · ищу дальше" if active else f"🔎 Найдено: {count}"
        if note is None and bucket != "exact":  # a held card sent after approval says what differs from the request
            try:
                note = offers.card_line(await self._deviation(campaign, campaign_request(campaign), finding))
            except Exception:  # noqa: BLE001 - the note is a courtesy; the card goes out without it
                note = None
        card = f"{finding_card(campaign, finding, note=note)}\n\n{tail}"
        if self.recorder is not None:
            # Stored first (outbox): if the store is down nothing is sent, the finding comes back next tick.
            try:
                await self.recorder.to_send(record_of(campaign.id, finding.id, state="to_send", bucket=bucket,
                                                      payload=finding.payload, text=finding.original or finding.text,
                                                      card_text=card[:MAX_MESSAGE_CHARS]))
            except Exception:  # noqa: BLE001
                log.warning("campaign.finding_record_failed", extra={"campaign_id": campaign.id, "finding_id": finding.id})
                await self.store.release_finding(finding.id)
                return False
        try:
            message_id = await self.messenger.send(campaign.chat_id, card)
        except Exception:  # noqa: BLE001 - Telegram down: give the slot back, retry next tick
            log.warning("campaign.finding_send_failed", extra={"campaign_id": campaign.id, "finding_id": finding.id})
            await self.store.release_finding(finding.id)
            await self._recorded(self.recorder.send_failed(campaign.id, finding.id) if self.recorder else None)
            return False
        await self.store.finding_sent(finding.id, message_id)
        self._cards_since[campaign.id] = self._cards_since.get(campaign.id, 0) + 1
        await self._recorded(self.recorder.sent(campaign.id, finding.id, message_id) if self.recorder else None)
        await self._queue_comments(campaign, finding)
        return True

    async def _queue_comments(self, campaign: Campaign, finding: StreamFinding) -> None:
        """A sent Facebook post: queue one read of its comments for investor leads (never blocks the stream)."""
        mode = self.config.comment_leads
        if mode == "off" or (mode == "investors" and campaign.plan.vertical == "real_estate"):
            return
        if not self.config.comment_max_posts or not is_facebook_post(finding.url):
            return
        try:
            await self.store.queue_comment_read(campaign.id, finding.id, finding.url or "", self.config.comment_max_posts)
        except Exception:  # noqa: BLE001 - the card is out; a lost read only costs leads
            log.warning("campaign.comment_queue_failed", extra={"campaign_id": campaign.id, "finding_id": finding.id})

    async def _people(self, campaign: Campaign) -> None:
        """An investor search: send each person stored from comments under objects in its city, once."""
        location = campaign.plan.location
        if campaign.plan.vertical not in ("investors", "both") or not location or not self.config.max_people:
            return
        room = self.config.max_people - await self.store.people_sent(campaign.id)
        if room <= 0:
            return
        limit = min(room, self.config.max_stream_per_step)
        # People from comments under objects first (they asked about a concrete object), then the reach.
        cards: list[tuple[str, str]] = [
            (p.profile_key, person_card(p, location=campaign.plan.location_aliases.get("ru") or location,
                                        now=self.now()))
            for p in await self.store.stored_people(campaign.id, location, self.config.lead_days, limit)]
        keyed: list[tuple[str, list[str], str | None, str]] = [(key, [key], None, card) for key, card in cards]
        if len(cards) < limit:
            keyed += await self._reach_cards(campaign, location, room - len(cards), limit - len(cards))
        headers: set[tuple[str, str]] = self.__dict__.setdefault("_reach_headers", set())
        for key, keys, group, card in keyed:
            if not await self.store.claim_person(campaign.id, key):
                continue
            claimed = [key, *[k for k in keys if k != key and await self.store.claim_person(campaign.id, k)]]
            try:
                if group and (campaign.id, group) not in headers:  # one header line per group, before its first card
                    await self.messenger.send(campaign.chat_id, people.GROUP_HEADERS[group])
                    headers.add((campaign.id, group))
                message_id = await self.messenger.send(campaign.chat_id, card[:MAX_MESSAGE_CHARS])
            except Exception:  # noqa: BLE001 - Telegram down: retry next tick
                log.warning("campaign.person_send_failed", extra={"campaign_id": campaign.id})
                for k in claimed:
                    await self.store.release_person(campaign.id, k)
                return
            for k in claimed:
                await self.store.person_sent(campaign.id, k, message_id)
            self._cards_since[campaign.id] = self._cards_since.get(campaign.id, 0) + 1

    async def _reach_cards(self, campaign: Campaign, location: str, fetch: int,
                           limit: int) -> list[tuple[str, list[str], str, str]]:
        """Stored reach contacts of the city as cards: scored against the spec, one per person (the same person on
        several platforms is merged), ordered investors/funds, developers, agents/networks, then by score.

        Returns ``(delivery key, every merged key, group id, text)``.
        """
        stored = await self.store.stored_contacts(campaign.id, location, self.config.lead_days, max(fetch, limit))
        pool = getattr(self.store, "pool", None)
        if stored and pool is not None:  # the enrichment columns (migration 037)
            try:
                extras = await contact_extras(pool, [c.url_key for c in stored])
            except Exception:  # noqa: BLE001 - cards without enrichment are still cards
                log.warning("campaign.reach_extras_failed", extra={"campaign_id": campaign.id})
                extras = {}
            stored = [replace(c, **extras[c.url_key]) if c.url_key in extras else c for c in stored]
        investor = people.investor_of(campaign.spec)
        places = [location, *campaign.plan.location_aliases.values(), *(investor or {}).get("geography", [])]
        ready = people.build_cards(stored, investor, places=places, now=self.now())[:limit]
        return [(card.keys[0], list(card.keys), card.group,
                 contact_card(card.contact, score=card.score, reasons=card.reasons, also=card.links))
                for card in ready]

    async def _record(self, campaign: Campaign, finding: StreamFinding, bucket: str, *,
                      reason: str | None = None) -> None:
        """Store a held (similar/other) or excluded finding (``reason``: e.g. ``duplicate_of:<head>``); never blocks the stream."""
        if self.recorder is None:
            return
        record = record_of(campaign.id, finding.id, state="excluded" if bucket == "excluded" else "held",
                           bucket=bucket, payload=finding.payload, text=finding.original or finding.text,
                           reason=reason)
        await self._recorded(self.recorder.excluded(record) if bucket == "excluded" else self.recorder.held(record))

    @staticmethod
    async def _recorded(write: Awaitable[None] | None) -> None:
        """A bookkeeping write after the fact: logged when it fails, never raised (the card is already out)."""
        if write is None:
            return
        try:
            await write
        except Exception:  # noqa: BLE001
            log.warning("campaign.finding_record_failed")

    async def _near_matches(self, campaign: Campaign, request: Request, active: bool) -> None:
        """Held similar/other findings: stream an approved bucket, or ask about it once (see ``offers``).

        Findings inside an approved deviation are already ``exact`` (they stream at once, saying what differs); every
        other similar one stays held and the question is asked as soon as the first one is held."""
        states: dict[str, str | None] = {}
        index = _HeadIndex()  # loaded on the first approved held card only
        for bucket in HELD_BUCKETS:
            state = states[bucket] = await self.store.offer_state(campaign.id, bucket)
            if state == "approved":
                for finding in await self.store.held_findings(campaign.id, bucket, self.config.max_stream_per_step):
                    count = await self.store.claim_held(campaign.id, finding.id)
                    if count is None:
                        continue
                    if await self._deduplicate(campaign, finding, active, index):  # the same object as a sent card
                        continue
                    if not await self._send_card(campaign, finding, count, active, bucket):
                        return
                    if index.loaded:
                        index.heads.insert(0, _head_of(SentFinding(finding, None, count, bucket=bucket)))
                continue
            if state is not None:  # asked and waiting, or declined: never sent
                continue
            held = await self.store.held_findings(campaign.id, bucket, 1)
            if not held:
                continue
            exact = await self.store.exact_count(campaign.id)
            if bucket == "similar":
                ask = True  # as soon as the first similar finding is held, not only at the end
            else:  # farther ones: after the search, once the similar question is out of the way
                ask = not active and states["similar"] != "asked" and not (
                    states["similar"] is None and await self.store.held_findings(campaign.id, "similar", 1))
            if ask:
                await self._ask(campaign, bucket, request, held[0], exact_found=exact > 0)
                states[bucket] = await self.store.offer_state(campaign.id, bucket)

    async def _ask(self, campaign: Campaign, bucket: offers.OfferBucket, request: Request, closest: StreamFinding,
                   *, exact_found: bool) -> None:
        if not await self.store.open_offer(campaign.id, bucket):
            return
        if bucket == "similar":
            text = offers.similar_question(await self._deviation(campaign, request, closest), exact_found=exact_found)
        else:
            text = offers.OTHER_QUESTION
        try:
            message_id = await self.messenger.send_buttons(campaign.chat_id, text, offers.buttons(bucket, campaign.id))
        except Exception:  # noqa: BLE001 - Telegram down: ask again next tick
            log.warning("campaign.offer_send_failed", extra={"campaign_id": campaign.id, "bucket": bucket})
            await self.store.drop_offer(campaign.id, bucket)
            return
        await self.store.offer_sent(campaign.id, bucket, message_id)
        self._below_status.add(campaign.id)
        log.info("campaign.offer_asked", extra={"campaign_id": campaign.id, "bucket": bucket})

    async def _summary(self, campaign: Campaign) -> None:
        """«Итог поиска»: what each source gave, sent once to an owner's campaign when it completes or is cancelled.

        Only for a campaign that ended within ``SUMMARY_WINDOW``: campaigns that ended before this
        feature existed (still stepped for a day) get none.
        """
        if (campaign.state not in SUMMARY_STATES or campaign.finished_at is None
                or campaign.requested_by not in self.owner_ids):  # owners only: it is a technical report
            return
        if self.now() - campaign.finished_at > SUMMARY_WINDOW or not await self.store.claim_summary(campaign.id):
            return
        try:
            sources = await self.store.source_counts(campaign.id)
            site_report = getattr(self.web, "site_report", None)
            reports = await site_report(campaign.id) if site_report is not None else []
            # The known portals only when the web stage ran (it reports every site it met or searched).
            portals = campaign_portals(campaign) if reports else ()
            text = summary_text(campaign.plan.goal, sources, reports, portals=portals)
            await self.messenger.send(campaign.chat_id, text[:MAX_MESSAGE_CHARS])
        except Exception:  # noqa: BLE001 - store or Telegram down: try again next tick
            log.warning("campaign.summary_failed", extra={"campaign_id": campaign.id})
            await self.store.release_summary(campaign.id)
            return
        self._below_status.add(campaign.id)  # the status message moves below the summary
        log.info("campaign.summary_sent", extra={"campaign_id": campaign.id})

    async def _final_report(self, campaign: Campaign) -> None:
        """«Отчёт по поиску» for the person who asked (``final_report``): once, when the campaign completes or is cancelled.

        Same guards as the summary: only for a campaign that ended within ``SUMMARY_WINDOW`` (older ones get none),
        claimed in the database before it is built (a restart never repeats it), given back when the store or
        Telegram failed. Investor-only searches have no property cards to report; a cancelled search that found and
        rejected nothing is not reported.
        """
        if (self.final_report is None or campaign.state not in SUMMARY_STATES or campaign.finished_at is None
                or campaign.plan.vertical == "investors"):
            return
        if self.now() - campaign.finished_at > SUMMARY_WINDOW or not await self.store.claim_final_report(campaign.id):
            return
        try:
            outcomes = await self.store.outcome_counts(campaign.id)
            if campaign.state == "cancelled" and not outcomes:
                return
            sent = await self.store.recent_sent_findings(campaign.id, 500)
            sources = await self.store.source_counts(campaign.id)
            site_report = getattr(self.web, "site_report", None)
            reports = await site_report(campaign.id) if site_report is not None else []
            portals = campaign_portals(campaign) if reports else ()
            request = campaign_request(campaign)
            title = task_title(request, campaign.plan.location_aliases.get("ru"), campaign.plan.goal)
            offers = {bucket: await self.store.offer_state(campaign.id, bucket) for bucket in ("similar", "other")}
            sink = costs.ledger()
            spent = await sink.summary(campaign.id) if sink is not None else None
            text = await self.final_report.build(title, request, outcomes, sent, sources, reports, portals,
                                                 offers=offers, costs=spent, budget=costs.budget())
            await self.messenger.send(campaign.chat_id, text[:MAX_MESSAGE_CHARS])
        except Exception:  # noqa: BLE001 - store or Telegram down: try again next tick
            log.warning("campaign.final_report_failed", extra={"campaign_id": campaign.id})
            await self.store.release_final_report(campaign.id)
            return
        self._below_status.add(campaign.id)  # the status message moves below the report
        log.info("campaign.final_report_sent", extra={"campaign_id": campaign.id})

    async def _metrics(self, campaign: Campaign, *, final: bool = False) -> None:
        """Recompute the campaign's ``campaign_metrics`` row: at most every ``metrics_seconds`` while it runs, and
        once (per process) after it ended. Bookkeeping: a failure is logged and never stops the step."""
        refresh = getattr(self.campaigns, "refresh_metrics", None)
        if refresh is None:
            return
        now = self.now()
        if final:
            if campaign.id in self._metrics_final:
                return
        else:
            last = self._metrics_at.get(campaign.id)
            if last is not None and (now - last).total_seconds() < self.config.metrics_seconds:
                return
        try:
            await refresh(campaign.id)
        except Exception:  # noqa: BLE001
            log.warning("campaign.metrics_failed", extra={"campaign_id": campaign.id})
            return
        self._metrics_at[campaign.id] = now
        if final:
            self._metrics_final.add(campaign.id)

    async def _show(self, campaign: Campaign, line: str) -> None:
        """Keep one status message per campaign, always the last one in the chat: it is edited when its
        text changes, and moved (sent again below, the old one deleted) after new cards or questions."""
        current = await self.campaigns.get(campaign.id) or campaign
        text = await self._status_text(current, line)
        run = await self.store.get_run(current.id)
        now = self.now()
        phase = self._phase.get(current.id, "searching")
        posted = self._posted_phase.get(current.id)
        # Phase change (searching -> checking -> done) and every N cards: post again at the bottom, at most once per
        # repost_seconds (a blocked one is edited in place meanwhile and happens on a later step).
        repost_due = current.status_message_id is not None and (
            (posted is not None and posted != phase) or self._cards_since.get(current.id, 0) >= self.config.repost_cards)
        posted_at = self._posted_at.get(current.id)
        repost = repost_due and (posted_at is None or (now - posted_at).total_seconds() >= self.config.repost_seconds)
        moved = current.id in self._below_status or repost
        if current.status_message_id is not None and run.status_text == text and not moved:
            return
        if (not moved and current.status_message_id is not None and is_live_line(text)
                and is_live_line(run.status_text or "")
                and (now - self._web_edit_at.get(current.id, datetime.min.replace(tzinfo=UTC))).total_seconds()
                < self.config.web_status_seconds):
            return  # the place changes every page: one edit per web_status_seconds is enough (a change waits, not lost)
        try:
            if current.status_message_id is None:
                await self._new_status(current, text)
            elif moved:
                old = current.status_message_id
                await self._new_status(current, text)  # the new one first: there is never no status
                try:
                    await self.messenger.delete(current.chat_id, old)
                except Exception:  # noqa: BLE001 - a leftover old status is only cosmetic
                    log.warning("campaign.status_delete_failed", extra={"campaign_id": current.id})
            else:
                try:
                    await self.messenger.edit(current.chat_id, current.status_message_id, text, parse_mode="HTML")
                except MessageGone:
                    await self._new_status(current, text)
        except Exception:  # noqa: BLE001 - status is cosmetic; the next tick retries
            log.warning("campaign.status_update_failed", extra={"campaign_id": current.id})
            return
        self._below_status.discard(current.id)
        if is_live_line(text):
            self._web_edit_at[current.id] = self.now()
        await self.store.save_run(current.id, replace(await self.store.get_run(current.id), status_text=text))

    def _forget_live(self, campaign_id: str) -> None:
        """A finished campaign shows no «Сейчас ищу» line: drop its in-memory live-status bookkeeping."""
        self._stage_seen.pop(campaign_id, None)
        self._group_now.pop(campaign_id, None)
        self._web_edit_at.pop(campaign_id, None)

    async def _status_text(self, campaign: Campaign, line: str) -> str:
        """The status message as HTML. Owners: the goal, the «Сейчас ищу» line, then the technical lines with their
        counts. Anyone else: one short line that says only where the bot searches now (or a fixed label)."""
        esc = html.escape
        terminal = campaign.state in TERMINAL_STATES
        if terminal:
            self._forget_live(campaign.id)
        web = await self._web(campaign.id) if not terminal else None
        web_active = web is not None and web.active
        social = await self.store.social_activity(campaign.id) if not terminal else None
        live = None if terminal else await self._live_line(campaign, line, web, social)
        self._phase[campaign.id] = ("done" if terminal else "checking" if live is not None and is_checking_line(live)
                                    else "searching")
        if campaign.requested_by in self.owner_ids:
            text = f"🎯 {esc(campaign.plan.goal[:300], quote=False)}"
            if live is not None:
                text += f"\n{live}"
            text += f"\n{esc(line or STOPPED, quote=False)}"
            if web_active and web.line and web.line != line:
                text += f"\n{esc(web.line[:300], quote=False)}"
            if web_active and getattr(web, "progress", None) is not None:
                text += esc(await self._web_detail(campaign, web.progress), quote=False)
            if web_active:  # a site asked for a person's check (human verification): owners see why it waits
                for host in getattr(web, "verification", None) or ():
                    text += esc(f"\nСайт {host} просит проверку — жду, пока её пройдут", quote=False)
            if social is not None:
                if social.searching:
                    query = f" · «{social.query}»" if social.query else ""
                    text += esc(f"\nСоцсети: {social.searching}{query}", quote=False)
                text += "".join(esc(f"\n{note[:300]}", quote=False) for note in social.notes)
            return text
        if terminal:
            return campaign_label(campaign.state, found=await self.store.streamed_count(campaign.id),
                                  reason=campaign.stop_reason)
        if live is not None:
            return live
        if web_active:
            return USER_SEARCHING  # the site stage plans its searches: no place to show yet
        return campaign_label(campaign.state)

    async def _live_line(self, campaign: Campaign, line: str, web: Any, social: Any) -> str | None:
        """«🔎 Сейчас ищу …» for the place searched now; once no search stage is left and judging is pending,
        «🔎 Поиск завершён. Проверяю найденное: проверено X из Y»; None if unknown.

        Search stages run in parallel; each remembers what it shows and when that last changed, and the stage that
        changed most recently is the one shown (ties: Facebook, site, network, reach).
        """
        shown: dict[str, tuple[str, str]] = {}  # stage -> (what it shows, the line)
        progress = getattr(web, "progress", None)
        if web is not None and web.active:
            text = site_line(web.host or getattr(progress, "host", None), getattr(progress, "url", None))
            if text is not None:
                shown["web"] = (text, text)
        if line.startswith("Сейчас: Facebook · ") and line.endswith(" · ищу дальше"):
            name, url = self._group_now.get(campaign.id) or (
                line.removeprefix("Сейчас: Facebook · ").removesuffix(" · ищу дальше"), None)
            text = group_line(name, url)
            shown["facebook"] = (text, text)
        if social is not None and social.searching:
            text = social_line(social.searching, social.query, social.url)
            if text is not None:
                shown["social"] = (text, text)
        reach = await self.store.reach_activity(campaign.id)
        if reach.platform is not None and (text := reach_line(reach.platform)) is not None:
            shown["reach"] = (text, text)
        if not shown and not getattr(web, "active", False) and line == ANALYSIS \
                and await self.store.pending_analysis(campaign.id):
            done, total = await self.store.analysis_progress(campaign.id)  # no search stage left: judging only
            shown["checking"] = ("checking", checking_line(done, total))
        seen, now = self._stage_seen.setdefault(campaign.id, {}), self.now()
        for stage in [st for st in seen if st not in shown]:
            del seen[stage]  # a stage that ended: its next run counts as a change
        for stage, (key, _) in shown.items():
            if stage not in seen or seen[stage][0] != key:
                seen[stage] = (key, now)
        if not shown:
            return None
        order = ["facebook", "web", "social", "reach", "checking"]
        best = max(shown, key=lambda stage: (seen[stage][1], -order.index(stage)))
        return shown[best][1]

    async def _web_detail(self, campaign: Campaign, progress: Any) -> str:
        """Owners only: the current host's refusals per layer and the cards sent so far."""
        parts = []
        if progress.refusals and progress.host:
            parts.append(f"{progress.host}: отказы " + ", ".join(f"{LAYER_NAMES.get(layer, layer)} {n}" for layer, n in progress.refusals))
        sent = await self.store.streamed_count(campaign.id)
        if sent:
            parts.append(f"карточек отправлено {sent}")
        return f"\nСайты: {' · '.join(parts)}" if parts else ""

    async def _web(self, campaign_id: str) -> Any:
        """The web stage's status, or None (no web stage, or it could not be read: never blocks the runner)."""
        if self.web is None:
            return None
        try:
            return await self.web.web_status(campaign_id)
        except Exception:  # noqa: BLE001 - a status read must not stop Facebook work
            log.warning("campaign.web_status_failed", extra={"campaign_id": campaign_id})
            return None

    async def _new_status(self, campaign: Campaign, text: str) -> None:
        message_id = await self.messenger.send(campaign.chat_id, text, parse_mode="HTML")
        await self.campaigns.set_status_message(campaign.id, message_id, actor=ACTOR)
        self._cards_since[campaign.id] = 0
        self._posted_at[campaign.id] = self.now()
        self._posted_phase[campaign.id] = self._phase.get(campaign.id, "searching")

    @staticmethod
    def _final_line(campaign: Campaign, found: int) -> str:
        if campaign.state == "completed":
            limit = " · лимит Facebook на сегодня исчерпан" if campaign.stop_reason in LIMIT_REASONS else ""
            return f"Кампания завершена · найдено {found}{limit}"
        if campaign.state == "failed":
            return f"{STOPPED} · ошибка: {campaign.stop_reason or 'unknown'}"
        return STOPPED if not found else f"{STOPPED} · найдено {found}"


def facebook_limit(refusal: str) -> str | None:
    """The stop reason when a refusal means no more Facebook reads today (daily quota, safety breaker)."""
    if refusal.startswith("daily quota reached") and "Facebook" in refusal:
        return "facebook_daily_limit"
    if refusal.startswith("safety breaker open"):
        return "facebook_breaker"
    return None


def campaign_portals(campaign: Campaign) -> tuple[str, ...]:
    """The known portals the web stage searched for this campaign (Idealista, Fotocasa first)."""
    from bot.web_search.worker import query_task

    return query_task(campaign).portals()


def campaign_request(campaign: Campaign) -> Request:
    """What the campaign asked for, for ``tolerance.classify``."""
    return request_for(campaign.plan.constraints, location=campaign.plan.location, vertical=campaign.plan.vertical,
                       text=f"{campaign.source_text} {campaign.plan.goal}", country=campaign.plan.country,
                       deviations=_deviations_of(campaign))


def _deviations_of(campaign: Campaign) -> dict[str, Any] | None:
    """The interview's approved deviations (``TaskSpec.deviations`` as a dict), None without a spec."""
    if campaign.plan.vertical == "investors":
        return None
    found = (getattr(campaign, "spec", None) or {}).get("deviations")
    return found if isinstance(found, dict) else None


APPROVED_LINE = "≈ В пределах согласованного отступления: "


def approved_note(campaign: Campaign, finding: StreamFinding) -> str | None:
    """«≈ В пределах согласованного отступления: бюджет +7 %» when the finding is exact only by an approved deviation."""
    if finding.payload is None:
        return None
    match = classify(finding.payload, campaign_request(campaign), vertical=finding.vertical)
    return f"{APPROVED_LINE}{match.note}" if match.why == "approved_deviation" and match.note else None


def finding_card(campaign: Campaign, finding: StreamFinding, *,
                 cluster_links: Sequence[dict[str, str]] = (), note: str | None = None) -> str:
    """The Russian card for one finding, its fields ordered by the campaign's task.

    ``note``: a line put first (what differs from the request: an approved deviation, a held similar variant)."""
    if note:
        body = finding_card(campaign, finding, cluster_links=cluster_links)
        return f"{note}\n\n{body}"[:MAX_MESSAGE_CHARS]
    if finding.payload is None:
        extra = also_on(cluster_links)
        return (f"{finding.text}\n\n{extra}" if extra else finding.text)[:MAX_MESSAGE_CHARS]
    constraints = campaign.plan.constraints
    deal, max_price, rooms = constraints.get("deal"), constraints.get("max_price"), constraints.get("rooms")
    task = CardTask(
        vertical=campaign.plan.vertical,
        deal=deal if isinstance(deal, str) else None,
        max_price=max_price if isinstance(max_price, int) else None,
        rooms=rooms if isinstance(rooms, int) else None,
    )
    return render_card(finding.payload, original=finding.original, task=task, vertical=finding.vertical,
                       language=finding.language, confidence=finding.confidence, limit=MAX_MESSAGE_CHARS,
                       cluster_links=cluster_links)


# --- service ------------------------------------------------------------------------------


async def main() -> None:
    import asyncpg

    from bot.facebook_collector.browser import BrowserSessionClient
    from bot.facebook_collector.reader import FacebookGroupReader

    from .discovery import FacebookDiscovery, PostgresDiscoveryStore
    from .runs import PostgresRunStore
    from .settings import CampaignRunnerSettings
    from .store import PostgresCampaignStore

    settings = CampaignRunnerSettings()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=6)
    costs.install(costs.PostgresLedger(pool), settings.budget_usd)  # every paid call of this process is booked
    messenger = TelegramMessenger(settings.telegram_token)
    campaigns = PostgresCampaignStore(pool)
    discovery = None
    if settings.browser_token:
        browser = BrowserSessionClient(settings.browser_url, settings.browser_token)
        reader = FacebookGroupReader(browser, max_posts=settings.discovery_max_posts, timeout_seconds=settings.discovery_group_timeout_seconds)
        discovery = FacebookDiscovery(campaigns, PostgresDiscoveryStore(pool), browser, reader)
    else:
        log.warning("campaign.runner.discovery_disabled", extra={"hint": "set BROWSER_SESSION_API_TOKEN"})
    web = await _web_stage(campaigns, pool, settings)
    judge = settings.relevance_judge()
    if judge is None:
        log.warning("campaign.runner.relevance_rules_only", extra={"hint": "set OPENROUTER_API_KEY"})
    final_reporter = settings.final_reporter()
    comments, lead_judge = _comment_worker(settings, pool)
    reach, reach_closers = _reach_worker(settings, pool, fetcher=getattr(web[0], "fetcher", None) if web else None)
    config = settings.runner_config()
    if comments is None:
        config = replace(config, comment_leads="off")
    store = PostgresRunStore(pool, settings.safety_limits(), dead_days=settings.facebook_group_dead_days)
    runner = CampaignRunner(campaigns, store, messenger, discovery,
                            config=config, owner_ids=settings.owner_ids(),
                            web=web[0].store if web else None, relevance=judge, recorder=PostgresRecorder(pool),
                            final_report=final_reporter)
    social, generator = _social_worker(settings, pool, campaigns)
    log.info("campaign.runner.ready", extra={"poll_seconds": settings.poll_seconds, "web_search": web is not None,
                                             "social_platforms": list(social.config.platforms) if social else [],
                                             "comment_leads": settings.comment_leads if comments else "off",
                                             "investor_reach": reach is not None})
    stop = asyncio.Event()
    try:
        # Each stage is its own loop: a slow site or network never delays Facebook work, and the reverse.
        tasks = [runner.serve(settings.poll_seconds, stop)]
        if web is not None:
            worker, poll, _closers = web
            tasks.append(worker.serve(poll, stop))
        if social is not None:
            tasks.append(social.serve(settings.social_poll_seconds, stop))
        if comments is not None:
            tasks.append(comments.serve(settings.comment_poll_seconds, stop))
        if reach is not None:
            tasks.append(reach.serve(settings.reach_poll_seconds, stop))
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        if web is not None:
            for close in web[2]:
                await close()
        if generator is not None:
            await generator.aclose()
        if lead_judge is not None:
            await lead_judge.aclose()
        for close in reach_closers:
            await close()
        if judge is not None:
            await judge.aclose()
        if final_reporter is not None:
            await final_reporter.aclose()
        await messenger.aclose()
        await pool.close()


async def _web_stage(campaigns: CampaignStore, pool: Any, runner_settings: Any) -> tuple[Any, float, list[Any]] | None:
    """The website search worker (bot.web_search), unless WEB_SEARCH_ENABLED=false.

    Pages drawn by JavaScript are read once more in the Browser Session Manager
    (the Agent Reach path) unless WEB_SEARCH_RENDER_ENABLED=false.
    """
    from bot.web_search.fetcher import PageFetcher
    from bot.web_search.queries import FallbackQueryGenerator, OpenRouterQueryGenerator
    from bot.web_search.searxng import SearxngClient
    from bot.web_search.settings import WebSearchSettings
    from bot.web_search.store import PostgresWebStore
    from bot.web_search.worker import WebSearchWorker

    settings = WebSearchSettings()
    if not settings.enabled:
        log.warning("campaign.web_search_disabled")
        return None
    config = settings.config()
    searcher = settings.searcher(SearxngClient(settings.searxng_url, timeout_seconds=settings.searxng_timeout_seconds,
                                               max_results=config.results_per_query, pages=config.pages_per_query))
    fetcher = PageFetcher(user_agent=settings.user_agent, request_timeout_seconds=settings.request_timeout_seconds,
                          max_content_bytes=settings.max_content_bytes, host_interval_seconds=settings.host_interval_seconds,
                          proxy_url=settings.proxy_url or None, impersonate=settings.impersonate,
                          browser_user_agent=settings.browser_user_agent or None)
    model = None
    if settings.openrouter_api_key:
        model = OpenRouterQueryGenerator(api_key=settings.openrouter_api_key, model=settings.query_model,
                                         timeout_seconds=settings.query_timeout_seconds)
    else:
        log.warning("campaign.web_search_template_queries", extra={"hint": "set OPENROUTER_API_KEY"})
    from bot.verification.settings import _bounded
    from bot.verification.store import PostgresVerificationStore

    job_hours = _bounded(os.environ, "VERIFICATION_JOB_HOURS", 24, 1, 168)  # as the verification service reads it
    web_store = PostgresWebStore(pool, job_hours=job_hours, human_verification=config.human_verification)
    verification = PostgresVerificationStore("")  # shares the runner's pool; closed with it
    verification.pool = pool
    renderer = None
    if settings.render_enabled and runner_settings.browser_token:
        from bot.facebook_collector.browser import BrowserSessionClient
        from bot.web_search.render import BrowserRenderer

        # With human verification the render profile is a browser_profiles row (so the verification flow's live
        # browser and watchdog open the same profile and its cookies) and a challenge page is reported, not read.
        renderer = BrowserRenderer(BrowserSessionClient(runner_settings.browser_url, runner_settings.browser_token),
                                   timeout_seconds=settings.render_timeout_seconds,
                                   profile_source=web_store.render_profile if config.human_verification else None,
                                   detect_challenges=config.human_verification)
    scraper = settings.scraper()
    sources = settings.sources()
    planner = runner_settings.search_planner()
    worker = WebSearchWorker(campaigns, web_store, searcher, fetcher, FallbackQueryGenerator(model),
                             renderer=renderer, scraper=scraper, planner=planner, sources=sources, config=config,
                             cancel_job=verification.cancel)
    closers = ([searcher.aclose, fetcher.aclose] + ([model.aclose] if model else [])
               + ([scraper.aclose] if scraper else []) + ([planner.aclose] if planner else [])
               + [source.aclose for source in sources])
    return worker, settings.poll_seconds, closers


def _social_worker(settings: Any, pool: Any, campaigns: CampaignStore) -> tuple[Any, Any]:
    """The social search worker when SOCIAL_SEARCH_PLATFORMS lists a platform (and the browser is reachable)."""
    config = settings.social_config()
    if not config.platforms:
        return None, None
    if not settings.browser_token:
        log.warning("campaign.runner.social_disabled", extra={"hint": "set BROWSER_SESSION_API_TOKEN"})
        return None, None
    from bot.facebook_collector.browser import BrowserSessionClient
    from bot.social_search.queries import OpenRouterQueryGenerator, QueryPlanner
    from bot.social_search.store import PostgresSocialStore
    from bot.social_search.worker import SocialSearchWorker

    generator = None
    if settings.openrouter_api_key:
        generator = OpenRouterQueryGenerator(api_key=settings.openrouter_api_key, model=settings.social_model,
                                             timeout_seconds=settings.social_model_timeout_seconds)
    else:
        log.warning("campaign.runner.social_queries_without_ai", extra={"hint": "set OPENROUTER_API_KEY"})
    # The browser's API answers after navigation, the bounded wait for results and the scrolls.
    browser = BrowserSessionClient(settings.browser_url, settings.browser_token, timeout_seconds=45)
    worker = SocialSearchWorker(PostgresSocialStore(pool), campaigns, browser, QueryPlanner(generator), config)
    return worker, generator


def _reach_worker(settings: Any, pool: Any, fetcher: Any = None) -> tuple[Any, list[Any]]:
    """The investor reach across platforms (search engines only), unless INVESTOR_REACH_ENABLED=false.

    ``fetcher``: the web stage's PageFetcher, which opens the public page of a relevant result for its contacts
    (None when the web stage is off: no enrichment).
    """
    if not settings.reach_enabled:
        return None, []
    from bot.web_search.searxng import SearxngClient
    from bot.web_search.settings import WebSearchSettings

    from .reach import OpenRouterReachJudge, PostgresReachStore, ReachWorker

    web = WebSearchSettings()
    searcher = SearxngClient(web.searxng_url, timeout_seconds=web.searxng_timeout_seconds, max_results=10)
    judge = None
    if settings.openrouter_api_key:
        judge = OpenRouterReachJudge(api_key=settings.openrouter_api_key, model=settings.reach_model,
                                     timeout_seconds=settings.leads_timeout_seconds)
    else:
        log.warning("campaign.runner.reach_rules_only", extra={"hint": "set OPENROUTER_API_KEY"})
    worker = ReachWorker(PostgresReachStore(pool), searcher, judge, config=settings.reach_config(), fetcher=fetcher)
    return worker, [searcher.aclose] + ([judge.aclose] if judge else [])


def _comment_worker(settings: Any, pool: Any) -> tuple[Any, Any]:
    """The comment reader for investor leads, unless CAMPAIGN_COMMENT_LEADS=off (or the browser is unreachable)."""
    if settings.comment_leads == "off" or not settings.comment_max_posts:
        return None, None
    if not settings.browser_token:
        log.warning("campaign.runner.comment_leads_disabled", extra={"hint": "set BROWSER_SESSION_API_TOKEN"})
        return None, None
    from bot.facebook_collector.browser import BrowserSessionClient

    from .leads import CommentLeadWorker, OpenRouterLeadJudge, PostgresLeadStore

    judge = None
    if settings.openrouter_api_key:
        judge = OpenRouterLeadJudge(api_key=settings.openrouter_api_key, model=settings.leads_model,
                                    timeout_seconds=settings.leads_timeout_seconds)
    else:
        log.warning("campaign.runner.comment_leads_rules_only", extra={"hint": "set OPENROUTER_API_KEY"})
    browser = BrowserSessionClient(settings.browser_url, settings.browser_token, timeout_seconds=45)
    worker = CommentLeadWorker(PostgresLeadStore(pool, settings.safety_limits()), browser, judge,
                               config=settings.comment_config())
    return worker, judge


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
