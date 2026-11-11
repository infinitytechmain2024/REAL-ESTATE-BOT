"""People who reacted under public Facebook posts, through ``apify/facebook-likes-scraper`` (v3 P1.6, D5).

Only the contract and the mapper: nothing calls it yet, and nothing here stores a person (the table and the signal
strength are P4.1). The paid run is :class:`bot.acquisition.apify_source.ApifySocialRun` (claim, ``pending:``
placeholder, one launch with ``maxTotalChargeUsd`` and a pinned build, billing, paged import).

The actor (``docs/v3/ACTORS.md``, FB): input ``startUrls`` (required) and ``resultsLimit``; a row has ``name``,
``profileUrl``, ``facebookId``, ``facebookUrl``, ``reaction``, ``url``, ``inputUrl``, ``error``,
``errorDescription``; $0.0062 per result. Public posts only: a closed group is read only after a person joined it
(``CLAUDE.md`` rule 10). The input is a white list (:func:`actor_input`): no cookie, session or account value can
reach the actor.

Mapping a row (:func:`map_rows`), nothing guessed:

* a row with ``error`` is the post's failure (:class:`PostError`), never a reaction;
* the profile is ``profileUrl`` only, through ``profile_link("facebook", …)``: missing -> ``no_profile``, another
  host -> ``foreign_host``, a Facebook link that is not a person's or a page's -> ``not_a_profile``;
* the post is ``inputUrl`` (what was sent), else ``url``; neither -> ``no_post``;
* ``reaction`` becomes one of :data:`REACTION_TYPES`; a word outside it becomes ``other`` (``Engagement.reaction_type``
  is a free string, so ``other`` is valid; the raw word is not kept in the engagement but counted in
  :attr:`ReactionPage.unknown_types`, to extend the list after a live sample); no word -> ``None``;
* ``name`` is the author as given, ``facebookId`` the raw id; the profile is never built from ``facebookId``;
* the same person twice under the same post in one page -> ``duplicate``.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from bot.acquisition.apify_client import ApifyHttp
from bot.acquisition.apify_source import ApifySocialRun, RunOutcome, SocialRunConfig, SourceRunStore
from bot.acquisition.people import FB_HOSTS, Engagement, profile_link

log = logging.getLogger(__name__)

ACTOR = "apify/facebook-likes-scraper"
USD_PER_RESULT = 0.0062               # the actor's price per result (FREE tier), docs/v3/ACTORS.md
PURPOSE = "reactions"
BUILD_SETTING = "APIFY_FACEBOOK_REACTIONS_ACTOR_BUILD"
MAX_POSTS_PER_RUN = 20
REACTION_TYPES: tuple[str, ...] = ("like", "love", "care", "haha", "wow", "sad", "angry")
OTHER = "other"
# The actor's own words for the same reactions (ACTORS.md: «haha/laugh»).
_ALIASES = {"laugh": "haha"}
# The only keys the actor ever gets: nothing else (cookies, sessions, proxies with accounts) can be passed.
INPUT_KEYS = frozenset({"startUrls", "resultsLimit"})


def min_charge_usd(max_results: int) -> float:
    """The least cap that pays for ``max_results`` results (rounded: 500 x 0.0062 is 3.10, not 3.1000000000000005)."""
    return round(max_results * USD_PER_RESULT, 6)


@dataclass(frozen=True, slots=True)
class ReactionsConfig:
    actor_id: str
    build: str                        # pinned build (number or tag, never «latest»)
    max_results: int                  # results of one run (resultsLimit and maxItems)
    max_charge_usd: float             # maxTotalChargeUsd of one run
    timeout_seconds: float = 300
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if not 1 <= self.max_results <= 500:
            raise ValueError("max_results must be within 1-500")
        if self.max_charge_usd < min_charge_usd(self.max_results):
            raise ValueError("max_charge_usd is below max_results x the price of one result")
        self.social()  # an unpinned build, a bad actor id or unsafe limits fail here

    def social(self) -> SocialRunConfig:
        return SocialRunConfig(platform="facebook", purpose=PURPOSE, actor_id=self.actor_id, build=self.build.strip(),
                               max_charge_usd=self.max_charge_usd, max_items=self.max_results,
                               build_setting=BUILD_SETTING, timeout_seconds=self.timeout_seconds,
                               max_attempts=self.max_attempts)


@dataclass(frozen=True, slots=True)
class PostError:
    """The actor could not read a post (``error`` row): the post failed, nobody reacted."""

    post_url: str | None
    error: str
    description: str | None = None


@dataclass(slots=True)
class ReactionPage:
    """One dataset page, mapped."""

    engagements: list[Engagement] = field(default_factory=list)
    errors: list[PostError] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)        # reason -> rows dropped
    unknown_types: Counter[str] = field(default_factory=Counter)  # raw reaction word -> rows mapped to «other»


def post_urls(urls: Iterable[str]) -> list[str]:
    """The posts to read: public Facebook http(s) links, deduplicated and sorted (a stable input hash)."""
    out: set[str] = set()
    for url in urls:
        value = url.strip() if isinstance(url, str) else ""
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in FB_HOSTS:
            raise ValueError("a reactions post must be a facebook.com link")
        out.add(value)
    if not 1 <= len(out) <= MAX_POSTS_PER_RUN:
        raise ValueError(f"1-{MAX_POSTS_PER_RUN} posts per reactions run")
    return sorted(out)


def actor_input(urls: Iterable[str], *, max_results: int) -> dict[str, Any]:
    """The whole actor input: ``startUrls`` and ``resultsLimit``, nothing else."""
    return {"startUrls": [{"url": url} for url in post_urls(urls)], "resultsLimit": int(max_results)}


def reaction_type(value: object) -> tuple[str | None, str | None]:
    """``(type, unknown raw word)``: a known reaction, ``other`` with the raw word, or ``None`` when none was given."""
    if not isinstance(value, str) or not value.strip():
        return None, None
    word = value.strip().lower()
    word = _ALIASES.get(word, word)
    if word in REACTION_TYPES:
        return word, None
    return OTHER, value.strip()[:40]


def _text(value: object, limit: int) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] or None


def _http(value: object) -> str | None:
    text = _text(value, 2048)
    if text is None:
        return None
    parts = urlsplit(text)
    return text if parts.scheme in ("http", "https") and parts.hostname else None


def _drop_reason(href: object) -> str:
    if not isinstance(href, str) or not href.strip():
        return "no_profile"
    host = (urlsplit(href.strip()).hostname or "").lower()
    return "not_a_profile" if host in FB_HOSTS else "foreign_host"


def map_rows(rows: Iterable[Any]) -> ReactionPage:
    """Actor rows -> reactions, post errors and dropped rows with their reason."""
    page, seen = ReactionPage(), set()
    for row in rows:
        if not isinstance(row, dict):
            page.skipped["not_a_row"] += 1
            continue
        post = _http(row.get("inputUrl")) or _http(row.get("url"))
        error = _text(row.get("error"), 200)
        if error is not None or row.get("error") is True:
            page.errors.append(PostError(post, error or "error", _text(row.get("errorDescription"), 500)))
            continue
        href = row.get("profileUrl")
        link = profile_link("facebook", href)
        if link is None:
            page.skipped[_drop_reason(href)] += 1
            continue
        if post is None:
            page.skipped["no_post"] += 1
            continue
        if (post, link[0]) in seen:
            page.skipped["duplicate"] += 1
            continue
        kind, unknown = reaction_type(row.get("reaction"))
        try:
            engagement = Engagement(platform="facebook", kind="reaction", author=_text(row.get("name"), 200),
                                    profile_key=link[0], profile_url=link[1], reaction_type=kind, post_url=post,
                                    raw_id=_text(row.get("facebookId"), 200))
        except ValidationError:
            page.skipped["invalid"] += 1
            continue
        seen.add((post, link[0]))
        if unknown is not None:
            page.unknown_types[unknown] += 1
        page.engagements.append(engagement)
    return page


# The caller's sink gets one mapped page and the run id and answers how many reactions it kept (idempotent: a page
# can come twice after a crash, see ApifySocialRun).
ReactionSink = Callable[[ReactionPage, str], Awaitable[int]]


class ApifyFacebookReactions:
    """One paid run per set of posts and campaign; its rows are mapped and handed to the caller's sink."""

    def __init__(self, store: SourceRunStore, http: ApifyHttp, config: ReactionsConfig, *,
                 poll_seconds: int = 20) -> None:
        self.config = config
        self._run = ApifySocialRun(store, http, config.social(), poll_seconds=poll_seconds)

    def __repr__(self) -> str:
        return f"ApifyFacebookReactions(actor={self.config.actor_id})"

    def actor_input(self, urls: Iterable[str]) -> dict[str, Any]:
        return actor_input(urls, max_results=self.config.max_results)

    async def collect(self, campaign_id: str, urls: Iterable[str], sink: ReactionSink, *,
                      owner_user_id: int | None = None) -> RunOutcome:
        body = self.actor_input(urls)

        async def mapped(items: list[dict[str, Any]], run_id: str) -> int:
            page = map_rows(items)
            if page.skipped or page.errors or page.unknown_types:
                log.info("apify_reactions.page", extra={
                    "campaign_id": campaign_id, "run_id": run_id, "reactions": len(page.engagements),
                    "post_errors": len(page.errors), "skipped": dict(page.skipped),
                    "unknown_types": sorted(page.unknown_types)})
            return await sink(page, run_id)

        return await self._run.run(campaign_id, body, mapped, owner_user_id=owner_user_id)


__all__ = ["ACTOR", "INPUT_KEYS", "OTHER", "REACTION_TYPES", "USD_PER_RESULT", "ApifyFacebookReactions", "PostError",
           "ReactionPage", "ReactionSink", "ReactionsConfig", "actor_input", "map_rows", "min_charge_usd", "post_urls", "reaction_type"]
