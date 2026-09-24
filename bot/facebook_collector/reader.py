"""Translate Browser Session DOM snapshots into bounded post evidence."""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse, urlunparse

from .browser import (
    MAX_NAVIGATION_SECONDS,
    SNAPSHOT_OVERHEAD_SECONDS,
    BrowserLease,
    BrowserSessionClient,
)
from .challenges import detect_challenge
from .models import ChallengeDetected, CollectedPost, GroupRead, GroupState


def canonical_post_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))


def _post_id(url: str) -> str:
    return hashlib.sha256(canonical_post_url(url).encode()).hexdigest()


# Only meaningful when the page yielded no posts: every private group, joined
# or not, shows "Private group" in its header.
_INACCESSIBLE_SIGNALS = ("content isn't available", "this content isn't available", "private group")


def navigation_timeout_ms(group_timeout_seconds: int) -> int:
    """Navigation budget that leaves room for feed rendering inside the group timeout."""
    budget = min(MAX_NAVIGATION_SECONDS, group_timeout_seconds - SNAPSHOT_OVERHEAD_SECONDS - 5)
    return max(5, budget) * 1000


class FacebookGroupReader:
    def __init__(self, browser: BrowserSessionClient, *, max_posts: int, timeout_seconds: int) -> None:
        self.browser, self.max_posts, self.timeout_seconds = browser, max_posts, timeout_seconds

    async def read(self, lease: BrowserLease, group_url: str) -> GroupRead:
        snapshot = await self.browser.snapshot(lease, group_url, navigation_timeout_ms(self.timeout_seconds))
        challenge = detect_challenge(snapshot)
        if challenge:
            raise ChallengeDetected(challenge, snapshot)
        raw_posts = snapshot.get("posts", [])
        posts: list[CollectedPost] = []
        seen: set[str] = set()
        if isinstance(raw_posts, list):
            for raw in raw_posts[: self.max_posts]:
                if not isinstance(raw, dict) or not isinstance(raw.get("url"), str):
                    continue
                url = canonical_post_url(raw["url"])
                if url in seen:
                    continue
                seen.add(url)
                posts.append(CollectedPost(_post_id(url), url, str(raw.get("text") or ""), raw.get("published_at")))
        diagnostics = _diagnostics(snapshot, raw_posts, posts)
        if posts:
            return GroupRead(GroupState.ACTIVE, tuple(posts), snapshot, diagnostics)
        text = str(snapshot.get("text", "")).lower()
        if any(signal in text for signal in _INACCESSIBLE_SIGNALS):
            return GroupRead(GroupState.INACCESSIBLE, (), snapshot, diagnostics)
        return GroupRead(GroupState.INACTIVE, (), snapshot, diagnostics)


def _diagnostics(snapshot: dict[str, object], raw_posts: object, posts: list[CollectedPost]) -> dict[str, object]:
    """Explain a read without keeping its content: counts, final URL, title."""
    page = snapshot.get("diagnostics")
    counts = page if isinstance(page, dict) else {}
    final = urlparse(str(snapshot.get("url", "")))
    return {
        "articles": counts.get("articles"),
        "articles_with_post_link": counts.get("articles_with_post_link"),
        "feed_present": counts.get("feed_present"),
        "feed_units": counts.get("feed_units"),
        "candidates": len(raw_posts) if isinstance(raw_posts, list) else 0,
        "posts_kept": len(posts),
        "final_url": urlunparse((final.scheme, final.netloc, final.path, "", "", "")),
        "title": str(snapshot.get("title", ""))[:200],
    }

