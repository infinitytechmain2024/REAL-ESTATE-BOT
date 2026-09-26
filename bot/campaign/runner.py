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
* One Telegram message per campaign shows the live status; it is edited only
  when the text changes. Owners (TELEGRAM_OPERATOR_IDS) see the technical line;
  anyone else sees only the short labels of ``status_text`` (no ids, windows or
  group counts).
* After the last window the runner waits (bounded) for analysis, then
  completes the campaign. A campaign cancelled from outside has its in-flight
  batch cancelled through the ordinary batch cancel.

Only one campaign uses Facebook at a time: no discovery or window starts
while another campaign has an open window or is discovering.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import httpx

from .models import TERMINAL_STATES, WINDOW_SIZE, Campaign
from .runs import TERMINAL_BATCH_STATES, RunState, RunStore, Window
from .status_text import campaign_label
from .store import CampaignStore

log = logging.getLogger(__name__)

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


class TelegramMessenger:
    """Plain-text Bot API calls (no HTML parsing of finding text)."""

    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=20)

    async def send(self, chat_id: int, text: str) -> int:
        data = await self._call("sendMessage", {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": True})
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


@dataclass(frozen=True)
class RunnerConfig:
    window_cooldown_seconds: float = 120
    analysis_grace_seconds: float = 600
    refusal_retry_seconds: float = 300
    max_stream_per_step: int = 20

    def __post_init__(self) -> None:
        if (self.window_cooldown_seconds < 0 or self.analysis_grace_seconds < 0 or self.refusal_retry_seconds < 0
                or not 1 <= self.max_stream_per_step <= 100):
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
    ) -> None:
        """``owner_ids`` (TELEGRAM_OPERATOR_IDS): campaigns they requested show the technical
        status; everyone else sees only the user-safe labels of ``status_text``."""
        self.campaigns, self.store, self.messenger, self.discovery = campaigns, store, messenger, discovery
        self.config, self.now = config or RunnerConfig(), now
        self.owner_ids = frozenset(owner_ids)

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
        """Give the analysis worker a bounded chance to finish the last posts, then complete."""
        now = self.now()
        if run.drain_started_at is None:
            run = replace(run, drain_started_at=now)
            await self.store.save_run(campaign.id, run)
        assert run.drain_started_at is not None
        waited = (now - run.drain_started_at).total_seconds()
        if await self.store.pending_analysis(campaign.id) and waited < self.config.analysis_grace_seconds:
            return ANALYSIS
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
        """Send each new finding of the campaign once, oldest first."""
        active = campaign.state not in TERMINAL_STATES
        for finding in await self.store.unstreamed_findings(campaign.id, self.config.max_stream_per_step):
            count = await self.store.claim_finding(campaign.id, finding.id)
            if count is None:
                continue
            tail = f"🔎 Найдено: {count} · ищу дальше" if active else f"🔎 Найдено: {count}"
            try:
                message_id = await self.messenger.send(campaign.chat_id, f"{finding.text[:MAX_MESSAGE_CHARS]}\n\n{tail}")
            except Exception:  # noqa: BLE001 - Telegram down: give the slot back, retry next tick
                log.warning("campaign.finding_send_failed", extra={"campaign_id": campaign.id, "finding_id": finding.id})
                await self.store.release_finding(finding.id)
                return
            await self.store.finding_sent(finding.id, message_id)

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
        if campaign.requested_by in self.owner_ids:
            return f"🎯 {campaign.plan.goal}\n{line or STOPPED}"
        if campaign.state in TERMINAL_STATES:
            return campaign_label(campaign.state, found=await self.store.streamed_count(campaign.id))
        checking = line == ANALYSIS and bool(await self.store.pending_analysis(campaign.id))
        return campaign_label(campaign.state, checking=checking)

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
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=4)
    messenger = TelegramMessenger(settings.telegram_token)
    campaigns = PostgresCampaignStore(pool)
    discovery = None
    if settings.browser_token:
        browser = BrowserSessionClient(settings.browser_url, settings.browser_token)
        reader = FacebookGroupReader(browser, max_posts=settings.discovery_max_posts, timeout_seconds=settings.discovery_group_timeout_seconds)
        discovery = FacebookDiscovery(campaigns, PostgresDiscoveryStore(pool), browser, reader)
    else:
        log.warning("campaign.runner.discovery_disabled", extra={"hint": "set BROWSER_SESSION_API_TOKEN"})
    runner = CampaignRunner(campaigns, PostgresRunStore(pool, settings.safety_limits()), messenger, discovery,
                            config=settings.runner_config(), owner_ids=settings.owner_ids())
    log.info("campaign.runner.ready", extra={"poll_seconds": settings.poll_seconds})
    try:
        await runner.serve(settings.poll_seconds)
    finally:
        await messenger.aclose()
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
