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
from bot.analysis_pipeline.cards import CardTask, render_card

from . import offers
from .models import TERMINAL_STATES, WINDOW_SIZE, Campaign
from .relevance import Relevance, RelevanceJudge, finding_data, task_data
from .runs import TERMINAL_BATCH_STATES, RunState, RunStore, StreamFinding, Window
from .status_text import FACEBOOK, campaign_label, user_status
from .store import CampaignStore
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

ACTOR = "campaign:runner"
SEARCHING = "Сейчас: поиск групп Facebook"
ANALYSIS = "Сейчас: анализ"
VERIFY = "Нужна verification"
PROFILE_BUSY = "Ожидание: профиль Facebook занят"
QUEUE_BUSY = "Ожидание: Facebook занят другой кампанией"
STOPPED = "Кампания остановлена"
MAX_MESSAGE_CHARS = 3900


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


@dataclass(frozen=True)
class RunnerConfig:
    window_cooldown_seconds: float = 120
    analysis_grace_seconds: float = 600
    refusal_retry_seconds: float = 300
    max_stream_per_step: int = 20
    # After the last window, how long the campaign waits for social network searches still to come.
    social_grace_seconds: float = 1800
    # AI relevance checks per campaign (``relevance``); past the cap the deterministic rules decide alone.
    max_relevance_calls: int = 200

    def __post_init__(self) -> None:
        if (self.window_cooldown_seconds < 0 or self.analysis_grace_seconds < 0 or self.refusal_retry_seconds < 0
                or self.social_grace_seconds < 0 or not 1 <= self.max_stream_per_step <= 100
                or not 0 <= self.max_relevance_calls <= 10_000):
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
            await self._stream(campaign)
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
            await self.store.save_run(campaign.id, replace(run, next_window_at=now + retry))
            log.warning("campaign.window_refused", extra={"campaign_id": campaign.id, "reason": str(exc)})
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
        await self._stream(campaign)
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

    async def _stream(self, campaign: Campaign) -> None:
        """Send each new exact finding once, oldest first; hold the rest and ask about them once."""
        active = campaign.state not in TERMINAL_STATES
        request = campaign_request(campaign)
        for finding in await self.store.unstreamed_findings(campaign.id, self.config.max_stream_per_step):
            match = await self._judge(campaign, request, finding)
            if match.bucket != "exact":
                if await self.store.hold_finding(campaign.id, finding.id, match.bucket, match.distance):
                    await self._record(campaign, finding, match.bucket)
                continue
            count = await self.store.claim_finding(campaign.id, finding.id)
            if count is None:
                continue
            if not await self._send_card(campaign, finding, count, active, "exact"):
                return
        await self._near_matches(campaign, request, active)

    async def _judge(self, campaign: Campaign, request: Request, finding: StreamFinding) -> Match:
        """The finding's bucket: the deterministic rules, then the AI verdict (stored once) on top.

        reject -> excluded; near -> at least similar; match -> the rules' bucket. Anything the
        rules exclude is never sent to the model.
        """
        match = classify(finding.payload, request, vertical=finding.vertical)
        if match.bucket == "excluded":
            log.info("campaign.finding_excluded", extra={"campaign_id": campaign.id, "finding_id": finding.id,
                                                         "why": match.why})
            return match
        verdict = await self._relevance(campaign, finding)
        if verdict is None or verdict.verdict is None or verdict.verdict == "match":
            return match
        if verdict.verdict == "reject":
            return Match("excluded", float("inf"), "ai")
        return match if match.bucket != "exact" else Match("similar", match.distance, "ai")

    async def _relevance(self, campaign: Campaign, finding: StreamFinding) -> Relevance | None:
        """The stored verdict, or one new AI call (within the cap); None: the rules decide alone."""
        stored = await self.store.relevance(campaign.id, finding.id)
        if stored is not None or self.relevance is None:
            return stored
        if self._relevance_paused_until is not None and self.now() < self._relevance_paused_until:
            return None
        if await self.store.relevance_calls(campaign.id) >= self.config.max_relevance_calls:
            return None
        try:
            verdict = await self.relevance.judge(task_data(campaign), finding_data(finding.payload, fallback_text=finding.text))
        except Exception as exc:  # noqa: BLE001 - fail open to the deterministic rules
            self._relevance_paused_until = self.now() + timedelta(seconds=RELEVANCE_PAUSE_SECONDS)
            code = getattr(exc, "code", type(exc).__name__)
            log.warning("campaign.relevance_failed %s", code, extra={"campaign_id": campaign.id, "finding_id": finding.id})
            verdict = Relevance(None, f"error:{code}"[:300], None, getattr(self.relevance, "model", None))
        await self.store.save_relevance(campaign.id, finding.id, verdict)
        # The reason is for owners (logs); users never see it.
        log.info("campaign.relevance %s: %s", verdict.verdict, verdict.reason,
                 extra={"campaign_id": campaign.id, "finding_id": finding.id})
        return verdict

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
        await self._recorded(self.recorder.sent(campaign.id, finding.id, message_id) if self.recorder else None)
        return True

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
        log.info("campaign.offer_asked", extra={"campaign_id": campaign.id, "bucket": bucket})

    async def _show(self, campaign: Campaign, line: str) -> None:
        """Keep one status message per campaign; edit it only when the text changes."""
        current = await self.campaigns.get(campaign.id) or campaign
        text = await self._status_text(current, line)
        run = await self.store.get_run(current.id)
        if current.status_message_id is not None and run.status_text == text:
            return
        try:
            if current.status_message_id is None:
                await self._new_status(current, text)
            else:
                try:
                    await self.messenger.edit(current.chat_id, current.status_message_id, text)
                except MessageGone:
                    await self._new_status(current, text)
        except Exception:  # noqa: BLE001 - status is cosmetic; the next tick retries
            log.warning("campaign.status_update_failed", extra={"campaign_id": current.id})
            return
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
            if social is not None:
                if social.searching:
                    query = f" · «{social.query}»" if social.query else ""
                    text += f"\nСоцсети: {social.searching}{query}"
                text += "".join(f"\n{note}" for note in social.notes)
            return text
        if terminal:
            return campaign_label(campaign.state, found=await self.store.streamed_count(campaign.id))
        if line.startswith("Сейчас: Facebook · ") and line.endswith("ищу дальше"):
            return FACEBOOK
        if web_active:
            return user_status("site", site=web.host) if web.host else user_status("web")
        checking = line == ANALYSIS and bool(await self.store.pending_analysis(campaign.id))
        # A network search is shown while Facebook itself is idle (between windows, waiting, at the end).
        facebook_busy = line == SEARCHING or line.startswith("Сейчас: Facebook")
        searching = social.searching if social is not None and not facebook_busy else None
        return campaign_label(campaign.state, checking=checking, social=searching)

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
            return f"Кампания завершена · найдено {found}"
        if campaign.state == "failed":
            return f"{STOPPED} · ошибка: {campaign.stop_reason or 'unknown'}"
        return STOPPED if not found else f"{STOPPED} · найдено {found}"


def campaign_request(campaign: Campaign) -> Request:
    """What the campaign asked for, for ``tolerance.classify``."""
    return request_for(campaign.plan.constraints, location=campaign.plan.location, vertical=campaign.plan.vertical,
                       text=f"{campaign.source_text} {campaign.plan.goal}")


def finding_card(campaign: Campaign, finding: StreamFinding) -> str:
    """The Russian card for one finding, its fields ordered by the campaign's task."""
    if finding.payload is None:
        return finding.text[:MAX_MESSAGE_CHARS]
    constraints = campaign.plan.constraints
    deal, max_price, rooms = constraints.get("deal"), constraints.get("max_price"), constraints.get("rooms")
    task = CardTask(
        vertical=campaign.plan.vertical,
        deal=deal if isinstance(deal, str) else None,
        max_price=max_price if isinstance(max_price, int) else None,
        rooms=rooms if isinstance(rooms, int) else None,
    )
    return render_card(finding.payload, original=finding.original, task=task, vertical=finding.vertical,
                       language=finding.language, confidence=finding.confidence, limit=MAX_MESSAGE_CHARS)


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
    runner = CampaignRunner(campaigns, PostgresRunStore(pool, settings.safety_limits()), messenger, discovery,
                            config=settings.runner_config(), owner_ids=settings.owner_ids(),
                            web=web[0].store if web else None, relevance=judge, recorder=PostgresRecorder(pool))
    social, generator = _social_worker(settings, pool, campaigns)
    log.info("campaign.runner.ready", extra={"poll_seconds": settings.poll_seconds, "web_search": web is not None,
                                             "social_platforms": list(social.config.platforms) if social else []})
    stop = asyncio.Event()
    try:
        # Each stage is its own loop: a slow site or network never delays Facebook work, and the reverse.
        tasks = [runner.serve(settings.poll_seconds, stop)]
        if web is not None:
            worker, poll, _closers = web
            tasks.append(worker.serve(poll, stop))
        if social is not None:
            tasks.append(social.serve(settings.social_poll_seconds, stop))
        await asyncio.gather(*tasks)
    finally:
        stop.set()
        if web is not None:
            for close in web[2]:
                await close()
        if generator is not None:
            await generator.aclose()
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
    searcher = SearxngClient(settings.searxng_url, timeout_seconds=settings.searxng_timeout_seconds,
                             max_results=config.results_per_query)
    fetcher = PageFetcher(user_agent=settings.user_agent, request_timeout_seconds=settings.request_timeout_seconds,
                          max_content_bytes=settings.max_content_bytes, host_interval_seconds=settings.host_interval_seconds,
                          proxy_url=settings.proxy_url or None)
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
    worker = WebSearchWorker(campaigns, PostgresWebStore(pool), searcher, fetcher, FallbackQueryGenerator(model),
                             renderer=renderer, config=config)
    closers = [searcher.aclose, fetcher.aclose] + ([model.aclose] if model else [])
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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
