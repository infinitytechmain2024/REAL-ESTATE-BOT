"""The browser service, seen from the verification flow.

``LiveBrowser`` opens the profile's own browser for a human (the browser
service's ``/v1/live``). ``RecoveryWatchdog`` is the only judge of "solved":
after the human is done it takes the profile's lease itself, loads the page
that was challenged, and says whether a challenge is still showing.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import aiohttp

from bot.facebook_collector.browser import BrowserSessionClient
from bot.facebook_collector.challenges import detect_challenge

from .classify import classify_snapshot, classify_website
from .models import WEB_JOB_TYPE, Recovery

log = logging.getLogger(__name__)
RECOVERY_NAVIGATION_MS = 45_000


class BrowserUnavailable(RuntimeError):
    """Safe to show to the operator."""


class LiveBrowser(Protocol):
    async def start(self, profile_id: str, profile_name: str, platform: str, url: str, minutes: int) -> str: ...
    async def stop(self, profile_id: str) -> None: ...
    async def is_open(self, profile_id: str) -> bool: ...


class RecoveryChecker(Protocol):
    async def check(self, profile_id: str, profile_name: str, platform: str, url: str, job_type: str = "") -> Recovery: ...


class BrowserLiveClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/live"
        self._headers = {"Authorization": f"Bearer {token}"}

    async def start(self, profile_id: str, profile_name: str, platform: str, url: str, minutes: int) -> str:
        body = {"profile_id": profile_id, "profile_name": profile_name, "platform": platform, "url": url, "minutes": minutes}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90), headers=self._headers) as http, http.post(self._url, json=body) as response:
                if response.status == 409:
                    raise BrowserUnavailable(f"the browser is busy: {(await response.text())[:200]}")
                if response.status != 200:
                    raise BrowserUnavailable(f"the browser service returned HTTP {response.status}")
                return str((await response.json())["password"])
        except aiohttp.ClientError as exc:
            raise BrowserUnavailable("the browser service is not reachable") from exc

    async def is_open(self, profile_id: str) -> bool:
        """Whether the browser service still shows this profile's window (a restart closes it)."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=self._headers) as http, http.get(self._url) as response:
                return response.status == 200 and (await response.json()).get("profile_id") == profile_id
        except (aiohttp.ClientError, ValueError):
            return False

    async def stop(self, profile_id: str) -> None:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30), headers=self._headers) as http, http.delete(self._url, json={"profile_id": profile_id}):
                pass
        except aiohttp.ClientError:
            log.warning("verification.live_stop_failed", extra={"profile_id": profile_id})


class RecoveryWatchdog:
    def __init__(self, client: BrowserSessionClient) -> None:
        self.client = client

    async def check(self, profile_id: str, profile_name: str, platform: str, url: str, job_type: str = "") -> Recovery:
        try:
            lease = await self.client.acquire(profile_id, profile_name, "ready", platform=platform)
        except aiohttp.ClientError as exc:
            return Recovery(False, reason=f"browser_unavailable:{type(exc).__name__}")
        try:
            snapshot: dict[str, Any] = await self.client.snapshot(lease, url, RECOVERY_NAVIGATION_MS)
        except aiohttp.ClientError as exc:
            return Recovery(False, reason=f"navigation_failed:{type(exc).__name__}")
        finally:
            try:
                await self.client.release(lease, "READY")
            except aiohttp.ClientError:
                log.warning("verification.recovery_release_failed", extra={"profile_id": profile_id})
        return judge_website(snapshot) if job_type == WEB_JOB_TYPE else judge(snapshot)


def judge(snapshot: dict[str, Any]) -> Recovery:
    """Clear only when neither the collector's detector nor ours sees a challenge."""
    kind, sensitive = classify_snapshot(snapshot)
    reason = detect_challenge(snapshot)
    if reason is None and kind is None:
        return Recovery(True)
    return Recovery(False, kind=kind or "unknown", sensitive=sensitive, reason=reason or kind)


def judge_website(snapshot: dict[str, Any]) -> Recovery:
    """A public website: clear only when the reloaded page is no CAPTCHA / anti-bot page (Facebook's signals such as
    a ``/login`` link do not apply to a site's own pages)."""
    kind = classify_website(snapshot)
    return Recovery(True) if kind is None else Recovery(False, kind=kind, reason=f"website_{kind}")
