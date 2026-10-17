"""The comments under ONE public Facebook post, read through Apify (v2 B1; ``APIFY_FACEBOOK_COMMENTS_ENABLED``).

Public posts only: the actor gets no cookies and no account. Nothing here posts, likes or messages anybody.

The caller (``bot.campaign.post_leads``) drives the run so that every step is resumable and a paid launch is never
repeated: :meth:`launch` (one POST, never retried; a lost answer is ``LaunchUnknown``) -> the caller stores the run id
-> :meth:`wait` (bounded, the run is aborted on timeout) -> :meth:`comments` (the dataset page by page).

Actor ``apify/facebook-comments-scraper`` ($0.0025 per comment + $0.001 per start) -- input: ``startUrls``
``[{"url": ...}]``, ``resultsLimit``, ``includeNestedComments`` (replies, each its own row), ``viewOption``
(``RANKED_UNFILTERED``, the default). ``onlyCommentsNewerThan`` is a paid add-on and is never sent. One dataset row is
one comment; the fields read: ``text``, ``profileName``, ``profileUrl`` (the commenter's public link), ``commentUrl``,
``postTitle`` (the post's own text), ``error`` / ``errorDescription`` (a row saying the post is unreachable: no
``text``). A missing field is ``None``; nothing is guessed.

Actor ``apify/facebook-posts-scraper`` ($0.005 per post): OPTIONAL, only when ``posts_actor_id`` is set (off by
default; ``postTitle`` already carries the text). ``startUrls`` with the post link; the row's ``text`` is read.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

from bot.acquisition.apify_client import (
    TERMINAL,
    ApifyHttp,
    TemporaryApifyError,
    actor_path,
    check_pinned_build,
)
from bot.campaign.leads import Comment, parse_comments

VIEW_OPTION = "RANKED_UNFILTERED"
MAX_POST_TEXT_CHARS = 2000
POST_TEXT_MAX_USD = 0.1  # the posts actor reads one item: its charge cap, never more than the per-post cap


@dataclass(frozen=True, slots=True)
class CommentsConfig:
    actor_id: str = "apify/facebook-comments-scraper"
    build: str = ""                       # pinned build (never «latest» in production); required to enable
    posts_actor_id: str = ""               # optional: the post text normally comes with each comment (``postTitle``)
    max_comments: int = 200
    max_usd_per_post: float = 1.0          # maxTotalChargeUsd of the comments run
    timeout_seconds: float = 300
    page_size: int = 100

    def __post_init__(self) -> None:
        actor_path(self.actor_id)
        if self.posts_actor_id:
            actor_path(self.posts_actor_id)
        check_pinned_build("APIFY_FACEBOOK_COMMENTS_ACTOR_BUILD", self.build)
        if not (1 <= self.max_comments <= 1000 and 1 <= self.page_size <= 1000
                and math.isfinite(self.max_usd_per_post) and 0 < self.max_usd_per_post <= 100
                and 30 <= self.timeout_seconds <= 3600):
            raise ValueError("unsafe apify comments limits")


@dataclass(frozen=True, slots=True)
class PostComments:
    comments: list[Comment]
    post_text: str | None = None
    error: str | None = None  # ``post_unavailable``: the actor reported the post as unreachable


def comment_items(items: list[Any], *, limit: int) -> PostComments:
    """The dataset's items as :class:`Comment` s (a known profile and some text, each person's text once).

    An item with ``error`` and no ``text`` is the actor saying the post cannot be read (closed, removed): when there
    are no comments besides, the result is ``error="post_unavailable"``.
    """
    rows: list[dict[str, Any]] = []
    post_text: str | None = None
    failed = False
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("error") and not item.get("text"):
            failed = True
            continue
        title = item.get("postTitle")
        if post_text is None and isinstance(title, str) and title.strip():
            post_text = " ".join(title.split())[:MAX_POST_TEXT_CHARS]
        rows.append({"author": item.get("profileName"), "author_url": item.get("profileUrl"),
                     "text": item.get("text"), "comment_url": item.get("commentUrl")})
    comments = parse_comments(rows, limit=limit)
    return PostComments(comments, post_text, "post_unavailable" if failed and not rows else None)


class ApifyCommentsSource:
    def __init__(self, http: ApifyHttp, config: CommentsConfig) -> None:
        self.http, self.config = http, config

    def __repr__(self) -> str:
        return f"ApifyCommentsSource(actor={self.config.actor_id})"

    async def aclose(self) -> None:
        await self.http.aclose()

    async def launch(self, url: str, *, posts: bool = False) -> str:
        """Start one run (never retried) and return its id; ``posts``: the posts actor reads the post's text."""
        cfg = self.config
        if posts:
            run = await self.http.launch(cfg.posts_actor_id, {"startUrls": [{"url": url}], "resultsLimit": 1},
                                         max_items=1, max_charge_usd=min(POST_TEXT_MAX_USD, cfg.max_usd_per_post),
                                         timeout_seconds=cfg.timeout_seconds)
        else:
            run = await self.http.launch(
                cfg.actor_id, {"startUrls": [{"url": url}], "resultsLimit": cfg.max_comments,
                               "includeNestedComments": True, "viewOption": VIEW_OPTION},
                build=cfg.build, max_items=cfg.max_comments, max_charge_usd=cfg.max_usd_per_post,
                timeout_seconds=cfg.timeout_seconds)
        return str(run["id"])

    async def wait(self, run_id: str) -> dict[str, Any]:
        """The run once terminal; past the timeout it is aborted (``TemporaryApifyError``)."""
        try:
            async with asyncio.timeout(self.config.timeout_seconds):
                run = await self.http.run(run_id)
                while run.get("status") not in TERMINAL:
                    run = await self.http.run(run_id, wait_seconds=20)
                return run
        except TimeoutError:
            await self.http.abort(run_id)
            raise TemporaryApifyError("apify_wait_timeout") from None

    async def items(self, dataset_id: str, *, limit: int) -> list[Any]:
        """At most ``limit`` dataset items, page by page."""
        items: list[Any] = []
        while len(items) < limit:
            want = min(self.config.page_size, limit - len(items))
            page = await self.http.dataset_page(dataset_id, offset=len(items), limit=want)
            items += page
            if len(page) < want:
                break
        return items

    async def comments(self, dataset_id: str) -> PostComments:
        cfg = self.config
        return comment_items(await self.items(dataset_id, limit=cfg.max_comments), limit=cfg.max_comments)

    async def post_text(self, dataset_id: str) -> str | None:
        for item in await self.items(dataset_id, limit=1):
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str) and text.strip():
                return " ".join(text.split())[:MAX_POST_TEXT_CHARS]
        return None
