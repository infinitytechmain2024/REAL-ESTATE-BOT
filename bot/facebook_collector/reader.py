"""Translate Browser Session DOM snapshots into bounded post evidence."""

from __future__ import annotations

import hashlib
from urllib.parse import urlparse, urlunparse

from .browser import BrowserLease, BrowserSessionClient
from .challenges import detect_challenge
from .models import ChallengeDetected, CollectedPost, GroupRead, GroupState


def canonical_post_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))


def _post_id(url: str) -> str:
    return hashlib.sha256(canonical_post_url(url).encode()).hexdigest()


class FacebookGroupReader:
    def __init__(self, browser: BrowserSessionClient, *, max_posts: int, timeout_seconds: int) -> None:
        self.browser, self.max_posts, self.timeout_seconds = browser, max_posts, timeout_seconds

    async def read(self, lease: BrowserLease, group_url: str) -> GroupRead:
        snapshot = await self.browser.snapshot(lease, group_url, self.timeout_seconds * 1000)
        challenge = detect_challenge(snapshot)
        if challenge:
            raise ChallengeDetected(challenge, snapshot)
        text = str(snapshot.get("text", "")).lower()
        if any(value in text for value in ("content isn't available", "this content isn't available", "private group")):
            return GroupRead(GroupState.INACCESSIBLE, (), snapshot)
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
        return GroupRead(GroupState.ACTIVE if posts else GroupState.INACTIVE, tuple(posts), snapshot)

