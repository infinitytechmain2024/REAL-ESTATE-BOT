"""Bot-driven live view: a human logs a profile in or clears a checkpoint.

The Telegram control plane asks for a live view through the internal API; this
controller opens the profile's page under the same Redis lease and ``flock``
every collector takes, starts x11vnc/noVNC behind a one-time password, keeps
the lease alive while a human (not an API caller) drives the browser, and
closes everything at the deadline. It never types, clicks, or solves anything:
what happens inside the window is entirely the human's.

There is one virtual display, so there is at most one live view, and none
while any other session is open: a collector's window would otherwise appear
on the operator's screen.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from . import interactive
from .manager import BrowserSessionManager
from .models import ProfileRequest, SessionHandle

log = logging.getLogger(__name__)
MAX_MINUTES = interactive.MAX_MINUTES
KEEPALIVE_SECONDS = 15


class LiveViewError(RuntimeError):
    """The live view cannot be started now; the message is safe to show."""


@dataclass
class _LiveView:
    profile_id: str
    handle: SessionHandle
    expires_at: float
    viewer: list[subprocess.Popen[bytes]]
    keepalive: asyncio.Task[None] | None = None


class LiveViewController:
    def __init__(
        self,
        manager: BrowserSessionManager,
        *,
        start_viewer: Callable[[str], list[Any]] | None = None,
        stop_viewer: Callable[[list[Any]], None] | None = None,
        keepalive_seconds: float = KEEPALIVE_SECONDS,
    ) -> None:
        self.manager = manager
        # Looked up at call time so tests can replace interactive's helpers.
        self._start_viewer = start_viewer or (lambda password: interactive._start_viewer(password))
        self._stop_viewer = stop_viewer or (lambda processes: interactive._stop(processes))
        self.keepalive_seconds = keepalive_seconds
        self._current: _LiveView | None = None
        self._lock = asyncio.Lock()

    def status(self) -> dict[str, Any]:
        view = self._current
        return {"profile_id": view.profile_id, "expires_at": view.expires_at} if view else {}

    async def start(self, request: ProfileRequest, url: str, minutes: int) -> dict[str, Any]:
        if not 1 <= minutes <= MAX_MINUTES:
            raise ValueError(f"minutes must be between 1 and {MAX_MINUTES}")
        async with self._lock:
            if self._current is not None:
                raise LiveViewError("a live view is already open")
            if self.manager.active_profiles():
                raise LiveViewError("another browser session is running; try again when it finishes")
            handle = await self.manager.acquire(request)
            viewer: list[Any] = []
            try:
                try:
                    # The same host-restricted navigation collectors use. A slow
                    # page is still worth showing: the human can reload it.
                    await self.manager.snapshot(handle, url, timeout_ms=60_000)
                except Exception as exc:
                    if type(exc).__name__ != "TimeoutError":
                        raise
                password = secrets.token_urlsafe(9)[:8]  # VNC reads at most 8 characters
                viewer = self._start_viewer(password)
            except BaseException:
                self._stop_viewer(viewer)
                await self.manager.release(handle)
                raise
            view = _LiveView(request.profile_id, handle, time.time() + minutes * 60, viewer)
            view.keepalive = asyncio.create_task(self._keepalive(view), name=f"live-view-{request.profile_id}")
            self._current = view
            log.info("browser_session.live_view_started", extra={"profile_id": request.profile_id, "minutes": minutes})
            return {"profile_id": request.profile_id, "password": password, "expires_at": view.expires_at}

    async def stop(self, profile_id: str | None = None) -> bool:
        async with self._lock:
            view = self._current
            if view is None or (profile_id is not None and view.profile_id != profile_id):
                return False
            self._current = None
        if view.keepalive is not None and view.keepalive is not asyncio.current_task():
            view.keepalive.cancel()
            with suppress(asyncio.CancelledError):
                await view.keepalive
        try:
            self._stop_viewer(view.viewer)
        finally:
            # Closing the persistent context flushes the login into the profile.
            with suppress(PermissionError):
                await self.manager.release(view.handle)
        log.info("browser_session.live_view_stopped", extra={"profile_id": view.profile_id})
        return True

    async def _keepalive(self, view: _LiveView) -> None:
        while time.time() < view.expires_at:
            try:
                self.manager.touch(view.handle)
            except PermissionError:
                return  # the lease was lost; stop() or the manager cleans up
            await asyncio.sleep(min(self.keepalive_seconds, max(0.0, view.expires_at - time.time())))
        await self.stop(view.profile_id)
