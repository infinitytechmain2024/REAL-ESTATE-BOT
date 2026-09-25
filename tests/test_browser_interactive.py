"""The operator login session always stops its viewer and releases the lease."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from bot.browser_session import interactive


class FakeManager:
    instances: list[FakeManager] = []  # noqa: RUF012 - test registry

    def __init__(self, _redis: object, **kwargs: Any) -> None:
        self.kwargs, self.events = kwargs, []
        FakeManager.instances.append(self)

    async def acquire(self, request: Any) -> str:
        self.events.append(("acquire", request.profile_id, request.platform))
        return "handle"

    async def snapshot(self, handle: str, url: str, *, timeout_ms: int) -> dict[str, object]:
        if url.endswith("/broken"):
            raise ValueError("navigation failed")
        self.events.append(("open", url))
        return {}

    async def release(self, handle: str) -> None:
        self.events.append(("release", handle))


class FakeRedis:
    async def aclose(self) -> None:
        return None


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    viewer: list[str] = []
    monkeypatch.setenv("REDIS_URL", "redis://example")
    monkeypatch.setenv("BROWSER_SESSION_API_TOKEN", "t" * 32)
    monkeypatch.setenv("BROWSER_PROFILE_ROOT", str(tmp_path))
    monkeypatch.setattr(interactive, "BrowserSessionManager", FakeManager)
    monkeypatch.setattr(interactive.redis, "from_url", lambda *_a, **_k: FakeRedis())
    monkeypatch.setattr(interactive, "_start_viewer", lambda password: viewer.append(password) or ["viewer"])
    monkeypatch.setattr(interactive, "_stop", lambda processes: viewer.append(f"stopped:{processes}"))

    async def ready(processes: list[str]) -> None:
        viewer.append(f"ready:{processes}")

    monkeypatch.setattr(interactive, "_wait_viewer", ready)
    FakeManager.instances.clear()
    return viewer


@pytest.mark.asyncio
async def test_session_opens_the_page_then_stops_the_viewer_and_releases(patched: list[str]) -> None:
    await interactive.run("profile-1", "facebook", "https://www.facebook.com/", minutes=0.001)  # type: ignore[arg-type]
    manager = FakeManager.instances[0]
    assert manager.events == [("acquire", "profile-1", "facebook"), ("open", "https://www.facebook.com/"), ("release", "handle")]
    assert len(patched[0]) == 8 and patched[1:] == ["ready:['viewer']", "stopped:['viewer']"]
    # A human drives this browser: idle release must not end the session early.
    assert manager.kwargs["idle_seconds"] >= 60


@pytest.mark.asyncio
async def test_a_failed_navigation_still_releases_and_never_starts_the_viewer(patched: list[str]) -> None:
    with pytest.raises(ValueError):
        await interactive.run("profile-1", "facebook", "https://www.facebook.com/broken", minutes=1)
    assert FakeManager.instances[0].events[-1] == ("release", "handle")
    assert patched == ["stopped:[]"]


class FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


@pytest.mark.asyncio
async def test_the_viewer_counts_as_ready_only_once_novnc_listens(monkeypatch: pytest.MonkeyPatch) -> None:
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    monkeypatch.setattr(interactive, "NOVNC_PORT", server.sockets[0].getsockname()[1])
    async with server:
        await interactive._wait_viewer([FakeProcess(), FakeProcess()], timeout=5)


@pytest.mark.asyncio
async def test_a_viewer_that_exits_is_reported_with_its_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    logs = (str(tmp_path / "x11vnc.log"), str(tmp_path / "websockify.log"))
    Path(logs[1]).write_text("socket.error: [Errno 98] Address already in use")
    monkeypatch.setattr(interactive, "VIEWER_LOGS", logs)
    monkeypatch.setattr(interactive, "NOVNC_PORT", 9)  # nothing listens
    with pytest.raises(RuntimeError, match=r"websockify exited .*Address already in use"):
        await interactive._wait_viewer([FakeProcess(), FakeProcess(1)], timeout=5)


@pytest.mark.asyncio
async def test_a_viewer_that_never_listens_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interactive, "NOVNC_PORT", 9)
    with pytest.raises(RuntimeError, match="did not start listening"):
        await interactive._wait_viewer([FakeProcess(), FakeProcess()], timeout=0.5)
