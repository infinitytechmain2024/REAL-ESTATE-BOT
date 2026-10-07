"""Agent Reach path: read a public page in the Browser Session Manager when plain HTTP shows nothing.

Some portals send an empty shell and draw the listing with JavaScript: the
plain fetch succeeds (HTTP 200) but has no readable text or no listing links.
Only then the page is read once more in a real browser, through the same
Browser Session Manager Agent Reach uses: a dedicated ``website`` profile with
no login and no cookies of any account, one page per lease, public hosts only
(checked by the manager), a hard timeout.

A page the site refused over plain HTTP (403/429) is also rendered here, unless
WEB_SEARCH_RENDER_ON_REFUSAL / WEB_SEARCH_RENDER_INDEX_ON_REFUSAL are off; a
robots.txt disallow is still respected, never worked around.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from bot.verification.classify import classify_website

from .urls import host_of

log = logging.getLogger(__name__)
PROFILE_ID = "web-search-render"


@dataclass(frozen=True, slots=True)
class RenderedPage:
    url: str
    title: str
    text: str
    links: tuple[tuple[str, str], ...] = ()   # (href, anchor text)
    jsonld: tuple[str, ...] = ()
    frames: tuple[str, ...] = ()   # iframe / script sources the browser saw (to recognise a challenge page)


class RenderError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code[:80]


class ChallengeDetected(RuntimeError):
    """The rendered page is a CAPTCHA / anti-bot interstitial, not the page asked for.

    ``kind``: captcha | interstitial | access_denied; ``url``: the page to open again after a person passed the
    check; ``host``: its site. Only raised when the renderer was built with ``detect_challenges`` (the web stage's
    human verification, WEB_SEARCH_HUMAN_VERIFICATION): nothing here tries to pass the check.
    """

    def __init__(self, kind: str, url: str, host: str) -> None:
        super().__init__(f"challenge:{kind}:{host}")
        self.kind, self.url, self.host = kind, url, host


class Renderer(Protocol):
    async def render(self, url: str) -> RenderedPage: ...


class Browser(Protocol):
    async def acquire(self, profile_id: str, profile_name: str, persisted_state: str, *, platform: str = "facebook") -> Any: ...
    async def snapshot(self, lease: Any, url: str, timeout_ms: int) -> dict[str, Any]: ...
    async def release(self, lease: Any, next_state: str = "READY") -> None: ...


ProfileSource = Callable[[], Awaitable[tuple[str, str]]]


class BrowserRenderer:
    """``profile_source``: an async ``() -> (profile id, profile name)``, asked once. With human verification the
    profile is a ``browser_profiles`` row, so the verification flow's live browser and watchdog open the same
    browser profile (and its cookies) this renderer reads with."""

    def __init__(self, browser: Browser, *, timeout_seconds: float = 30, profile_id: str = PROFILE_ID,
                 profile_source: ProfileSource | None = None, detect_challenges: bool = False) -> None:
        if not 5 <= timeout_seconds <= 60:
            raise ValueError("unsafe render timeout")
        self.browser, self.timeout_ms, self.profile_id = browser, int(timeout_seconds * 1000), profile_id
        self.profile_name, self.profile_source, self.detect_challenges = profile_id, profile_source, detect_challenges

    async def _profile(self) -> tuple[str, str]:
        if self.profile_source is not None:
            self.profile_id, self.profile_name = await self.profile_source()
            self.profile_source = None
        return self.profile_id, self.profile_name

    async def render(self, url: str) -> RenderedPage:
        try:
            profile_id, profile_name = await self._profile()
            lease = await self.browser.acquire(profile_id, profile_name, "ready", platform="website")
        except Exception as exc:
            raise RenderError(f"browser_unavailable:{type(exc).__name__}") from exc
        try:
            snapshot = await self.browser.snapshot(lease, url, self.timeout_ms)
        except Exception as exc:
            raise RenderError(f"render_failed:{type(exc).__name__}") from exc
        finally:
            try:
                await self.browser.release(lease)
            except Exception:  # noqa: BLE001 - the lease expires on its own
                log.warning("web_search.render_release_failed")
        if self.detect_challenges:
            kind = classify_website({**snapshot, "url": snapshot.get("url") or url})
            if kind is not None:
                raise ChallengeDetected(kind, url, host_of(url))
        return page_of(snapshot, url)


def page_of(snapshot: dict[str, Any], url: str) -> RenderedPage:
    links = tuple((str(item.get("url") or ""), str(item.get("text") or ""))
                  for item in (snapshot.get("links") or []) if isinstance(item, dict) and item.get("url"))
    jsonld = tuple(str(blob) for blob in (snapshot.get("jsonld") or []) if isinstance(blob, str))
    frames = tuple(str(src)[:300] for src in (snapshot.get("frames") or []) if isinstance(src, str))
    return RenderedPage(str(snapshot.get("url") or url), str(snapshot.get("title") or "")[:300],
                        str(snapshot.get("text") or ""), links, jsonld, frames)
