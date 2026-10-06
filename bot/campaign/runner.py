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
import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from bot.agents.recorder import PostgresRecorder, Recorder, record_of
from bot.analysis_pipeline.cards import CardTask, also_on, render_card

from . import offers
from .dedup import listing_of, same_object, site_of
from .leads import MODES as COMMENT_MODES
from .leads import is_facebook_post, person_card
from .models import TERMINAL_STATES, WINDOW_SIZE, Campaign
from .reach import contact_card
from .relevance import Relevance, RelevanceJudge, finding_data, task_data
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
    FACEBOOK,
    LAYER_NAMES,
    LIMIT_REASONS,
    campaign_label,
    group_status,
    user_status,
    web_done_status,
    web_progress_status,
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
    async def send(self, chat_id: int, text: str) -> int: ...
    async def edit(self, chat_id: int, message_id: int, text: str) -> None: ...
    async def send_buttons(self, chat_id: int, text: str, buttons: Sequence[tuple[str, str]]) -> int:
        """A message with one row of inline callback buttons: (label, callback data)."""
        ...
    async def delete(self, chat_id: int, message_id: int) -> None: ...


class TelegramMessenger:
    """Plain-text Bot API calls (no HTML parsing of finding text)."""

    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=20)

    async def send(self, chat_id: int, text: str) -> int:
        data = await self._call("sendMessage", {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True})
        return int(data["result"]["message_id"])

    async def send_buttons(self, chat_id: int, text: str, buttons: Sequence[tuple[str, str]]) -> int:
        markup = {"inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]}
        data = await self._call("sendMessage", {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True,
                                                "reply_markup": markup})
        return int(data["result"]["message_id"])

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text[:4000],
                                                 "disable_web_page_preview": True})
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


@dataclass(frozen=True)
class RunnerConfig:
    window_cooldown_seconds: float = 120
    analysis_grace_seconds: float = 600
    refusal_retry_seconds: float = 300
    web_status_seconds: float = 10  # the web progress line is edited at most this often
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

    def __post_init__(self) -> None:
        if (self.window_cooldown_seconds < 0 or self.analysis_grace_seconds < 0 or self.refusal_retry_seconds < 0
                or self.social_grace_seconds < 0 or not 1 <= self.max_stream_per_step <= 100
                or not 0 <= self.max_relevance_calls <= 10_000 or not 1 <= self.relevance_retry_limit <= 100 or self.comment_leads not in COMMENT_MODES
                or not 0 <= self.comment_max_posts <= 100 or not 0 <= self.max_people <= 500
                or not 1 <= self.lead_days <= 3650):
            raise ValueError("unsafe campaign runner settings")


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
        self.recorder = recorder
        # After a failed relevance call the model is left alone for a minute: one slow or broken
        # provider must not hold a step for 20 findings x the timeout (the rules decide meanwhile).
        self._relevance_paused_until: datetime | None = None
        # Finding id -> failed AI calls so far (in memory, bounded); ids here are skipped while the judge is paused.
        self._relevance_misses: dict[str, int] = {}
        # Campaigns whose chat got a card or a question below the status message: the status moves down.
        self._below_status: set[str] = set()
        self._web_edit_at: dict[str, datetime] = {}  # campaign id -> when the web progress line was last shown

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
        line = ""
        if campaign.state not in TERMINAL_STATES:
            await self._stream(campaign)
            try:
                line = await self._advance(campaign)
            except Exception as exc:
                log.exception("campaign.runner.failed", extra={"campaign_id": campaign_id})
                await self.campaigns.set_state(campaign_id, "failed", ACTOR, reason=f"runner_error:{type(exc).__name__}"[:500])
            campaign = await self.campaigns.get(campaign_id) or campaign
        if campaign.state in TERMINAL_STATES:
            await self._close(campaign)
            # Final: a finding whose AI check is still missing is held as unverified, never lost.
            await self._stream(campaign, final=True)
            await self._summary(campaign)
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
        for finding in await self.store.unstreamed_findings(campaign.id, self.config.max_stream_per_step, skip=skip):
            match = await self._judge(campaign, request, finding, final=final)
            if match is None:  # transient miss: neither held nor streamed, retried on a later step
                deferred += 1
                continue
            if match.bucket != "exact":
                reason = match.note if match.why == "unverified" else None
                if await self.store.hold_finding(campaign.id, finding.id, match.bucket, match.distance, reason):
                    if reason:
                        log.info("campaign.finding_unverified %s", reason,
                                 extra={"campaign_id": campaign.id, "finding_id": finding.id})
                    await self._record(campaign, finding, match.bucket)
                continue
            if await self._deduplicate(campaign, finding, active):
                continue
            count = await self.store.claim_finding(campaign.id, finding.id)
            if count is None:
                continue
            if not await self._send_card(campaign, finding, count, active, "exact"):
                return deferred
        await self._near_matches(campaign, request, active)
        await self._people(campaign)
        return deferred

    async def _deduplicate(self, campaign: Campaign, finding: StreamFinding, active: bool) -> bool:
        """The same object as an exact card already sent: store the finding as its duplicate and add its link.

        No second card is sent; the head card is edited to show «Также на: ...». Conservative (``dedup.same_object``):
        when in doubt the finding is sent as its own card. A finding without a link is never merged.
        Returns True when the finding was attached (nothing is left to send).
        """
        if not finding.url:
            return False
        mine = listing_of(finding.payload, url=finding.url, text=finding.text)
        if mine.price is None and mine.area is None:
            return False
        try:
            heads = await self.store.recent_sent_findings(campaign.id)
            for head in heads:
                if head.finding.id == finding.id or not same_object(
                        mine, listing_of(head.finding.payload, url=head.finding.url, text=head.finding.text)):
                    continue
                link = {"url": finding.url, "site": site_of(finding.url)}
                if finding.url == head.finding.url:
                    link = {}  # the same post again: nothing to add
                attached = await self.store.attach_to_cluster(finding.id, head.finding.id, link)
                if attached is None:
                    continue
                log.info("campaign.finding_deduplicated", extra={
                    "campaign_id": campaign.id, "finding_id": finding.id, "head_finding_id": head.finding.id})
                if len(attached.links) > len(head.links):
                    await self._edit_head(campaign, head, attached, active)
                return True
        except Exception:  # noqa: BLE001 - dedup is an optimisation: on any failure send the card as usual
            log.warning("campaign.dedup_failed", extra={"campaign_id": campaign.id, "finding_id": finding.id})
        return False

    async def _edit_head(self, campaign: Campaign, head: SentFinding, attached: ClusterHead, active: bool) -> None:
        """Re-render the head card with «Также на: ...» and edit its Telegram message (best effort)."""
        if attached.message_id is None:
            return
        tail = f"🔎 Найдено: {head.number}" + (" · ищу дальше" if active else "")
        card = f"{finding_card(campaign, head.finding, cluster_links=attached.links)}\n\n{tail}"
        try:
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
                return Match("similar", match.distance, "unverified", note=note or UNVERIFIED_FAILED)
            return match
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
        if self._relevance_paused_until is not None and self.now() < self._relevance_paused_until:
            return None, UNVERIFIED_FAILED, True
        if await self.store.relevance_calls(campaign.id) >= self.config.max_relevance_calls:
            return None, UNVERIFIED_CAP, False
        try:
            verdict = await self.relevance.judge(task_data(campaign), finding_data(finding.payload, fallback_text=finding.text,
                                                                                 original=finding.original))
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

    async def _send_card(self, campaign: Campaign, finding: StreamFinding, count: int, active: bool, bucket: str) -> bool:
        tail = f"🔎 Найдено: {count} · ищу дальше" if active else f"🔎 Найдено: {count}"
        card = f"{finding_card(campaign, finding)}\n\n{tail}"
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
        self._below_status.add(campaign.id)
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
        if len(cards) < limit:
            cards += [(c.delivery_key, contact_card(c)) for c in await self.store.stored_contacts(
                campaign.id, location, self.config.lead_days, limit - len(cards))]
        for key, card in cards:
            if not await self.store.claim_person(campaign.id, key):
                continue
            try:
                message_id = await self.messenger.send(campaign.chat_id, card[:MAX_MESSAGE_CHARS])
            except Exception:  # noqa: BLE001 - Telegram down: retry next tick
                log.warning("campaign.person_send_failed", extra={"campaign_id": campaign.id})
                await self.store.release_person(campaign.id, key)
                return
            await self.store.person_sent(campaign.id, key, message_id)
            self._below_status.add(campaign.id)

    async def _record(self, campaign: Campaign, finding: StreamFinding, bucket: str) -> None:
        """Store a held (similar/other) or excluded finding; never blocks the stream."""
        if self.recorder is None:
            return
        record = record_of(campaign.id, finding.id, state="excluded" if bucket == "excluded" else "held",
                           bucket=bucket, payload=finding.payload, text=finding.original or finding.text)
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
        """Held similar/other findings: stream an approved bucket, or ask about it once (see ``offers``)."""
        states: dict[str, str | None] = {}
        for bucket in HELD_BUCKETS:
            state = states[bucket] = await self.store.offer_state(campaign.id, bucket)
            if state == "approved":
                for finding in await self.store.held_findings(campaign.id, bucket, self.config.max_stream_per_step):
                    count = await self.store.claim_held(campaign.id, finding.id)
                    if count is not None and not await self._send_card(campaign, finding, count, active, bucket):
                        return
                continue
            if state is not None:  # asked and waiting, or declined: never sent
                continue
            held = await self.store.held_findings(campaign.id, bucket, 1)
            if not held:
                continue
            exact = await self.store.exact_count(campaign.id)
            if bucket == "similar":
                ask = not active or exact == 0
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

    async def _show(self, campaign: Campaign, line: str) -> None:
        """Keep one status message per campaign, always the last one in the chat: it is edited when its
        text changes, and moved (sent again below, the old one deleted) after new cards or questions."""
        current = await self.campaigns.get(campaign.id) or campaign
        text = await self._status_text(current, line)
        run = await self.store.get_run(current.id)
        moved = current.id in self._below_status
        if current.status_message_id is not None and run.status_text == text and not moved:
            return
        if (not moved and current.status_message_id is not None and text.startswith("Сейчас: сайты")
                and (run.status_text or "").startswith("Сейчас: сайты")
                and (self.now() - self._web_edit_at.get(current.id, datetime.min.replace(tzinfo=UTC))).total_seconds()
                < self.config.web_status_seconds):
            return  # the counters move every page: one edit per web_status_seconds is enough
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
                    await self.messenger.edit(current.chat_id, current.status_message_id, text)
                except MessageGone:
                    await self._new_status(current, text)
        except Exception:  # noqa: BLE001 - status is cosmetic; the next tick retries
            log.warning("campaign.status_update_failed", extra={"campaign_id": current.id})
            return
        self._below_status.discard(current.id)
        if text.startswith("Сейчас: сайты"):
            self._web_edit_at[current.id] = self.now()
        await self.store.save_run(current.id, replace(await self.store.get_run(current.id), status_text=text))

    async def _status_text(self, campaign: Campaign, line: str) -> str:
        """Owners get the technical line; anyone else one short label, never ids, windows or counts."""
        terminal = campaign.state in TERMINAL_STATES
        web = await self._web(campaign.id) if not terminal else None
        web_active = web is not None and web.active
        social = await self.store.social_activity(campaign.id) if not terminal else None
        if campaign.requested_by in self.owner_ids:
            text = f"🎯 {campaign.plan.goal}\n{line or STOPPED}"
            if web_active and web.line and web.line != line:
                text += f"\n{web.line}"
            if web_active and getattr(web, "progress", None) is not None:
                text += await self._web_detail(campaign, web.progress)
            if social is not None:
                if social.searching:
                    query = f" · «{social.query}»" if social.query else ""
                    text += f"\nСоцсети: {social.searching}{query}"
                text += "".join(f"\n{note}" for note in social.notes)
            return text
        if terminal:
            return campaign_label(campaign.state, found=await self.store.streamed_count(campaign.id),
                                  reason=campaign.stop_reason)
        if line.startswith("Сейчас: Facebook · ") and line.endswith(" · ищу дальше"):
            # The group being read right now, by its name (like «Ищу на сайте fotocasa.es…» for sites).
            return group_status(line.removeprefix("Сейчас: Facebook · ").removesuffix(" · ищу дальше"))
        progress = getattr(web, "progress", None)
        if web_active:
            if progress is not None:
                return web_progress_status(web.host or progress.host, progress.layer, progress.read, progress.found,
                                           progress.portals_done, progress.portals_total)
            return user_status("site", site=web.host) if web.host else user_status("web")
        checking = line == ANALYSIS and bool(await self.store.pending_analysis(campaign.id))
        # A network search is shown while Facebook itself is idle (between windows, waiting, at the end).
        facebook_busy = line == SEARCHING or line.startswith("Сейчас: Facebook")
        searching = social.searching if social is not None and not facebook_busy else None
        label = campaign_label(campaign.state, checking=checking, social=searching)
        if progress is not None and progress.finished and label in (USER_SEARCHING, FACEBOOK) and searching is None:
            return web_done_status(progress.read, progress.found)  # the web stage ended: its totals stay on screen
        return label

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
        message_id = await self.messenger.send(campaign.chat_id, text)
        await self.campaigns.set_status_message(campaign.id, message_id, actor=ACTOR)

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
                       text=f"{campaign.source_text} {campaign.plan.goal}", country=campaign.plan.country)


def finding_card(campaign: Campaign, finding: StreamFinding, *,
                 cluster_links: Sequence[dict[str, str]] = ()) -> str:
    """The Russian card for one finding, its fields ordered by the campaign's task."""
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
    judge = None
    if settings.openrouter_api_key and settings.relevance_max_calls > 0:
        from .relevance import OpenRouterRelevanceJudge

        judge = OpenRouterRelevanceJudge(api_key=settings.openrouter_api_key, model=settings.relevance_model,
                                         timeout_seconds=settings.relevance_timeout_seconds)
    else:
        log.warning("campaign.runner.relevance_rules_only", extra={"hint": "set OPENROUTER_API_KEY"})
    comments, lead_judge = _comment_worker(settings, pool)
    reach, reach_closers = _reach_worker(settings, pool)
    config = settings.runner_config()
    if comments is None:
        config = replace(config, comment_leads="off")
    store = PostgresRunStore(pool, settings.safety_limits(), dead_days=settings.facebook_group_dead_days)
    runner = CampaignRunner(campaigns, store, messenger, discovery,
                            config=config, owner_ids=settings.owner_ids(),
                            web=web[0].store if web else None, relevance=judge, recorder=PostgresRecorder(pool))
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
    renderer = None
    if settings.render_enabled and runner_settings.browser_token:
        from bot.facebook_collector.browser import BrowserSessionClient
        from bot.web_search.render import BrowserRenderer

        renderer = BrowserRenderer(BrowserSessionClient(runner_settings.browser_url, runner_settings.browser_token),
                                   timeout_seconds=settings.render_timeout_seconds)
    scraper = settings.scraper()
    planner = runner_settings.search_planner()
    worker = WebSearchWorker(campaigns, PostgresWebStore(pool), searcher, fetcher, FallbackQueryGenerator(model),
                             renderer=renderer, scraper=scraper, planner=planner, config=config)
    closers = ([searcher.aclose, fetcher.aclose] + ([model.aclose] if model else [])
               + ([scraper.aclose] if scraper else []) + ([planner.aclose] if planner else []))
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


def _reach_worker(settings: Any, pool: Any) -> tuple[Any, list[Any]]:
    """The investor reach across platforms (search engines only), unless INVESTOR_REACH_ENABLED=false."""
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
    worker = ReachWorker(PostgresReachStore(pool), searcher, judge, config=settings.reach_config())
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
