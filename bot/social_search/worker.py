"""The social search worker: one query at a time per platform, in parallel with Facebook.

It runs inside campaign-runner beside the campaign loop (``CampaignRunner``)
and never touches the Facebook profile, so Facebook windows are not blocked.
Each tick, for every enabled platform (``SOCIAL_SEARCH_PLATFORMS``):

1. Gates: the platform's cooldown (after a rate limit) and pacing (a pause
   with jitter after every query), the daily query cap, and a ``ready``
   logged-in profile. No profile: the platform is skipped silently for users;
   the campaign's owner-only note says to run ``/login <platform>``.
2. The next campaign in turn gets its next planned query, or a new round of
   AI queries (``queries.QueryPlanner``) that repeats nothing already used,
   up to ``max_rounds`` rounds.
3. The profile is claimed in the database (``ready`` -> ``in_use``, so no
   other worker takes it), ONE browser lease is taken for the query and
   always released: the search page is read with a bounded scroll, new items
   (never seen by any campaign) are opened when their card has no caption
   (a capped number, with pauses), and each is filed for analysis.
4. A login wall, captcha or checkpoint stops at once: the profile becomes
   ``human_verification_required`` with a verification job, so the existing
   flow asks the owner to open the live browser. A rate limit pauses the
   platform for hours. The bot never types credentials or solves anything.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from bot.campaign.models import TERMINAL_STATES

from .adapters import PLATFORM_NAMES, SOCIAL_PLATFORMS, Block, SocialItem, adapter_for, detect_block
from .queries import QueryContext, QueryPlanner
from .store import PlannedQuery, Profile, SocialStore

log = logging.getLogger(__name__)


class Lease(Protocol):
    profile_id: str


class SocialBrowser(Protocol):
    async def acquire(self, profile_id: str, profile_name: str, persisted_state: str, *, platform: str = "facebook") -> Any: ...
    async def snapshot(self, lease: Any, url: str, timeout_ms: int, *, scrolls: int = 0) -> dict[str, Any]: ...
    async def release(self, lease: Any, next_state: str = "READY") -> None: ...


class Campaigns(Protocol):
    async def get(self, campaign_id: str) -> Any: ...


@dataclass(frozen=True)
class SocialConfig:
    """Strict per-platform caps. Empty ``platforms`` (the default) turns social search off."""

    platforms: tuple[str, ...] = ()
    queries_per_day: int = 20  # per platform, all campaigns together
    items_per_query: int = 12
    queries_per_round: int = 4
    max_rounds: int = 3  # per campaign and platform
    pause_seconds: float = 120.0  # between two queries on one platform ...
    jitter_seconds: float = 90.0  # ... plus up to this much at random
    detail_per_query: int = 4  # post pages opened per query (Instagram grid tiles have no caption)
    detail_pause_seconds: tuple[float, float] = (4.0, 9.0)
    scrolls: int = 2
    reuse_days: int = 7  # a query another campaign ran in this window is not repeated
    rate_limit_cooldown_seconds: float = 6 * 3600.0
    page_timeout_seconds: int = 45

    def __post_init__(self) -> None:
        unknown = set(self.platforms) - set(SOCIAL_PLATFORMS)
        if unknown:
            raise ValueError(f"unknown social platforms: {sorted(unknown)}")
        bounds = {
            "queries_per_day": (1, 200), "items_per_query": (1, 30), "queries_per_round": (1, 10), "max_rounds": (1, 20),
            "pause_seconds": (20, 3600), "jitter_seconds": (0, 3600), "detail_per_query": (0, 10), "scrolls": (1, 5),
            "reuse_days": (0, 90), "rate_limit_cooldown_seconds": (600, 7 * 86_400), "page_timeout_seconds": (10, 60),
        }
        for name, (low, high) in bounds.items():
            if not low <= getattr(self, name) <= high:
                raise ValueError(f"unsafe social search setting {name}")
        low, high = self.detail_pause_seconds
        if not 1 <= low <= high <= 120:
            raise ValueError("unsafe social search setting detail_pause_seconds")


def parse_platforms(raw: str) -> tuple[str, ...]:
    """``tiktok, instagram,linkedin`` -> ordered unique platforms; ``off`` -> none; anything unknown stops startup."""
    platforms: list[str] = []
    if raw.strip().lower() in {"off", "none", "false", "0"}:
        return ()
    for part in raw.replace(",", " ").split():
        name = part.strip().lower()
        if name not in SOCIAL_PLATFORMS:
            raise ValueError(f"SOCIAL_SEARCH_PLATFORMS: unknown platform {part!r} (use {', '.join(SOCIAL_PLATFORMS)})")
        if name not in platforms:
            platforms.append(name)
    return tuple(platforms)


class _Blocked(Exception):
    def __init__(self, block: Block) -> None:
        super().__init__(block.reason)
        self.block = block


class SocialSearchWorker:
    def __init__(
        self,
        store: SocialStore,
        campaigns: Campaigns,
        browser: SocialBrowser,
        planner: QueryPlanner,
        config: SocialConfig,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.store, self.campaigns, self.browser, self.planner, self.config = store, campaigns, browser, planner, config
        self.now, self.sleep, self.rng = now, sleep, rng or random.Random()
        self._turn: dict[str, int] = {}  # round-robin position per platform

    @property
    def enabled(self) -> bool:
        return bool(self.config.platforms)

    async def serve(self, poll_seconds: float, stop: asyncio.Event) -> None:
        if not self.enabled:
            return
        with suppress(Exception):
            recovered = await self.store.recover()
            if recovered:
                log.warning("social.recovered", extra={"queries": recovered})
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:  # a database or browser outage delays searches, it never ends the loop
                log.exception("social.tick_failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)

    async def tick(self) -> int:
        """Run at most one query per enabled platform; returns how many ran."""
        if not self.enabled:
            return 0
        campaign_ids = await self.store.open_campaigns()
        if not campaign_ids:
            return 0
        ran = 0
        for platform in self.config.platforms:
            try:
                ran += await self._platform(platform, campaign_ids)
            except Exception:  # one platform must never stop the others
                log.exception("social.platform_failed", extra={"platform": platform})
        return ran

    # -- one platform --

    async def _platform(self, platform: str, campaign_ids: list[str]) -> int:
        now, name = self.now(), PLATFORM_NAMES[platform]
        pacing = await self.store.pacing(platform)
        if pacing.paused_until is not None and pacing.paused_until > now:
            await self._note(campaign_ids, platform, f"{name}: пауза после лимита платформы до {pacing.paused_until:%H:%M} UTC")
            return 0
        if pacing.next_action_at is not None and pacing.next_action_at > now:
            return 0
        if await self.store.queries_today(platform) >= self.config.queries_per_day:
            await self._note(campaign_ids, platform, f"{name}: дневной лимит запросов ({self.config.queries_per_day}) исчерпан")
            return 0
        profile = await self.store.ready_profile(platform)
        if profile is None:
            await self._note(campaign_ids, platform, f"{name}: нет готового профиля — войдите через /login {platform}")
            return 0
        start = self._turn.get(platform, 0) % len(campaign_ids)
        for offset in range(len(campaign_ids)):
            index = (start + offset) % len(campaign_ids)
            campaign_id = campaign_ids[index]
            social = await self.store.campaign_social(campaign_id, platform)
            if social.state == "done":
                continue
            campaign = await self.campaigns.get(campaign_id)
            if campaign is None or campaign.state in TERMINAL_STATES:
                continue
            query = await self._next_query(campaign, platform, social.rounds)
            if query is None:
                await self.store.set_campaign_social(campaign_id, platform, "done")
                continue
            self._turn[platform] = index + 1
            await self._run(campaign, platform, profile, query)
            return 1
        return 0

    async def _note(self, campaign_ids: list[str], platform: str, note: str) -> None:
        """An owner-only line on each open campaign that still has work on this platform."""
        for campaign_id in campaign_ids:
            social = await self.store.campaign_social(campaign_id, platform)
            if social.state != "done":
                await self.store.set_campaign_social(campaign_id, platform, "waiting", note=note)

    async def _next_query(self, campaign: Any, platform: str, rounds: int) -> PlannedQuery | None:
        planned = await self.store.next_query(campaign.id, platform)
        if planned is not None or rounds >= self.config.max_rounds:
            return planned
        used = await self.store.used_queries(campaign.id, platform, self.config.reuse_days)
        fresh = await self.planner.round(
            QueryContext.from_campaign(campaign), platform, {(kind, key) for kind, key, _ in used},
            [text for _, _, text in used], self.config.queries_per_round)
        if not fresh or not await self.store.add_queries(campaign.id, platform, rounds + 1, fresh):
            return None
        await self.store.set_campaign_social(campaign.id, platform, "pending", rounds=rounds + 1)
        log.info("social.round_planned", extra={"campaign_id": campaign.id, "platform": platform, "round": rounds + 1,
                                                "queries": [q.text for q in fresh]})
        return await self.store.next_query(campaign.id, platform)

    async def _run(self, campaign: Any, platform: str, profile: Profile, query: PlannedQuery) -> None:
        if not await self.store.claim_profile(profile.id):
            return  # taken by another worker between the check and the claim: next tick
        adapter = adapter_for(platform)
        vertical = campaign.plan.vertical
        try:
            shown_url: str | None = adapter.search_url(query.kind, query.text)  # the status line links to it
        except ValueError:
            shown_url = None  # the run below fails on it and records why
        await self.store.set_campaign_social(campaign.id, platform, "running", current_query=query.text,
                                             current_url=shown_url)
        await self.store.start_query(query.id, profile.id)
        lease: Any = None
        blocked: Block | None = None
        found = saved = 0
        try:
            lease = await self.browser.acquire(profile.id, profile.name, "in_use", platform=platform)
            timeout_ms = self.config.page_timeout_seconds * 1000
            page = await self.browser.snapshot(lease, adapter.search_url(query.kind, query.text), timeout_ms,
                                               scrolls=self.config.scrolls)
            self._check(platform, page)
            items = adapter.parse(page, query.kind, limit=self.config.items_per_query)
            found = len(items)
            known = await self.store.seen(platform, items)
            opened = 0
            for item in (i for i in items if i.key not in known):
                if adapter.needs_detail(item) and opened < self.config.detail_per_query:
                    await self.sleep(self.rng.uniform(*self.config.detail_pause_seconds))
                    detail = await self.browser.snapshot(lease, item.url, timeout_ms, scrolls=0)
                    opened += 1
                    self._check(platform, detail)
                    item = adapter.with_detail(item, detail)
                if _worth_saving(item):
                    saved += await self.store.save_item(campaign.id, vertical, query.id, item)
            await self.store.finish_query(query.id, "done", found=found, new=saved)
            log.info("social.query_done", extra={"campaign_id": campaign.id, "platform": platform, "kind": query.kind,
                                                 "found": found, "new": saved})
        except _Blocked as exc:
            blocked = exc.block
        except Exception as exc:  # noqa: BLE001 - the query is recorded; the lease and profile are freed below
            if getattr(exc, "status", None) == 409:  # the profile's browser is busy (a live view, another lease)
                await self.store.finish_query(query.id, "planned")
            else:
                log.warning("social.query_failed", extra={"platform": platform, "error": type(exc).__name__})
                await self.store.finish_query(query.id, "failed", found=found, new=saved, error=type(exc).__name__)
        finally:
            await self._finish(campaign, platform, profile, query, lease, blocked, vertical)

    def _check(self, platform: str, snapshot: dict[str, Any]) -> None:
        block = detect_block(platform, snapshot)
        if block is not None:
            raise _Blocked(block)

    async def _finish(self, campaign: Any, platform: str, profile: Profile, query: PlannedQuery, lease: Any,
                      blocked: Block | None, vertical: str) -> None:
        name, now = PLATFORM_NAMES[platform], self.now()
        needs_human = blocked is not None and blocked.kind != "rate_limit"
        if lease is not None:
            with suppress(Exception):
                await self.browser.release(lease, "VERIFICATION_REQUIRED" if needs_human else "READY")
        note: str | None = None
        if needs_human:
            assert blocked is not None
            await self.store.challenge(platform, profile.id, query.id, blocked, vertical)
            note = f"{name}: нужен вход или проверка ({blocked.kind}) — владельцу отправлен запрос на вход"
            log.warning("social.challenge", extra={"platform": platform, "reason": blocked.reason})
        else:
            await self.store.release_profile(profile.id)
        if blocked is not None and blocked.kind == "rate_limit":
            until = now + timedelta(seconds=self.config.rate_limit_cooldown_seconds)
            await self.store.finish_query(query.id, "failed", error=f"blocked:{blocked.reason}")
            await self.store.set_pacing(platform, paused_until=until, reason=blocked.reason)
            note = f"{name}: лимит платформы, пауза до {until:%H:%M} UTC"
            log.warning("social.rate_limited", extra={"platform": platform, "reason": blocked.reason})
        pause = self.config.pause_seconds + self.rng.uniform(0, self.config.jitter_seconds)
        await self.store.set_pacing(platform, next_action_at=now + timedelta(seconds=pause))
        await self.store.set_campaign_social(campaign.id, platform, "waiting" if blocked else "pending", note=note)


def _worth_saving(item: SocialItem) -> bool:
    """A card with no words has nothing to analyse; it is left unseen, so a later search may read it."""
    return len(item.text.strip()) >= 3
