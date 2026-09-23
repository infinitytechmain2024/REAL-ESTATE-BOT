"""Contract tests for the policy-enforced Agent Reach adapter."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from bot.agent_reach.models import ReachOutcome, ReachPlatform, ReachSkill, ReachTask
from bot.agent_reach.policy import PolicyViolation
from bot.agent_reach.runner import ControlledAgentReach
from bot.agent_reach.settings import AgentReachSettings
from bot.facebook_collector.browser import BrowserLease


@dataclass
class FakeBrowser:
    snapshots: list[dict[str, object]]
    events: list[tuple[object, ...]] = field(default_factory=list)

    async def acquire(self, profile_id: str, _name: str, _state: str, *, platform: str = "facebook") -> BrowserLease:
        self.events.append(("acquire", profile_id, platform))
        return BrowserLease(profile_id, "lease-token")

    async def snapshot(self, _lease: BrowserLease, url: str, timeout_ms: int) -> dict[str, object]:
        self.events.append(("snapshot", url, timeout_ms))
        result = self.snapshots.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def release(self, lease: BrowserLease, next_state: str = "READY") -> None:
        self.events.append(("release", lease.profile_id, next_state))

    async def health(self) -> bool:
        return True


def task(*urls: str, platform: ReachPlatform = ReachPlatform.TIKTOK) -> ReachTask:
    return ReachTask("task-1", platform, tuple(urls), "profile-1", "main")


@pytest.mark.asyncio
async def test_explicit_pages_run_sequentially_with_a_browser_lease_and_normalized_output() -> None:
    browser = FakeBrowser([
        {"url": "https://www.tiktok.com/@one", "title": "One", "text": "first"},
        {"url": "https://www.tiktok.com/@two", "title": "Two", "text": "second"},
    ])
    runner = ControlledAgentReach(browser, max_pages=2, max_execution_seconds=10, page_timeout_seconds=5)
    result = await runner.run(task("https://www.tiktok.com/@one", "https://www.tiktok.com/@two"))
    assert result.outcome is ReachOutcome.COMPLETED
    assert [page.text for page in result.pages] == ["first", "second"]
    assert browser.events == [
        ("acquire", "profile-1", "tiktok"),
        ("snapshot", "https://www.tiktok.com/@one", 5000),
        ("snapshot", "https://www.tiktok.com/@two", 5000),
        ("release", "profile-1", "READY"),
    ]
    assert result.as_dict()["pages"][0]["source_type"] == "public_page"  # type: ignore[index]


@pytest.mark.asyncio
async def test_challenge_stops_immediately_and_releases_profile_for_verification() -> None:
    browser = FakeBrowser([{"url": "https://www.facebook.com/checkpoint", "title": "Security check", "text": ""}])
    runner = ControlledAgentReach(browser, max_pages=2, max_execution_seconds=10, page_timeout_seconds=5)
    result = await runner.run(task("https://www.facebook.com/groups/a", platform=ReachPlatform.FACEBOOK))
    assert result.outcome is ReachOutcome.STOPPED_CHALLENGE
    assert result.pages == ()
    assert browser.events[-1] == ("release", "profile-1", "VERIFICATION_REQUIRED")


@pytest.mark.asyncio
async def test_page_limit_and_non_readonly_skills_are_rejected_before_a_browser_is_acquired() -> None:
    browser = FakeBrowser([])
    runner = ControlledAgentReach(browser, max_pages=1, max_execution_seconds=10, page_timeout_seconds=5)
    with pytest.raises(PolicyViolation):
        await runner.run(task("https://www.tiktok.com/@one", "https://www.tiktok.com/@two"))
    bad = ReachTask("task", ReachPlatform.WEBSITE, ("https://example.org",), "p", "p", allowed_skills=(ReachSkill.READ_PUBLIC_PAGE, "send_message"))  # type: ignore[arg-type]
    with pytest.raises(PolicyViolation):
        await runner.run(bad)
    assert browser.events == []


@pytest.mark.asyncio
async def test_total_execution_timeout_releases_lease() -> None:
    class SlowBrowser(FakeBrowser):
        async def snapshot(self, lease: BrowserLease, url: str, timeout_ms: int) -> dict[str, object]:
            await asyncio.sleep(0.03)
            return await super().snapshot(lease, url, timeout_ms)

    browser = SlowBrowser([{"url": "https://example.org", "title": "x", "text": "x"}])
    runner = ControlledAgentReach(browser, max_pages=1, max_execution_seconds=10, page_timeout_seconds=5)
    # Exercise the timeout path directly; constructor intentionally forbids an
    # unsafe production timeout below ten seconds.
    runner.max_execution_seconds = 0.01
    result = await runner.run(task("https://example.org", platform=ReachPlatform.WEBSITE))
    assert result.outcome is ReachOutcome.STOPPED_LIMIT
    assert browser.events[-1] == ("release", "profile-1", "READY")


@pytest.mark.asyncio
async def test_errors_are_structured_and_private_web_targets_are_refused() -> None:
    browser = FakeBrowser([RuntimeError("network")])
    runner = ControlledAgentReach(browser, max_pages=1, max_execution_seconds=10, page_timeout_seconds=5)
    result = await runner.run(task("https://example.org", platform=ReachPlatform.WEBSITE))
    assert result.outcome is ReachOutcome.FAILED and result.stop_reason == "RuntimeError"
    with pytest.raises(PolicyViolation):
        await runner.run(task("https://127.0.0.1/private", platform=ReachPlatform.WEBSITE))


def test_settings_reject_an_attempt_to_enable_the_unrestricted_upstream_runtime() -> None:
    with pytest.raises(ValueError, match="upstream Agent Reach"):
        AgentReachSettings(
            BROWSER_SESSION_API_TOKEN="x" * 24,
            AGENT_REACH_UPSTREAM_ENABLED=True,
        )
