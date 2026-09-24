"""Contract tests for the bounded dedicated Facebook batch collector."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from aiohttp import web

from bot.browser_session.main import create_app
from bot.browser_session.manager import BrowserSessionManager
from bot.facebook_collector.browser import (
    MAX_NAVIGATION_SECONDS,
    SNAPSHOT_OVERHEAD_SECONDS,
    BrowserLease,
    BrowserSessionClient,
)
from bot.facebook_collector.challenges import detect_challenge
from bot.facebook_collector.collector import FacebookBatchCollector
from bot.facebook_collector.models import (
    BatchCancelled,
    BatchItem,
    BatchPlan,
    ChallengeDetected,
    CollectedPost,
    GroupRead,
    GroupState,
)
from bot.facebook_collector.reader import (
    FacebookGroupReader,
    canonical_post_url,
    navigation_timeout_ms,
)
from bot.facebook_collector.settings import FacebookCollectorSettings


@dataclass
class FakeStore:
    plan: BatchPlan
    events: list[tuple] = field(default_factory=list)
    posts: list[CollectedPost] = field(default_factory=list)
    cancelled_from: set[int] = field(default_factory=set)

    async def load_plan(self, _: str) -> BatchPlan:
        return self.plan

    async def start(self, _: BatchPlan) -> str:
        self.events.append(("batch_start",))
        return "batch-run"

    async def start_item(self, item: BatchItem, *_: str, **__: int) -> str:
        if item.sequence_no in self.cancelled_from:
            raise BatchCancelled(item.id)
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


@pytest.mark.asyncio
async def test_operator_cancellation_stops_before_the_next_group_and_frees_the_profile() -> None:
    store, browser, reader = FakeStore(plan(3), cancelled_from={2, 3}), FakeBrowser(), FakeReader([posts(1)])
    collector = FacebookBatchCollector(store, browser, reader, max_posts=10, item_timeout_seconds=10, pause_min_seconds=0, pause_max_seconds=0)
    assert await collector.run("batch") == "cancelled"
    assert reader.urls == ["https://www.facebook.com/groups/1"]
    assert store.events[-1] == ("batch_finish", "cancelled", "operator_cancelled")
    assert browser.events[-1] == ("release", "profile", "READY")


@pytest.mark.asyncio
async def test_a_joined_private_group_with_posts_is_active_not_inaccessible() -> None:
    class Browser:
        def __init__(self, posts: list[dict[str, str]]) -> None:
            self.posts = posts

        async def snapshot(self, *_: object) -> dict[str, object]:
            return {"url": "https://www.facebook.com/groups/a", "text": "Private group · 12K members", "posts": self.posts}

    lease, url = BrowserLease("p", "t"), "https://www.facebook.com/groups/a"
    member = await FacebookGroupReader(Browser([{"url": f"{url}/posts/1", "text": "flat"}]), max_posts=20, timeout_seconds=90).read(lease, url)  # type: ignore[arg-type]
    outsider = await FacebookGroupReader(Browser([]), max_posts=20, timeout_seconds=90).read(lease, url)  # type: ignore[arg-type]
    assert member.state is GroupState.ACTIVE and len(member.posts) == 1
    assert outsider.state is GroupState.INACCESSIBLE


def test_navigation_budget_fits_the_manager_cap_and_the_group_timeout() -> None:
    for group_timeout in (35, 60, 90, 600):
        navigation = navigation_timeout_ms(group_timeout) / 1000
        assert 5 <= navigation <= MAX_NAVIGATION_SECONDS
        assert navigation + SNAPSHOT_OVERHEAD_SECONDS <= group_timeout


def test_collector_tunables_are_read_from_prefixed_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("BROWSER_SESSION_API_TOKEN", "t" * 32)
    monkeypatch.setenv("FACEBOOK_COLLECTOR_GROUP_TIMEOUT_SECONDS", "50")
    monkeypatch.setenv("FACEBOOK_COLLECTOR_MAX_POSTS_PER_GROUP", "5")
    settings = FacebookCollectorSettings(_env_file=None)  # type: ignore[call-arg]
    assert (settings.group_timeout_seconds, settings.max_posts_per_group) == (50, 5)
    assert settings.database_url == "postgresql://example"


@pytest.mark.asyncio
async def test_default_settings_read_a_group_through_the_real_session_api(tmp_path: Path) -> None:
    """The reader, HTTP client and aiohttp app together, with only Chromium faked."""
    class Page:
        url = ""

        class mouse:
            @staticmethod
            async def wheel(_x: int, _y: int) -> None:
                return None

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
            assert timeout <= 60_000
            self.url = url

        async def wait_for_selector(self, _selector: str, *, timeout: int) -> None:
            return None

        async def wait_for_timeout(self, _ms: int) -> None:
            return None

        async def evaluate(self, _script: str) -> dict[str, object]:
            return {"url": self.url, "title": "Group", "text": "Private group", "posts": [{"url": f"{self.url}/posts/7/", "text": "flat for sale"}]}

    class Context:
        def __init__(self) -> None:
            self.pages = [Page()]

        async def close(self) -> None:
            return None

    class Redis:
        def __init__(self) -> None:
            self.values: dict[str, str] = {}

        async def set(self, name: str, value: str, *, nx: bool, ex: int) -> bool:
            return self.values.setdefault(name, value) == value

        async def get(self, name: str) -> str | None:
            return self.values.get(name)

        async def eval(self, *_: object) -> int:
            return 1

        async def ping(self) -> bool:
            return True

    async def launch(_: Path) -> Context:
        return Context()

    manager = BrowserSessionManager(Redis(), profile_root=tmp_path / "p", screenshot_root=tmp_path / "s", launcher=launch)  # type: ignore[arg-type]
    runner = web.AppRunner(create_app(manager, "t" * 32))
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        client = BrowserSessionClient(f"http://127.0.0.1:{port}", "t" * 32)
        lease = await client.acquire("fb-profile", "fb", "ready")
        result = await FacebookGroupReader(client, max_posts=15, timeout_seconds=90).read(lease, "https://www.facebook.com/groups/a")
        await client.release(lease)
    finally:
        await runner.cleanup()
    assert result.state is GroupState.ACTIVE
    assert [post.canonical_url for post in result.posts] == ["https://www.facebook.com/groups/a/posts/7"]
