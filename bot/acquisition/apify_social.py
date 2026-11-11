"""What the Instagram and TikTok Apify sources (P1.2, P1.3) share: their settings, the input guard and one run.

Each source builds its actor input from a **white list** of fields (``check_input``): any other key is refused
before the paid launch, and so is a key that could carry a login anywhere in the input (cookies, sessions, headers,
page scripts) -- the actors are only ever given public links, hashtags and words (D1, ``CLAUDE.md`` rule 10).

The run itself is :class:`~bot.acquisition.apify_source.ApifySocialRun` (claim, one launch with
``maxTotalChargeUsd`` and the pinned build, billing once, page-by-page import): a source only maps dataset items.
A mapper takes facts from the actor's own fields and nothing else: no price, area or deal is read out of the text
here (the analysis worker does that against quotes), no name is made up from a username, and a missing value is
``None``.

An item that is the actor's error report (``error``/``errorCode``/``errorDescription`` and no text of its own) is no
post and no comment: when a run gave only such items, :class:`SocialResult` says ``post_unavailable``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Generic, TypeVar
from urllib.parse import urlsplit

from bot.acquisition.apify_client import ApifyHttp
from bot.acquisition.apify_source import ApifySocialRun, RunOutcome, SocialRunConfig
from bot.acquisition.contracts import AcquiredPost, Platform

if TYPE_CHECKING:
    from bot.acquisition.apify_source import SourceRunStore

T = TypeVar("T")
# Keys that could hand an actor a login or a script: never sent, at any depth (D1).
_LOGIN_KEY = re.compile(r"cookie|session|header|pagefunction|token|password|login|auth", re.IGNORECASE)
LOGIN_KEYS = ("cookie", "session", "initialCookies", "customHttpHeaders", "pageFunction")
MAX_TARGETS = 10                     # links, hashtags or queries in one run
MAX_TERM_CHARS = 100
# A 429 on the launch: retried for at most this long within one call without spending an attempt.
RATE_LIMIT_SECONDS = 120.0
RATE_LIMIT_PAUSE_SECONDS = 15.0


def check_input(actor_input: Mapping[str, Any], allowed: Iterable[str]) -> dict[str, Any]:
    """The input as sent, or ``ValueError`` for a key off the white list or a login-like key at any depth."""
    extra = sorted(set(actor_input) - set(allowed))
    if extra:
        raise ValueError(f"actor input keys not allowed: {', '.join(extra)}")
    _no_login_keys(actor_input)
    return dict(actor_input)


def _no_login_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, inner in value.items():
            if _LOGIN_KEY.search(str(key)):
                raise ValueError("an actor input never carries login data")
            _no_login_keys(inner)
    elif isinstance(value, list | tuple):
        for inner in value:
            _no_login_keys(inner)


def links(urls: Iterable[str], hosts: frozenset[str]) -> list[str]:
    """Public https links of one network (at most ``MAX_TARGETS``), or ``ValueError``."""
    out: list[str] = []
    for url in urls:
        parts = urlsplit(str(url).strip())
        if parts.scheme != "https" or (parts.hostname or "").lower() not in hosts or parts.username or parts.password:
            raise ValueError("not a public link of this network")
        out.append(parts.geturl())
    if not 1 <= len(out) <= MAX_TARGETS:
        raise ValueError(f"1-{MAX_TARGETS} links per run")
    return out


def terms(values: Iterable[str], *, strip: str = "") -> list[str]:
    """Hashtags or search words (trimmed, at most ``MAX_TARGETS``), or ``ValueError``."""
    out = [" ".join(str(v).split()).lstrip(strip).strip() for v in values]
    out = [v for v in out if v]
    if not 1 <= len(out) <= MAX_TARGETS or any(len(v) > MAX_TERM_CHARS for v in out):
        raise ValueError(f"1-{MAX_TARGETS} search terms of at most {MAX_TERM_CHARS} characters")
    return out


def text(value: Any) -> str | None:
    """A non-empty string of the item, stripped; anything else is None."""
    return value.strip() or None if isinstance(value, str) else None


def when(value: Any) -> datetime | None:
    """An ISO time (``2026-10-01T09:30:00.000Z``) as an aware datetime; anything else is None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def host_url(value: Any, hosts: frozenset[str]) -> str | None:
    """The item's http(s) link when it is on one of ``hosts``, else None (another host is never trusted)."""
    url = text(value)
    if url is None:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in hosts:
        return None
    return url


def is_error_row(item: Mapping[str, Any], text_key: str) -> bool:
    """The actor reporting a target it could not read (closed, removed, private), not an item of its own."""
    reported = any(item.get(k) not in (None, "", False) for k in ("error", "errorCode", "errorDescription"))
    return reported and text(item.get(text_key)) is None


@dataclass(frozen=True, slots=True)
class Author:
    """A post's author as the network gave them: ``profile_link`` key and URL, and the display name if any."""

    profile_key: str
    profile_url: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class SocialPost:
    post: AcquiredPost
    author: Author | None = None


@dataclass(frozen=True, slots=True)
class SocialResult:
    """One call of a source: the run's outcome, the items mapped and kept by ``save`` in this call, the actor's
    error rows, and ``post_unavailable`` when the run gave only error rows."""

    outcome: RunOutcome
    kept: int = 0
    error_rows: int = 0
    error: str | None = None


# One page of mapped items and the run id -> how many the caller kept (an idempotent write: a page can come twice).
Save = Callable[[list[T], str], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class SocialSourceConfig:
    """The settings of one network's two actors (posts, comments); both pinned to ``build``."""

    platform: Platform
    posts_actor: str
    comments_actor: str
    build: str
    max_results: int                  # items of one run (1-50): resultsLimit / resultsPerPage / commentsPerPost
    max_charge_usd: float             # maxTotalChargeUsd of one run (the comments one too, unless set below)
    build_setting: str
    timeout_seconds: float = 300
    comments_max_charge_usd: float | None = None  # the comments actor's own cap; None: max_charge_usd

    def __post_init__(self) -> None:
        if not 1 <= self.max_results <= 50:
            raise ValueError("unsafe apify social limits: max results 1-50")
        self.run_config("posts")  # an unpinned build, a bad actor or an unsafe cap fails here
        self.run_config("comments")

    def run_config(self, purpose: str) -> SocialRunConfig:
        actor = self.posts_actor if purpose == "posts" else self.comments_actor
        cap = self.max_charge_usd
        if purpose != "posts" and self.comments_max_charge_usd is not None:
            cap = self.comments_max_charge_usd
        short = {"instagram": "ig", "tiktok": "tt"}.get(self.platform, self.platform)
        return SocialRunConfig(platform=self.platform, purpose=f"{short}:{purpose}", actor_id=actor,
                               build=self.build, max_charge_usd=cap, max_items=self.max_results,
                               build_setting=self.build_setting, timeout_seconds=self.timeout_seconds,
                               rate_limit_seconds=RATE_LIMIT_SECONDS, rate_limit_pause_seconds=RATE_LIMIT_PAUSE_SECONDS)


class SocialSource(Generic[T]):
    """One actor of one network on :class:`ApifySocialRun`: a subclass builds the input and hands a mapper over."""

    purpose = "posts"
    text_key = "text"

    def __init__(self, store: SourceRunStore, http: ApifyHttp, config: SocialSourceConfig, **run_kw: Any) -> None:
        self.config = config  # run_kw: ApifySocialRun's clock, sleep, poll_seconds (tests)
        self.runner = ApifySocialRun(store, http, config.run_config(self.purpose), **run_kw)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(actor={self.runner.config.actor_id})"

    @property
    def actor_id(self) -> str:
        return self.runner.config.actor_id

    async def _collect(self, campaign_id: str, actor_input: dict[str, Any], save: Save[T],
                       mapper: Callable[[dict[str, Any], str], T | None], *,
                       owner_user_id: int | None) -> SocialResult:
        counts = {"kept": 0, "errors": 0, "mapped": 0}

        async def sink(items: list[dict[str, Any]], run_id: str) -> int:
            mapped: list[T] = []
            for item in items:
                if is_error_row(item, self.text_key):
                    counts["errors"] += 1
                    continue
                try:
                    one = mapper(item, run_id)
                except ValueError:  # pydantic's ValidationError included: an item that does not fit is skipped
                    one = None
                if one is not None:
                    mapped.append(one)
            counts["mapped"] += len(mapped)
            kept = max(0, int(await save(mapped, run_id))) if mapped else 0
            counts["kept"] += kept
            return kept

        outcome = await self.runner.run(campaign_id, actor_input, sink, owner_user_id=owner_user_id)
        unavailable = counts["errors"] > 0 and counts["mapped"] == 0
        return SocialResult(outcome, counts["kept"], counts["errors"], "post_unavailable" if unavailable else None)


def collected_now() -> datetime:
    return datetime.now(UTC)


__all__ = ["LOGIN_KEYS", "MAX_TARGETS", "Author", "SocialPost", "SocialResult", "SocialSource", "SocialSourceConfig",
           "check_input", "host_url", "is_error_row", "links", "terms", "text", "when"]
