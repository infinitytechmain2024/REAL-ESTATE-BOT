"""Tests for the no-concurrent-owner browser session boundary."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from bot.browser_session.manager import (
    BrowserSessionManager,
    ProfileUnavailableError,
    SessionBusyError,
)
from bot.browser_session.models import BrowserProfileStatus, ProfileRequest


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, float]] = {}
        self.healthy = True

    def _live(self, key: str) -> str | None:
        value = self.values.get(key)
        if value and value[1] > time.monotonic():
            return value[0]
        self.values.pop(key, None)
        return None

    async def set(self, name: str, value: str, *, nx: bool, ex: int) -> bool:
        if nx and self._live(name):
            return False
        self.values[name] = (value, time.monotonic() + ex)
        return True

    async def get(self, name: str) -> str | None:
        return self._live(name)

    async def eval(self, script: str, numkeys: int, *args: str | int) -> int:
        key, token = str(args[0]), str(args[1])
        if self._live(key) != token:
            return 0
        if "expire" in script:
            self.values[key] = (token, time.monotonic() + int(args[2]))
            return 1
        self.values.pop(key, None)
        return 1

    async def ping(self) -> bool:
        if not self.healthy:
            raise ConnectionError("redis unavailable")
        return True


class FakeMouse:
    def __init__(self) -> None:
        self.scrolls = 0

    async def wheel(self, _x: int, _y: int) -> None:
        self.scrolls += 1


class FakePage:
    def __init__(self) -> None:
        self.url = ""
        self.mouse = FakeMouse()
        self.active_navigations = 0
        self.max_parallel_navigations = 0
        self.navigation_delay = 0.0

    async def wait_for_selector(self, _selector: str, *, timeout: int) -> None:
        return None

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def screenshot(self, *, path: str, full_page: bool) -> None:
        Path(path).write_bytes(b"fake-png")

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.active_navigations += 1
        self.max_parallel_navigations = max(self.max_parallel_navigations, self.active_navigations)
        await asyncio.sleep(self.navigation_delay)
        self.url = url
        self.active_navigations -= 1

    async def evaluate(self, _: str) -> dict[str, object]:
        return {"url": self.url, "title": "Facebook", "text": "", "posts": []}


class FakeBrowser:
    def __init__(self) -> None:
        self.pages = [FakePage()]
        self.closed = False

    async def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def profile_request() -> ProfileRequest:
    return ProfileRequest("profile_1", "facebook-main", "facebook")


@pytest.fixture
def manager(tmp_path: Path) -> BrowserSessionManager:
    async def launch(_: Path) -> FakeBrowser:
        return FakeBrowser()

    return BrowserSessionManager(
        FakeRedis(), profile_root=tmp_path / "profiles", screenshot_root=tmp_path / "shots",
        lease_seconds=30, renew_seconds=5, launcher=launch,
    )


@pytest.mark.asyncio
async def test_start_release_and_owner_only_permissions(manager: BrowserSessionManager, profile_request: ProfileRequest) -> None:
    states: list[BrowserProfileStatus] = []
    manager.state_changed = lambda _id, state: _append(states, state)
    handle = await manager.acquire(profile_request)
    assert (await manager.status(profile_request.profile_id)).state is BrowserProfileStatus.IN_USE
    assert handle.profile_dir.stat().st_mode & 0o777 == 0o700
    with pytest.raises(PermissionError):
        await manager.release(handle.__class__(handle.profile_id, "wrong", handle.profile_dir, 0))
    await manager.release(handle)
    assert (await manager.status(profile_request.profile_id)).state is BrowserProfileStatus.READY
    assert states == [BrowserProfileStatus.IN_USE, BrowserProfileStatus.READY]


async def _append(items: list[BrowserProfileStatus], item: BrowserProfileStatus) -> None:
    items.append(item)


@pytest.mark.asyncio
async def test_redis_and_filesystem_prevent_concurrent_profile_use(
    manager: BrowserSessionManager, profile_request: ProfileRequest, tmp_path: Path
) -> None:
    handle = await manager.acquire(profile_request)
    with pytest.raises(SessionBusyError):
        await manager.acquire(profile_request)
    other = BrowserSessionManager(
        manager.redis, profile_root=tmp_path / "profiles", screenshot_root=tmp_path / "other", lease_seconds=30,
        renew_seconds=5, launcher=manager.launcher,
    )
    with pytest.raises(SessionBusyError):
        await other.acquire(profile_request)
    await manager.release(handle)
    recovered = await other.acquire(profile_request)
    await other.release(recovered)


@pytest.mark.asyncio
async def test_crash_recovery_is_lease_expiry_and_kernel_lock_release(
    manager: BrowserSessionManager, profile_request: ProfileRequest, tmp_path: Path
) -> None:
    handle = await manager.acquire(profile_request)
    _, fd, browser, renewal = manager._sessions.pop(profile_request.profile_id)  # simulate process death
    renewal.cancel()
    with pytest.raises(asyncio.CancelledError):
        await renewal
    await browser.close()
    os.close(fd)  # OS does this automatically when the crashed process exits.
    manager.redis.values[manager._key(profile_request.profile_id)] = (handle.token, time.monotonic() - 1)  # type: ignore[attr-defined]
    replacement = BrowserSessionManager(
        manager.redis, profile_root=tmp_path / "profiles", screenshot_root=tmp_path / "other", lease_seconds=30,
        renew_seconds=5, launcher=manager.launcher,
    )
    recovered = await replacement.acquire(profile_request)
    await replacement.release(recovered)


@pytest.mark.asyncio
async def test_screenshot_health_and_state_reporting(manager: BrowserSessionManager, profile_request: ProfileRequest) -> None:
    assert await manager.health()
    assert (await manager.status(profile_request.profile_id, persisted_state="human_verification_required")).state is BrowserProfileStatus.VERIFICATION_REQUIRED
    manager.redis.values[manager._key(profile_request.profile_id)] = ("other", time.monotonic() + 30)  # type: ignore[attr-defined]
    assert (await manager.status(profile_request.profile_id)).state is BrowserProfileStatus.LOCKED
    handle = await manager.acquire(ProfileRequest("profile_2", "other", "facebook"))
    screenshot = await manager.screenshot(handle)
    assert screenshot.read_bytes() == b"fake-png"
    assert screenshot.stat().st_mode & 0o777 == 0o600
    await manager.release(handle, next_state=BrowserProfileStatus.VERIFICATION_REQUIRED)
    manager.redis.healthy = False  # type: ignore[attr-defined]
    assert not await manager.health()


@pytest.mark.asyncio
async def test_lease_bound_facebook_snapshot_is_narrow_and_profile_scoped(manager: BrowserSessionManager, profile_request: ProfileRequest) -> None:
    handle = await manager.acquire(profile_request)
    snapshot = await manager.snapshot(handle, "https://www.facebook.com/groups/example", timeout_ms=1_000)
    assert snapshot["url"] == "https://www.facebook.com/groups/example"
    with pytest.raises(ValueError):
        await manager.snapshot(handle, "https://example.com", timeout_ms=1_000)
    await manager.release(handle)


@pytest.mark.asyncio
async def test_snapshot_supports_only_scoped_public_platforms_and_safe_website_hosts(manager: BrowserSessionManager) -> None:
    instagram = await manager.acquire(ProfileRequest("profile_ig", "ig", "instagram"))
    assert (await manager.snapshot(instagram, "https://www.instagram.com/example", timeout_ms=1_000))["url"]
    with pytest.raises(ValueError):
        await manager.snapshot(instagram, "https://www.tiktok.com/example", timeout_ms=1_000)
    await manager.release(instagram)
    website = await manager.acquire(ProfileRequest("profile_web", "web", "website"))
    with pytest.raises(ValueError):
        await manager.snapshot(website, "https://127.0.0.1/private", timeout_ms=1_000)
    await manager.release(website)


@pytest.mark.asyncio
async def test_invalid_state_and_path_are_rejected(manager: BrowserSessionManager, profile_request: ProfileRequest) -> None:
    with pytest.raises(ProfileUnavailableError):
        await manager.acquire(profile_request, persisted_state="quarantined")
    with pytest.raises(ValueError):
        await manager.acquire(ProfileRequest("../escape", "x", "facebook"))


@pytest.mark.asyncio
async def test_a_silent_caller_loses_the_profile_after_the_idle_window(tmp_path: Path, profile_request: ProfileRequest) -> None:
    browsers: list[FakeBrowser] = []

    async def launch(_: Path) -> FakeBrowser:
        browsers.append(FakeBrowser())
        return browsers[-1]

    manager = BrowserSessionManager(
        FakeRedis(), profile_root=tmp_path / "profiles", screenshot_root=tmp_path / "shots",
        lease_seconds=30, renew_seconds=0.05, idle_seconds=0.15, launcher=launch,  # type: ignore[arg-type]
    )
    await manager.acquire(profile_request)
    await asyncio.sleep(0.5)
    assert browsers[0].closed
    second = await manager.acquire(profile_request)
    await manager.release(second)


@pytest.mark.asyncio
async def test_an_active_caller_keeps_its_session(tmp_path: Path, profile_request: ProfileRequest) -> None:
    async def launch(_: Path) -> FakeBrowser:
        return FakeBrowser()

    manager = BrowserSessionManager(
        FakeRedis(), profile_root=tmp_path / "profiles", screenshot_root=tmp_path / "shots",
        lease_seconds=30, renew_seconds=0.05, idle_seconds=0.3, launcher=launch,  # type: ignore[arg-type]
    )
    handle = await manager.acquire(profile_request)
    for _ in range(6):
        await asyncio.sleep(0.1)
        await manager.snapshot(handle, "https://www.facebook.com/groups/a", timeout_ms=1_000)
    await manager.release(handle)


@pytest.mark.asyncio
async def test_snapshots_on_one_profile_never_navigate_in_parallel(manager: BrowserSessionManager, profile_request: ProfileRequest) -> None:
    handle = await manager.acquire(profile_request)
    page = manager._sessions[profile_request.profile_id][2].pages[0]
    page.navigation_delay = 0.05
    await asyncio.gather(*(
        manager.snapshot(handle, f"https://www.facebook.com/groups/{n}", timeout_ms=1_000) for n in range(3)
    ))
    assert page.max_parallel_navigations == 1
    await manager.release(handle)


@pytest.mark.asyncio
async def test_facebook_snapshots_wait_for_the_feed_and_tolerate_an_empty_one(manager: BrowserSessionManager, profile_request: ProfileRequest) -> None:
    handle = await manager.acquire(profile_request)
    page = manager._sessions[profile_request.profile_id][2].pages[0]
    await manager.snapshot(handle, "https://www.facebook.com/groups/a", timeout_ms=1_000)
    assert page.mouse.scrolls == 3

    class TimeoutError(Exception):
        pass

    async def no_feed(_selector: str, *, timeout: int) -> None:
        raise TimeoutError("no articles")

    page.wait_for_selector = no_feed
    assert (await manager.snapshot(handle, "https://www.facebook.com/groups/b", timeout_ms=1_000))["url"].endswith("/b")
    assert page.mouse.scrolls == 3
    await manager.release(handle)
