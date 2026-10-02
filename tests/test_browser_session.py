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

    async def evaluate(self, script: str, *args: object) -> object:
        if args:  # the social result-card script, called with the platform
            return [{"kind": "post", "url": f"https://www.{args[0]}.com/x", "text": "card"}]
        if "jsonld" in script:  # a public website: its links and JSON-LD
            return {"links": [{"url": "https://example.com/a", "text": "A"}], "jsonld": ['{"@type": "House"}']}
        if "og_title" in script:
            return {"og_title": "", "og_description": "", "og_url": "", "description": "", "time": None}
        return {"url": self.url, "title": "Facebook", "text": "", "posts": []}


class FakeBrowser:
    def __init__(self) -> None:
        self.pages = [FakePage()]
        self.closed = False
        self.jar: list[dict[str, object]] = []

    async def cookies(self) -> list[dict[str, object]]:
        return self.jar

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
async def test_website_snapshot_adds_links_and_jsonld(manager: BrowserSessionManager, monkeypatch: pytest.MonkeyPatch) -> None:
    async def public(*_args: object, **_kwargs: object) -> list[tuple[object, ...]]:
        return [(0, 0, 0, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", public)
    website = await manager.acquire(ProfileRequest("profile_web", "web", "website"))
    snapshot = await manager.snapshot(website, "https://example.com/listing", timeout_ms=1_000)
    assert snapshot["links"] == [{"url": "https://example.com/a", "text": "A"}]
    assert snapshot["jsonld"] == ['{"@type": "House"}']
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


@pytest.mark.asyncio
async def test_the_real_browser_is_not_marked_as_automated(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import playwright.async_api

    from bot.browser_session.manager import BrowserSessionManager

    seen: dict[str, object] = {}

    class FakeChromium:
        async def launch_persistent_context(self, user_data_dir: str, **options: object) -> object:
            seen.update(options, user_data_dir=user_data_dir)
            return object()

    class FakePlaywright:
        chromium = FakeChromium()

    class Starter:
        async def start(self) -> FakePlaywright:
            return FakePlaywright()

    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: Starter())
    await BrowserSessionManager._playwright_launcher(tmp_path)
    assert seen["headless"] is False and seen["user_data_dir"] == str(tmp_path)
    assert "--enable-automation" in seen["ignore_default_args"]  # type: ignore[operator]
    assert "--disable-blink-features=AutomationControlled" in seen["args"]  # type: ignore[operator]


_SEARCH_PAGE = """
<div role="feed">
  <div role="article"><div>
    <a href="https://www.facebook.com/groups/PisosMadrid/?__cft__[0]=x"><img alt=""></a>
    <a href="https://www.facebook.com/groups/PisosMadrid/?__tn__=y">Pisos Madrid</a>
    <span>Público · 12 mil miembros · 10+ publicaciones al día</span>
  </div></div>
  <div><div>
    <a href="https://m.facebook.com/groups/123456789/">Rent Madrid</a>
    <span>Public · 5K members · 3 posts a week</span>
    <a href="https://www.facebook.com/groups/123456789/members/">See members</a>
  </div></div>
  <a href="https://www.facebook.com/groups/feed/">Your feed</a>
  <a href="https://www.facebook.com/groups/777/posts/1/">A post</a>
  <a href="https://www.facebook.com/groups/777/permalink/2/">A permalink</a>
  <a href="https://example.com/groups/elsewhere/">Elsewhere</a>
</div>
"""


@pytest.mark.asyncio
async def test_snapshot_extracts_bounded_group_links_with_their_result_cards() -> None:
    """Runs the real extraction script in a headless Chromium when one is installed."""
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        installed = sorted(Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/nonexistent")).glob("chromium-*/chrome-linux/chrome"))
        browser = None
        for executable in [None, *installed]:
            try:
                browser = await playwright.chromium.launch(headless=True, executable_path=executable)
                break
            except Exception:  # noqa: BLE001 - try the next binary
                continue
        if browser is None:
            pytest.skip("chromium unavailable")
        try:
            page = await browser.new_page()
            await page.set_content(_SEARCH_PAGE)
            snapshot = await BrowserSessionManager._extract(page)
            assert {"url", "title", "text", "diagnostics", "posts", "group_links"} <= set(snapshot)
            assert snapshot["group_links"] == [
                {"url": "https://www.facebook.com/groups/pisosmadrid/", "name": "Pisos Madrid",
                 "card": "Pisos Madrid Público · 12 mil miembros · 10+ publicaciones al día"},
                {"url": "https://www.facebook.com/groups/123456789/", "name": "Rent Madrid",
                 "card": "Rent Madrid Public · 5K members · 3 posts a week See members"},
            ]

            long_name = "x" * 500
            links = "".join(f'<p><a href="https://www.facebook.com/groups/g{i}/">{long_name}</a></p>' for i in range(60))
            await page.set_content(links)
            group_links = (await BrowserSessionManager._extract(page))["group_links"]
            assert len(group_links) == 40
            assert all(len(g["name"]) == 200 and len(g["card"]) <= 400 for g in group_links)
        finally:
            await browser.close()


@pytest.mark.asyncio
async def test_social_snapshots_scroll_a_bounded_number_of_times_and_report_cards_and_login(tmp_path: Path) -> None:
    browsers: list[FakeBrowser] = []

    async def launch(_: Path) -> FakeBrowser:
        browsers.append(FakeBrowser())
        return browsers[-1]

    manager = BrowserSessionManager(FakeRedis(), profile_root=tmp_path / "p", screenshot_root=tmp_path / "s",
                                    lease_seconds=30, renew_seconds=5, launcher=launch)  # type: ignore[arg-type]
    linkedin = await manager.acquire(ProfileRequest("profile_li", "li", "linkedin"))
    result = await manager.snapshot(linkedin, "https://www.linkedin.com/search/results/content/?keywords=x",
                                    timeout_ms=1_000, scrolls=2)
    assert result["items"] and result["meta"]["og_title"] == "" and result["logged_in"] is False
    assert browsers[0].pages[0].mouse.scrolls == 2
    with pytest.raises(ValueError):  # at most SOCIAL_MAX_SCROLLS
        await manager.snapshot(linkedin, "https://www.linkedin.com/feed/", timeout_ms=1_000, scrolls=9)
    with pytest.raises(ValueError):  # LinkedIn profiles stay on LinkedIn
        await manager.snapshot(linkedin, "https://www.facebook.com/", timeout_ms=1_000)
    browsers[0].jar.append({"name": "li_at", "value": "secret", "domain": ".www.linkedin.com", "expires": -1})
    assert await manager.logged_in(linkedin) is True
    assert "secret" not in repr(await manager.snapshot(linkedin, "https://www.linkedin.com/feed/", timeout_ms=1_000))
    await manager.release(linkedin)


async def test_only_an_x_profile_hands_out_its_two_session_cookies(tmp_path: Path) -> None:
    browsers: list[FakeBrowser] = []

    async def launch(_: Path) -> FakeBrowser:
        browsers.append(FakeBrowser())
        return browsers[-1]

    manager = BrowserSessionManager(FakeRedis(), profile_root=tmp_path / "p", screenshot_root=tmp_path / "s",
                                    lease_seconds=30, renew_seconds=5, launcher=launch)  # type: ignore[arg-type]
    x = await manager.acquire(ProfileRequest("profile_x", "x-main", "x"))
    assert await manager.x_credentials(x) is None, "not signed in yet"
    browsers[0].jar.extend([{"name": "auth_token", "value": "tok", "domain": ".x.com", "expires": -1},
                            {"name": "ct0", "value": "csrf", "domain": ".x.com", "expires": -1},
                            {"name": "guest_id", "value": "g", "domain": ".x.com", "expires": -1}])
    assert await manager.x_credentials(x) == {"auth_token": "tok", "ct0": "csrf"}
    linkedin = await manager.acquire(ProfileRequest("profile_li2", "li", "linkedin"))
    browsers[1].jar.append({"name": "auth_token", "value": "tok", "domain": ".x.com", "expires": -1})
    assert await manager.x_credentials(linkedin) is None, "no other platform ever hands out a cookie"
    with pytest.raises(PermissionError):
        await manager.x_credentials(type(x)(x.profile_id, "forged", x.profile_dir, x.expires_at))
    await manager.release(x)
    await manager.release(linkedin)


def test_login_cookie_rules_per_platform() -> None:
    from bot.browser_session.manager import signed_in

    now = 1_000_000.0
    live = {"value": "v", "expires": now + 60}
    assert signed_in([{"name": "c_user", "domain": ".facebook.com", **live}], "facebook", now=now) is True
    assert signed_in([{"name": "sessionid", "domain": ".instagram.com", **live}], "instagram", now=now) is True
    assert signed_in([{"name": "sid_tt", "domain": ".tiktok.com", **live}], "tiktok", now=now) is True
    assert signed_in([{"name": "auth_token", "domain": ".x.com", **live}], "x", now=now) is True
    assert signed_in([{"name": "ct0", "domain": ".x.com", **live}], "x", now=now) is False
    assert signed_in([{"name": "li_at", "domain": ".linkedin.com", **live}], "linkedin", now=now) is True
    # Wrong site, expired, empty, or a cookie that exists logged out too.
    assert signed_in([{"name": "sessionid", "domain": ".tiktok.com.evil.io", **live}], "tiktok", now=now) is False
    assert signed_in([{"name": "li_at", "domain": ".linkedin.com", "value": "v", "expires": now - 1}], "linkedin", now=now) is False
    assert signed_in([{"name": "c_user", "domain": ".facebook.com", "value": "", "expires": -1}], "facebook", now=now) is False
    assert signed_in([{"name": "datr", "domain": ".facebook.com", **live}], "facebook", now=now) is False
    assert signed_in([], "website") is None
