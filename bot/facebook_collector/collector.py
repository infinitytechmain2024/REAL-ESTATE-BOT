"""One-at-a-time Facebook group batch runner with stop-on-challenge semantics."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Protocol

from .browser import BrowserLease, BrowserSessionClient
from .models import BatchCancelled, BatchItem, BatchPlan, ChallengeDetected, GroupState
from .reader import FacebookGroupReader

log = logging.getLogger(__name__)


class CollectorStore(Protocol):
    async def load_plan(self, batch_id: str) -> BatchPlan: ...
    async def start(self, plan: BatchPlan) -> str: ...
    async def start_item(self, item: BatchItem, batch_run_id: str, profile_id: str, *, max_runtime_seconds: int) -> str: ...
    async def save_post(self, source_id: str, run_id: str, post: object) -> None: ...
    async def finish_item(self, item: BatchItem, run_id: str, state: str, group_state: GroupState, detail: str | None = None, *, diagnostics: dict[str, object] | None = None) -> None: ...
    async def finish_batch(self, plan: BatchPlan, batch_run_id: str, state: str, reason: str | None = None) -> None: ...
    async def challenge(self, plan: BatchPlan, batch_run_id: str, item: BatchItem, run_id: str, reason: str) -> None: ...


class FacebookBatchCollector:
    def __init__(self, store: CollectorStore, browser: BrowserSessionClient, reader: FacebookGroupReader, *, max_groups: int = 20, max_posts: int, item_timeout_seconds: int, pause_min_seconds: float, pause_max_seconds: float, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        if not 1 <= max_groups <= 20 or not 1 <= max_posts <= 20 or item_timeout_seconds < 10 or pause_min_seconds < 0 or pause_max_seconds < pause_min_seconds:
            raise ValueError("unsafe collector limits")
        self.store, self.browser, self.reader = store, browser, reader
        self.max_groups, self.max_posts, self.item_timeout_seconds = max_groups, max_posts, item_timeout_seconds
        self.pause_min_seconds, self.pause_max_seconds, self.sleep = pause_min_seconds, pause_max_seconds, sleep

    async def _screenshot(self, lease: BrowserLease) -> str | None:
        """Best effort: a missing screenshot must never fail the group or batch."""
        capture = getattr(self.browser, "capture_screenshot", None)
        if capture is None:
            return None
        try:
            return await asyncio.wait_for(capture(lease), timeout=30) or None
        except Exception:  # noqa: BLE001 - diagnostics only
            log.warning("facebook_collector.screenshot_failed")
            return None

    @staticmethod
    def _log_group(item: BatchItem, outcome: str, diagnostics: dict[str, object]) -> None:
        log.info("group %s %s %s", item.sequence_no, outcome, diagnostics)

    async def run(self, batch_id: str) -> str:
        plan = await self.store.load_plan(batch_id)
        if len(plan.items) > min(plan.max_items, self.max_groups, 20):
            raise ValueError("batch exceeds hard Facebook group limit")
        batch_run = await self.store.start(plan)
        lease: BrowserLease | None = None
        release_state = "READY"
        try:
            lease = await self.browser.acquire(plan.browser_profile_id, plan.browser_profile_name, plan.browser_profile_state)
            for index, item in enumerate(plan.items):
                if index:
                    await self.sleep(random.uniform(self.pause_min_seconds, self.pause_max_seconds))
                try:
                    run_id = await self.store.start_item(item, batch_run, plan.browser_profile_id, max_runtime_seconds=self.item_timeout_seconds)
                except BatchCancelled:
                    await self.store.finish_batch(plan, batch_run, "cancelled", "operator_cancelled")
                    return "cancelled"
                try:
                    result = await asyncio.wait_for(self.reader.read(lease, item.canonical_url), timeout=self.item_timeout_seconds)
                    for post in result.posts[: self.max_posts]:
                        await self.store.save_post(item.source_id, run_id, post)
                    diagnostics = dict(result.diagnostics)
                    if not result.posts:
                        diagnostics["screenshot"] = await self._screenshot(lease)
                    self._log_group(item, result.state.value, diagnostics)
                    await self.store.finish_item(item, run_id, "succeeded", result.state, diagnostics=diagnostics)
                except ChallengeDetected as challenge:
                    release_state = "VERIFICATION_REQUIRED"
                    self._log_group(item, "challenge", {"reason": challenge.reason})
                    await self.store.challenge(plan, batch_run, item, run_id, challenge.reason)
                    return "human_verification_required"
                except TimeoutError:
                    diagnostics = {"error": "group_timeout", "screenshot": await self._screenshot(lease)}
                    self._log_group(item, "failed", diagnostics)
                    await self.store.finish_item(item, run_id, "failed", GroupState.UNKNOWN, "group_timeout", diagnostics=diagnostics)
                except Exception as exc:  # noqa: BLE001 - a failed group must not abandon a safe batch
                    diagnostics = {"error": type(exc).__name__, "detail": str(exc)[:300], "screenshot": await self._screenshot(lease)}
                    self._log_group(item, "failed", diagnostics)
                    await self.store.finish_item(item, run_id, "failed", GroupState.UNKNOWN, "group_read_failed", diagnostics=diagnostics)
            await self.store.finish_batch(plan, batch_run, "succeeded")
            return "succeeded"
        except Exception:
            await self.store.finish_batch(plan, batch_run, "failed", "collector_error")
            raise
        finally:
            if lease:
                await self.browser.release(lease, release_state)
