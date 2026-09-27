"""Browser-side live view: one window, the collectors' lease, always cleaned up."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.browser_session.live import LiveViewController, LiveViewError
from bot.browser_session.main import create_app
from bot.browser_session.models import ProfileRequest

REQUEST = ProfileRequest("profile-1", "facebook-main", "facebook")


class FakeManager:
    def __init__(self, *, busy: set[str] | None = None, navigation_error: Exception | None = None) -> None:
        self.events: list[tuple[str, ...]] = []
        self.busy = busy or set()
        self.navigation_error = navigation_error
        self.touches = 0
        self.owned: set[str] = set()

    def active_profiles(self) -> frozenset[str]:
        return frozenset(self.busy | self.owned)

    async def acquire(self, request: ProfileRequest) -> str:
        self.events.append(("acquire", request.profile_id))
        self.owned.add(request.profile_id)
        return f"handle:{request.profile_id}"

    async def snapshot(self, handle: str, url: str, *, timeout_ms: int) -> dict[str, Any]:
        self.events.append(("open", url))
        if self.navigation_error:
            raise self.navigation_error
        return {}

    def touch(self, handle: str) -> None:
        self.touches += 1

    async def logged_in(self, handle: str) -> bool | None:
        self.events.append(("logged_in", handle))
        return self.signed_in

    signed_in: bool | None = False

    async def release(self, handle: str) -> None:
        self.events.append(("release", handle))
        self.owned.discard(handle.split(":", 1)[1])

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        return None


def controller(manager: FakeManager, viewers: list[str], keepalive: float = 0.01) -> LiveViewController:
    return LiveViewController(
        manager,  # type: ignore[arg-type]
        start_viewer=lambda password: viewers.append(f"start:{password}") or ["viewer"],
        stop_viewer=lambda processes: viewers.append(f"stop:{processes}"),
        keepalive_seconds=keepalive,
    )


@pytest.mark.asyncio
async def test_start_opens_the_page_with_a_one_time_password_and_stop_releases() -> None:
    manager, viewers = FakeManager(), []
    live = controller(manager, viewers)
    result = await live.start(REQUEST, "https://www.facebook.com/", 5)
    assert len(result["password"]) == 8 and viewers == [f"start:{result['password']}"]
    assert live.status()["profile_id"] == "profile-1"
    await asyncio.sleep(0.03)
    assert manager.touches > 0  # a human drives it: the idle release must not fire

    assert await live.stop("profile-1") is True
    assert viewers[-1] == "stop:['viewer']" and ("release", "handle:profile-1") in manager.events
    assert live.status() == {} and await live.stop() is False


@pytest.mark.asyncio
async def test_only_one_window_and_never_beside_a_collector() -> None:
    manager, viewers = FakeManager(), []
    live = controller(manager, viewers)
    await live.start(REQUEST, "https://www.facebook.com/", 5)
    with pytest.raises(LiveViewError, match="already open"):
        await live.start(ProfileRequest("profile-2", "b", "facebook"), "https://www.facebook.com/", 5)
    await live.stop()

    collector_running = controller(FakeManager(busy={"profile-9"}), [])
    with pytest.raises(LiveViewError, match="another browser session"):
        await collector_running.start(REQUEST, "https://www.facebook.com/", 5)


@pytest.mark.asyncio
async def test_a_failed_navigation_releases_the_profile() -> None:
    manager, viewers = FakeManager(navigation_error=ValueError("snapshot URL is not approved for facebook")), []
    live = controller(manager, viewers)
    with pytest.raises(ValueError):
        await live.start(REQUEST, "https://evil.example/", 5)
    assert manager.events[-1] == ("release", "handle:profile-1") and live.status() == {}


@pytest.mark.asyncio
async def test_a_slow_page_is_still_shown() -> None:
    class TimeoutError(Exception):
        pass

    manager, viewers = FakeManager(navigation_error=TimeoutError("slow")), []
    live = controller(manager, viewers)
    result = await live.start(REQUEST, "https://www.facebook.com/", 5)
    assert result["password"] and live.status()
    await live.stop()


@pytest.mark.asyncio
async def test_the_window_closes_itself_at_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    manager, viewers = FakeManager(), []
    live = controller(manager, viewers)
    await live.start(REQUEST, "https://www.facebook.com/", 1)
    live._current.expires_at = 0  # type: ignore[union-attr]
    await asyncio.sleep(0.05)
    assert live.status() == {} and ("release", "handle:profile-1") in manager.events


@pytest.mark.asyncio
async def test_minutes_are_bounded() -> None:
    live = controller(FakeManager(), [])
    for minutes in (0, 61):
        with pytest.raises(ValueError):
            await live.start(REQUEST, "https://www.facebook.com/", minutes)


@pytest.mark.asyncio
async def test_live_api_requires_the_token_and_maps_errors() -> None:
    manager, viewers = FakeManager(), []
    app = create_app(manager, "t" * 32, controller(manager, viewers))  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    auth = {"Authorization": "Bearer " + "t" * 32}
    body = {"profile_id": "profile-1", "profile_name": "facebook-main", "platform": "facebook", "url": "https://www.facebook.com/", "minutes": 5}
    try:
        assert (await client.post("/v1/live", json=body)).status == 401
        started = await client.post("/v1/live", json=body, headers=auth)
        assert started.status == 200 and len((await started.json())["password"]) == 8
        assert (await (await client.get("/v1/live", headers=auth)).json())["profile_id"] == "profile-1"
        assert (await client.post("/v1/live", json=body, headers=auth)).status == 409
        assert (await client.post("/v1/live", json={**body, "minutes": 0}, headers=auth)).status in {400, 409}
        stopped = await client.delete("/v1/live", json={"profile_id": "profile-1"}, headers=auth)
        assert (await stopped.json()) == {"closed": True}
        assert (await client.post("/v1/live", json={"platform": "facebook"}, headers=auth)).status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_viewer_that_does_not_start_is_an_error_and_frees_the_profile() -> None:
    manager, viewers = FakeManager(), []

    async def never_ready(processes: list[str]) -> None:
        raise RuntimeError("websockify exited (1): Address already in use")

    live = LiveViewController(
        manager,  # type: ignore[arg-type]
        start_viewer=lambda password: viewers.append("start") or ["viewer"],
        stop_viewer=lambda processes: viewers.append(f"stop:{processes}"),
        wait_viewer=never_ready,
    )
    with pytest.raises(LiveViewError, match="viewer did not start: websockify exited"):
        await live.start(REQUEST, "https://www.facebook.com/", 5)
    assert viewers == ["start", "stop:['viewer']"]
    assert manager.events[-1] == ("release", "handle:profile-1") and live.status() == {}


@pytest.mark.asyncio
async def test_the_login_check_reads_only_the_open_window_and_answers_a_boolean() -> None:
    manager, viewers = FakeManager(), []
    live = controller(manager, viewers)
    app = create_app(manager, "t" * 32, live)  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    auth = {"Authorization": "Bearer " + "t" * 32}
    body = {"profile_id": "profile-9", "profile_name": "linkedin-main", "platform": "linkedin",
            "url": "https://www.linkedin.com/login", "minutes": 5}
    try:
        # No window open for that profile: 409, nothing checked.
        assert (await client.post("/v1/live/check", json={"profile_id": "profile-9"}, headers=auth)).status == 409
        assert (await client.post("/v1/live", json=body, headers=auth)).status == 200
        assert (await client.post("/v1/live/check", json={"profile_id": "profile-9"})).status == 401
        checked = await client.post("/v1/live/check", json={"profile_id": "profile-9"}, headers=auth)
        assert checked.status == 200 and await checked.json() == {"logged_in": False}
        manager.signed_in = True
        checked = await client.post("/v1/live/check", json={"profile_id": "profile-9"}, headers=auth)
        assert await checked.json() == {"logged_in": True}
        assert (await client.post("/v1/live/check", json={"profile_id": "other"}, headers=auth)).status == 409
        assert ("logged_in", "handle:profile-9") in manager.events
    finally:
        await live.stop()
        await client.close()
