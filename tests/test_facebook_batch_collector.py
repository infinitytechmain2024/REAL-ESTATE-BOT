"""Contract tests for the bounded dedicated Facebook batch collector."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from bot.facebook_collector.browser import BrowserLease
from bot.facebook_collector.challenges import detect_challenge
from bot.facebook_collector.collector import FacebookBatchCollector
from bot.facebook_collector.models import (
    BatchItem,
    BatchPlan,
    ChallengeDetected,
    CollectedPost,
    GroupRead,
    GroupState,
)
from bot.facebook_collector.reader import FacebookGroupReader, canonical_post_url


@dataclass
class FakeStore:
    plan: BatchPlan
    events: list[tuple] = field(default_factory=list)
    posts: list[CollectedPost] = field(default_factory=list)

    async def load_plan(self, _: str) -> BatchPlan:
        return self.plan

    async def start(self, _: BatchPlan) -> str:
        self.events.append(("batch_start",))
        return "batch-run"

    async def start_item(self, item: BatchItem, *_: str, **__: int) -> str:
        self.events.append(("start", item.sequence_no))
        return f"run-{item.sequence_no}"

    async def save_post(self, _source: str, _run: str, post: CollectedPost) -> None:
        self.posts.append(post)

    async def finish_item(self, item: BatchItem, _run: str, state: str, group_state: GroupState, detail: str | None = None) -> None:
        self.events.append(("finish", item.sequence_no, state, group_state, detail))

    async def finish_batch(self, _plan: BatchPlan, _run: str, state: str, reason: str | None = None) -> None:
        self.events.append(("batch_finish", state, reason))

    async def challenge(self, _plan: BatchPlan, _batch: str, item: BatchItem, _run: str, reason: str) -> None:
        self.events.append(("challenge", item.sequence_no, reason))


class FakeBrowser:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def acquire(self, *args: str) -> BrowserLease:
        self.events.append(("acquire", args[0]))
        return BrowserLease(args[0], "lease")

    async def release(self, lease: BrowserLease, state: str) -> None:
        self.events.append(("release", lease.profile_id, state))


class FakeReader:
    def __init__(self, results: list[GroupRead | Exception]) -> None: self.results, self.urls = results, []
    async def read(self, _lease: BrowserLease, url: str) -> GroupRead:
        self.urls.append(url)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def plan(count: int = 3) -> BatchPlan:
    return BatchPlan("batch", "profile", "fb", "ready", 20, tuple(
        BatchItem(f"item-{i}", f"source-{i}", f"https://www.facebook.com/groups/{i}", i) for i in range(1, count + 1)
    ))


def posts(count: int) -> GroupRead:
    return GroupRead(GroupState.ACTIVE, tuple(CollectedPost(str(i), f"https://www.facebook.com/groups/a/posts/{i}", f"post {i}") for i in range(count)), {})


@pytest.mark.asyncio
async def test_groups_are_strictly_sequential_with_randomized_pause() -> None:
    store, browser, reader = FakeStore(plan()), FakeBrowser(), FakeReader([posts(1), posts(1), posts(1)])
    pauses: list[float] = []

    async def record_pause(seconds: float) -> None:
        pauses.append(seconds)

    collector = FacebookBatchCollector(store, browser, reader, max_posts=10, item_timeout_seconds=10, pause_min_seconds=1, pause_max_seconds=2, sleep=record_pause)
    assert await collector.run("batch") == "succeeded"
    assert [event[:2] for event in store.events if event[0] == "start"] == [("start", 1), ("start", 2), ("start", 3)]
    assert len(pauses) == 2 and all(1 <= pause <= 2 for pause in pauses)
    assert browser.events == [("acquire", "profile"), ("release", "profile", "READY")]


@pytest.mark.asyncio
async def test_post_limit_is_enforced_even_if_reader_returns_more() -> None:
    store, browser, reader = FakeStore(plan(1)), FakeBrowser(), FakeReader([posts(25)])
    collector = FacebookBatchCollector(store, browser, reader, max_posts=10, item_timeout_seconds=10, pause_min_seconds=0, pause_max_seconds=0)
    await collector.run("batch")
    assert len(store.posts) == 10


@pytest.mark.asyncio
async def test_challenge_stops_immediately_creates_job_and_releases_for_verification() -> None:
    store, browser = FakeStore(plan(3)), FakeBrowser()
    reader = FakeReader([ChallengeDetected("facebook_url:/checkpoint", {"url": "https://facebook.com/checkpoint"}), posts(1), posts(1)])
    collector = FacebookBatchCollector(store, browser, reader, max_posts=10, item_timeout_seconds=10, pause_min_seconds=0, pause_max_seconds=0)
    assert await collector.run("batch") == "human_verification_required"
    assert reader.urls == ["https://www.facebook.com/groups/1"]
    assert ("challenge", 1, "facebook_url:/checkpoint") in store.events
    assert browser.events[-1] == ("release", "profile", "VERIFICATION_REQUIRED")


def test_multi_signal_challenge_detection_and_canonical_identity() -> None:
    assert detect_challenge({"url": "https://www.facebook.com/checkpoint/123", "text": ""})
    assert detect_challenge({"url": "https://www.facebook.com/groups/a", "text": "Captcha: confirm it's you"})
    assert canonical_post_url("https://WWW.facebook.com/groups/a/posts/1/?ref=feed") == "https://www.facebook.com/groups/a/posts/1"


@pytest.mark.asyncio
async def test_reader_deduplicates_canonical_posts_and_reports_group_state() -> None:
    class Browser:
        async def snapshot(self, *_: object) -> dict[str, object]:
            return {"url": "https://www.facebook.com/groups/a", "text": "", "posts": [
                {"url": "https://www.facebook.com/groups/a/posts/1/?x=1", "text": "one"},
                {"url": "https://www.facebook.com/groups/a/posts/1", "text": "one duplicate"},
            ]}
    result = await FacebookGroupReader(Browser(), max_posts=20, timeout_seconds=10).read(BrowserLease("p", "t"), "https://www.facebook.com/groups/a")  # type: ignore[arg-type]
    assert result.state is GroupState.ACTIVE and len(result.posts) == 1


@pytest.mark.asyncio
async def test_inaccessible_and_failed_groups_are_reported_without_parallelism() -> None:
    store, browser = FakeStore(plan(2)), FakeBrowser()
    reader = FakeReader([GroupRead(GroupState.INACCESSIBLE, (), {}), RuntimeError("broken layout")])
    collector = FacebookBatchCollector(store, browser, reader, max_posts=10, item_timeout_seconds=10, pause_min_seconds=0, pause_max_seconds=0)
    await collector.run("batch")
    finishes = [event for event in store.events if event[0] == "finish"]
    assert finishes[0][3] is GroupState.INACCESSIBLE
    assert finishes[1] == ("finish", 2, "failed", GroupState.UNKNOWN, "group_read_failed")
