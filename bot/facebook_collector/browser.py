"""Authenticated, narrow client for Browser Session Manager."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp

# The Browser Session Manager caps navigation at 60 s, then may wait up to
# ~15 s for Facebook's feed to render and extract it. The HTTP request for a
# snapshot must outlive both, or the client gives up while the page still loads.
MAX_NAVIGATION_SECONDS = 60
SNAPSHOT_OVERHEAD_SECONDS = 25


@dataclass(frozen=True)
class BrowserLease:
    profile_id: str
    token: str


class BrowserSessionClient:
    def __init__(self, base_url: str, api_token: str, *, timeout_seconds: int = 45) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_token}"}
        self.timeout_seconds = timeout_seconds

    async def acquire(self, profile_id: str, profile_name: str, persisted_state: str, *, platform: str = "facebook") -> BrowserLease:
        data = await self._request("POST", "/v1/sessions", {
            "profile_id": profile_id, "profile_name": profile_name, "platform": platform,
            "persisted_state": persisted_state,
        })
        return BrowserLease(profile_id, str(data["session_token"]))

    async def snapshot(self, lease: BrowserLease, url: str, timeout_ms: int) -> dict[str, Any]:
        return await self._request("POST", "/v1/sessions/snapshot", {
            "profile_id": lease.profile_id, "session_token": lease.token, "url": url, "timeout_ms": timeout_ms,
        }, timeout_seconds=timeout_ms / 1000 + SNAPSHOT_OVERHEAD_SECONDS)

    async def screenshot(self, lease: BrowserLease) -> bytes:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout, headers=self.headers) as session, session.post(f"{self.base_url}/v1/sessions/screenshot", json={
                "profile_id": lease.profile_id, "session_token": lease.token,
            }) as response:
            response.raise_for_status()
            return await response.read()

    async def release(self, lease: BrowserLease, next_state: str = "READY") -> None:
        await self._request("DELETE", "/v1/sessions", {
            "profile_id": lease.profile_id, "session_token": lease.token, "next_state": next_state,
        })

    async def health(self) -> bool:
        try:
            result = await self._request("GET", "/healthz", None, authenticated=False)
            return result == {"ok": True}
        except aiohttp.ClientError:
            return False

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None, *, authenticated: bool = True, timeout_seconds: float | None = None) -> dict[str, Any]:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds or self.timeout_seconds)
        headers = self.headers if authenticated else None
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session, session.request(method, f"{self.base_url}{path}", json=payload) as response:
            response.raise_for_status()
            return await response.json()
