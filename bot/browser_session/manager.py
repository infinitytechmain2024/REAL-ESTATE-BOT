"""Redis-lease and flock protected persistent Playwright browser sessions."""

from __future__ import annotations

import asyncio
import fcntl
import ipaddress
import logging
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from .models import BrowserProfileStatus, ProfileRequest, ProfileStatus, SessionHandle

log = logging.getLogger(__name__)
MAX_NAVIGATION_MS = 60_000
# Facebook renders its feed after DOMContentLoaded. These bound the extra time
# a snapshot may spend waiting for posts; callers budget for them (see the
# collector's SNAPSHOT_OVERHEAD_SECONDS).
FEED_WAIT_MS = 10_000
FEED_SCROLLS = 3
FEED_SCROLL_PAUSE_MS = 1_500


class RedisLease(Protocol):
    async def set(self, name: str, value: str, *, nx: bool, ex: int) -> bool | None: ...
    async def get(self, name: str) -> str | None: ...
    async def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> Any: ...
    async def ping(self) -> bool: ...


class BrowserLauncher(Protocol):
    async def __call__(self, profile_dir: Path) -> Any: ...


class SessionBusyError(RuntimeError):
    """A profile has a live Redis lease or an in-process filesystem lock."""


class ProfileUnavailableError(RuntimeError):
    """The requested persisted profile state cannot safely start a browser."""


class _ManagedContext:
    """Close both a persistent context and its owning Playwright process."""

    def __init__(self, context: Any, playwright: Any) -> None:
        self._context, self._playwright = context, playwright

    def __getattr__(self, name: str) -> Any:
        return getattr(self._context, name)

    async def close(self) -> None:
        await self._context.close()
        await self._playwright.stop()


_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


class BrowserSessionManager:
    """Owns browsers; callers receive opaque lease tokens, never profile paths.

    `flock` is released by the kernel on a crash. Redis leases expire without
    a clean shutdown; the renewal loop stops extending them if this process is
    unhealthy. Both properties make stale locks self-healing.
    """

    def __init__(
        self,
        redis: RedisLease,
        *,
        profile_root: Path,
        screenshot_root: Path,
        lease_seconds: int = 120,
        renew_seconds: int = 30,
        idle_seconds: int = 300,
        launcher: BrowserLauncher | None = None,
        state_changed: Callable[[str, BrowserProfileStatus], Awaitable[None]] | None = None,
    ) -> None:
        if renew_seconds >= lease_seconds:
            raise ValueError("renew_seconds must be shorter than lease_seconds")
        if idle_seconds <= renew_seconds:
            raise ValueError("idle_seconds must be longer than renew_seconds")
        self.redis, self.profile_root, self.screenshot_root = redis, profile_root, screenshot_root
        self.lease_seconds, self.renew_seconds, self.idle_seconds = lease_seconds, renew_seconds, idle_seconds
        self.launcher = launcher or self._playwright_launcher
        self.state_changed = state_changed
        self._sessions: dict[str, tuple[SessionHandle, int, Any, asyncio.Task[None]]] = {}
        self._platforms: dict[str, str] = {}
        # A crashed caller never releases; renewal stops once a session has
        # been idle this long, so the profile cannot stay locked indefinitely.
        self._last_used: dict[str, float] = {}
        # One navigation at a time per profile: a caller that gave up on a slow
        # snapshot must not have its page reused mid-load by the next request.
        self._page_locks: dict[str, asyncio.Lock] = {}
        self._background: set[asyncio.Task[None]] = set()  # keep idle releases alive until done
        self._closing = False

    @staticmethod
    def _safe_component(value: str) -> str:
        if not value or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in value):
            raise ValueError("profile id/name may contain only letters, digits, underscores, and hyphens")
        return value

    def _paths(self, request: ProfileRequest) -> tuple[Path, Path]:
        profile = self.profile_root / self._safe_component(request.profile_id)
        root = self.profile_root.resolve()
        if root not in profile.resolve().parents:
            raise ValueError("profile directory escapes configured root")
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(profile, 0o700)
        return profile, profile / ".session.lock"

    @staticmethod
    def _key(profile_id: str) -> str:
        return f"browser-session:profile:{profile_id}"

    async def acquire(self, request: ProfileRequest, *, persisted_state: str = "ready") -> SessionHandle:
        if self._closing:
            raise RuntimeError("browser session manager is shutting down")
        if persisted_state not in {"ready", "in_use"}:
            raise ProfileUnavailableError(f"profile is {persisted_state}, not ready")
        if request.profile_id in self._sessions:
            raise SessionBusyError("profile is already owned by this manager")
        profile_dir, lock_path = self._paths(request)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise SessionBusyError("profile filesystem lock is held") from exc
        token = uuid.uuid4().hex
        acquired = await self.redis.set(self._key(request.profile_id), token, nx=True, ex=self.lease_seconds)
        if not acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise SessionBusyError("profile Redis lease is held")
        try:
            browser = await self.launcher(profile_dir)
            handle = SessionHandle(request.profile_id, token, profile_dir, time.time() + self.lease_seconds)
            renewal = asyncio.create_task(self._renew_forever(handle), name=f"browser-lease-{request.profile_id}")
            self._sessions[request.profile_id] = (handle, fd, browser, renewal)
            self._platforms[request.profile_id] = request.platform
            self._last_used[request.profile_id] = time.monotonic()
            self._page_locks[request.profile_id] = asyncio.Lock()
            await self._report(request.profile_id, BrowserProfileStatus.IN_USE)
            return handle
        except BaseException:
            await self.redis.eval(_RELEASE_SCRIPT, 1, self._key(request.profile_id), token)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise

    async def release(self, handle: SessionHandle, *, next_state: BrowserProfileStatus = BrowserProfileStatus.READY) -> None:
        owned = self._sessions.get(handle.profile_id)
        if not owned or owned[0].token != handle.token:
            raise PermissionError("session token does not own this profile")
        _, fd, browser, renewal = self._sessions.pop(handle.profile_id)
        self._platforms.pop(handle.profile_id, None)
        self._last_used.pop(handle.profile_id, None)
        self._page_locks.pop(handle.profile_id, None)
        renewal.cancel()
        with suppress(asyncio.CancelledError):
            await renewal
        try:
            close = getattr(browser, "close", None)
            if close:
                result = close()
                if hasattr(result, "__await__"):
                    await result
        finally:
            await self.redis.eval(_RELEASE_SCRIPT, 1, self._key(handle.profile_id), handle.token)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            await self._report(handle.profile_id, next_state)

    async def screenshot(self, handle: SessionHandle) -> Path:
        owned = self._sessions.get(handle.profile_id)
        if not owned or owned[0].token != handle.token:
            raise PermissionError("session token does not own this profile")
        self._touch(handle.profile_id)
        browser = owned[2]
        pages = getattr(browser, "pages", [])
        page = pages[0] if pages else await browser.new_page()
        target = self.screenshot_root / self._safe_component(handle.profile_id)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target, 0o700)
        image = target / f"{int(time.time() * 1000)}-{uuid.uuid4().hex}.png"
        await page.screenshot(path=str(image), full_page=False)
        os.chmod(image, 0o600)
        return image

    async def snapshot(self, handle: SessionHandle, url: str, *, timeout_ms: int = 30_000) -> dict[str, Any]:
        """Return a bounded DOM snapshot through the owner of the leased browser.

        This deliberately exposes no arbitrary JavaScript, CDP endpoint, cookies,
        or profile path.  It is the only navigation primitive collectors receive.
        """
        from urllib.parse import urlparse

        owned = self._sessions.get(handle.profile_id)
        if not owned or owned[0].token != handle.token:
            raise PermissionError("session token does not own this profile")
        parsed = urlparse(url)
        platform = self._platforms.get(handle.profile_id)
        approved_hosts = {
            "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com"},
            "instagram": {"instagram.com", "www.instagram.com"},
            "tiktok": {"tiktok.com", "www.tiktok.com", "m.tiktok.com"},
        }
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("snapshot URL must be HTTPS with a hostname")
        if platform in approved_hosts and parsed.hostname not in approved_hosts[platform]:
            raise ValueError(f"snapshot URL is not approved for {platform}")
        if platform == "website" and (
            parsed.hostname in {"localhost", "metadata.google.internal"}
            or parsed.hostname.startswith("127.")
            or parsed.hostname.startswith("169.254.")
        ):
            raise ValueError("website snapshot cannot target local or link-local hosts")
        if platform == "website":
            try:
                addresses = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise ValueError("website hostname could not be resolved safely") from exc
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise ValueError("website hostname resolves to a non-public address")
        if platform not in {*approved_hosts, "website"}:
            raise ValueError("snapshot profile platform is unsupported")
        if not 1_000 <= timeout_ms <= MAX_NAVIGATION_MS:
            raise ValueError(f"timeout_ms must be between 1000 and {MAX_NAVIGATION_MS}")
        self._touch(handle.profile_id)
        async with self._page_locks[handle.profile_id]:
            browser = owned[2]
            pages = getattr(browser, "pages", [])
            page = pages[0] if pages else await browser.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if platform == "facebook":
                await self._settle_feed(page)
            self._touch(handle.profile_id)
            return await self._extract(page)

    @staticmethod
    async def _settle_feed(page: Any) -> None:
        """Give Facebook's client-rendered feed a bounded chance to appear.

        No posts after the wait is a normal outcome (empty, private, or
        challenge page), so a timeout here is not an error.
        """
        try:
            await page.wait_for_selector('[role="article"]', timeout=FEED_WAIT_MS)
        except Exception as exc:
            if type(exc).__name__ != "TimeoutError":
                raise
            return
        for _ in range(FEED_SCROLLS):
            await page.mouse.wheel(0, 2_500)
            await page.wait_for_timeout(FEED_SCROLL_PAUSE_MS)

    @staticmethod
    async def _extract(page: Any) -> dict[str, Any]:
        # Keep the payload small and evidence-focused.  Facebook's markup is
        # volatile, so the collector treats this as a candidate feed, not truth.
        return await page.evaluate(
            """() => ({
              url: location.href,
              title: document.title.slice(0, 500),
              text: (document.body?.innerText || '').slice(0, 120000),
              posts: Array.from(document.querySelectorAll('[role="article"]')).slice(0, 20).map((node) => {
                const link = Array.from(node.querySelectorAll('a[href]')).map(a => a.href)
                  .find(href => /\\/(posts|permalink)\\//.test(href)) || null;
                const time = node.querySelector('time');
                return {url: link, text: (node.innerText || '').slice(0, 12000), published_at: time?.dateTime || null};
              })
            })"""
        )

    async def status(self, profile_id: str, *, persisted_state: str = "ready") -> ProfileStatus:
        if profile_id in self._sessions:
            return ProfileStatus(profile_id, BrowserProfileStatus.IN_USE, self._sessions[profile_id][0].expires_at)
        if persisted_state != "ready":
            state_map = {
                "human_verification_required": BrowserProfileStatus.VERIFICATION_REQUIRED,
                "quarantined": BrowserProfileStatus.QUARANTINED,
                "disabled": BrowserProfileStatus.DISABLED,
                "retired": BrowserProfileStatus.RETIRED,
                "provisioned": BrowserProfileStatus.PROVISIONED,
            }
            return ProfileStatus(profile_id, state_map.get(persisted_state, BrowserProfileStatus.LOCKED))
        if await self.redis.get(self._key(profile_id)):
            return ProfileStatus(profile_id, BrowserProfileStatus.LOCKED, detail="live lease owned elsewhere")
        return ProfileStatus(profile_id, BrowserProfileStatus.READY)

    async def health(self) -> bool:
        try:
            return bool(await self.redis.ping()) and not self._closing
        except Exception:  # noqa: BLE001 - health must report dependency failure, not crash endpoint
            return False

    async def close(self) -> None:
        self._closing = True
        for handle, *_ in list(self._sessions.values()):
            await self.release(handle)

    def _touch(self, profile_id: str) -> None:
        self._last_used[profile_id] = time.monotonic()

    async def _renew_forever(self, handle: SessionHandle) -> None:
        try:
            while True:
                await asyncio.sleep(self.renew_seconds)
                idle = time.monotonic() - self._last_used.get(handle.profile_id, 0.0)
                if idle > self.idle_seconds:
                    # The caller has gone quiet (crashed or killed). Release
                    # from a separate task: release() awaits this one.
                    log.warning("browser_session.idle_release", extra={"profile_id": handle.profile_id, "idle_seconds": int(idle)})
                    task = asyncio.get_running_loop().create_task(self._release_idle(handle))
                    self._background.add(task)
                    task.add_done_callback(self._background.discard)
                    return
                extended = await self.redis.eval(
                    _RENEW_SCRIPT, 1, self._key(handle.profile_id), handle.token, self.lease_seconds
                )
                if not extended:
                    # Lease loss means the browser is no longer trusted. Leave it
                    # closed on release; callers see an error through their next action.
                    return
        except asyncio.CancelledError:
            raise

    async def _release_idle(self, handle: SessionHandle) -> None:
        with suppress(PermissionError):
            await self.release(handle, next_state=BrowserProfileStatus.READY)

    async def _report(self, profile_id: str, state: BrowserProfileStatus) -> None:
        if self.state_changed:
            await self.state_changed(profile_id, state)

    @staticmethod
    async def _playwright_launcher(profile_dir: Path) -> Any:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir), headless=False, viewport={"width": 1440, "height": 1000}
        )
        return _ManagedContext(context, playwright)
