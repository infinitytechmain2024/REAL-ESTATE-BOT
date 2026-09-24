"""Bounded, read-only task runner over the existing Browser Session Manager."""

from __future__ import annotations

import asyncio
from typing import Protocol

from bot.facebook_collector.browser import BrowserLease

from .models import NormalizedPage, ReachOutcome, ReachResult, ReachTask
from .policy import PolicyViolation, challenge_reason, validate_task


class BrowserSession(Protocol):
    async def acquire(self, profile_id: str, profile_name: str, persisted_state: str, *, platform: str = "facebook") -> BrowserLease: ...
    async def snapshot(self, lease: BrowserLease, url: str, timeout_ms: int) -> dict[str, object]: ...
    async def release(self, lease: BrowserLease, next_state: str = "READY") -> None: ...
    async def health(self) -> bool: ...


class ControlledAgentReach:
    """Runs an explicit list of public-page reads; it never invokes a shell.

    Agent Reach is represented as a capability adapter: upstream integration is
    intentionally disabled until it exposes a read-only, externally managed
    browser adapter.  The policy here is the enforcement point, not a prompt.
    """

    def __init__(self, browser: BrowserSession, *, max_pages: int = 5, max_execution_seconds: int = 120, page_timeout_seconds: int = 30) -> None:
        if not 1 <= max_pages <= 20 or not 10 <= max_execution_seconds <= 600 or not 5 <= page_timeout_seconds <= 60:
            raise ValueError("unsafe Agent Reach limits")
        self.browser = browser
        self.max_pages = max_pages
        self.max_execution_seconds = max_execution_seconds
        self.page_timeout_seconds = page_timeout_seconds

    async def run(self, task: ReachTask) -> ReachResult:
        validate_task(task, max_pages=self.max_pages)
        lease: BrowserLease | None = None
        release_state = "READY"
        pages: list[NormalizedPage] = []
        try:
            async with asyncio.timeout(self.max_execution_seconds):
                lease = await self.browser.acquire(
                    task.browser_profile_id, task.browser_profile_name, task.browser_profile_state, platform=task.platform.value
                )
                for target in task.targets:
                    snapshot = await self.browser.snapshot(lease, target, self.page_timeout_seconds * 1000)
                    reason = challenge_reason(snapshot)
                    if reason:
                        release_state = "VERIFICATION_REQUIRED"
                        return ReachResult(task.task_id, ReachOutcome.STOPPED_CHALLENGE, len(pages) + 1, tuple(pages), reason)
                    pages.append(NormalizedPage(
                        canonical_url=str(snapshot.get("url") or target), title=str(snapshot.get("title") or "")[:500],
                        text=str(snapshot.get("text") or "")[:120_000], platform=task.platform.value,
                    ))
        except TimeoutError:
            return ReachResult(task.task_id, ReachOutcome.STOPPED_LIMIT, len(pages), tuple(pages), "execution_timeout")
        except PolicyViolation:
            raise
        except Exception as exc:  # noqa: BLE001 - sandbox boundary returns a structured result.
            return ReachResult(task.task_id, ReachOutcome.FAILED, len(pages), tuple(pages), type(exc).__name__)
        finally:
            if lease:
                await self.browser.release(lease, release_state)
        return ReachResult(task.task_id, ReachOutcome.COMPLETED, len(pages), tuple(pages))

    async def health(self) -> bool:
        return await self.browser.health()
