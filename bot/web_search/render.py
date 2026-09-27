"""Agent Reach path: read a public page in the Browser Session Manager when plain HTTP shows nothing.

Some portals send an empty shell and draw the listing with JavaScript: the
plain fetch succeeds (HTTP 200) but has no readable text or no listing links.
Only then the page is read once more in a real browser, through the same
Browser Session Manager Agent Reach uses: a dedicated ``website`` profile with
no login and no cookies of any account, one page per lease, public hosts only
(checked by the manager), a hard timeout.

It never retries a page the site refused (403/429, robots.txt): a refusal is
respected, not worked around.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)
PROFILE_ID = "web-search-render"


@dataclass(frozen=True, slots=True)
class RenderedPage:
    url: str
    title: str
    text: str
    links: tuple[tuple[str, str], ...] = ()   # (href, anchor text)
    jsonld: tuple[str, ...] = ()


class RenderError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code[:80]


class Renderer(Protocol):
    async def render(self, url: str) -> RenderedPage: ...


class Browser(Protocol):
    async def acquire(self, profile_id: str, profile_name: str, persisted_state: str, *, platform: str = "facebook") -> Any: ...
    async def snapshot(self, lease: Any, url: str, timeout_ms: int) -> dict[str, Any]: ...
    async def release(self, lease: Any, next_state: str = "READY") -> None: ...


class BrowserRenderer:
    def __init__(self, browser: Browser, *, timeout_seconds: float = 30, profile_id: str = PROFILE_ID) -> None:
        if not 5 <= timeout_seconds <= 60:
            raise ValueError("unsafe render timeout")
        self.browser, self.timeout_ms, self.profile_id = browser, int(timeout_seconds * 1000), profile_id

    async def render(self, url: str) -> RenderedPage:
        try:
            lease = await self.browser.acquire(self.profile_id, self.profile_id, "ready", platform="website")
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
        return page_of(snapshot, url)


def page_of(snapshot: dict[str, Any], url: str) -> RenderedPage:
    links = tuple((str(item.get("url") or ""), str(item.get("text") or ""))
                  for item in (snapshot.get("links") or []) if isinstance(item, dict) and item.get("url"))
    jsonld = tuple(str(blob) for blob in (snapshot.get("jsonld") or []) if isinstance(blob, str))
    return RenderedPage(str(snapshot.get("url") or url), str(snapshot.get("title") or "")[:300],
                        str(snapshot.get("text") or ""), links, jsonld)
