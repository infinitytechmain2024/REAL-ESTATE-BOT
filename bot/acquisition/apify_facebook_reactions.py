"""People who reacted under public Facebook posts, through ``apify/facebook-likes-scraper`` (v3 P1.6, D5).

Only the contract and the mapper: nothing calls it yet, and nothing here stores a person (the table and the signal
strength are P4.1). The paid run is :class:`bot.acquisition.apify_source.ApifySocialRun` (claim, ``pending:``
placeholder, one launch with ``maxTotalChargeUsd`` and a pinned build, billing, paged import).

Posts are sent in one canonical form (:func:`normalize_post_url`): the same post written as ``m.facebook.com/…?__tn__``
or ``www.facebook.com/…/`` is one input hash, so it is never paid for twice.

Whether the actor counts ``resultsLimit`` per post or per run is not known (no live sample yet), so it gets
``max(1, max_results // posts)`` and ``maxItems`` stays ``max_results``: per post the total cannot pass
``max_results``; per run the sample is only smaller. A post with exactly the limit of rows may be cut short: it is
counted in :attr:`ReactionPage.truncated` and logged.

The actor (``docs/v3/ACTORS.md``, FB): input ``startUrls`` (required) and ``resultsLimit``; a row has ``name``,
``profileUrl``, ``facebookId``, ``facebookUrl``, ``reaction``, ``url``, ``inputUrl``, ``error``,
``errorDescription``; $0.0062 per result. Public posts only: a closed group is read only after a person joined it
(``CLAUDE.md`` rule 10). The input is a white list (:func:`actor_input`): no cookie, session or account value can
reach the actor.

Mapping a row (:func:`map_rows`), nothing guessed:

* a row with ``error`` is the post's failure (:class:`PostError`), never a reaction (``0``, ``false``, empty: none);
* the profile is ``profileUrl`` only, through ``profile_link("facebook", …)``: missing -> ``no_profile``, a link
  without ``http(s)://`` -> ``no_scheme``, another host -> ``foreign_host``, a Facebook link that is not a person's
  or a page's -> ``not_a_profile``;
* the post is ``inputUrl`` (what was sent), else ``url``, in its :func:`normalize_post_url` form; neither ->
  ``no_post``;
* ``reaction`` becomes one of :data:`REACTION_TYPES`; a word outside it becomes ``other`` (``Engagement.reaction_type``
  is a free string, so ``other`` is valid; the raw word is not kept in the engagement but counted in
  :attr:`ReactionPage.unknown_types`, to extend the list after a live sample); no word -> ``None``;
* ``name`` is the author as given, ``facebookId`` the raw id; the profile is never built from ``facebookId``;
* the same person twice under the same post in one page -> ``duplicate``.

Nothing calls :meth:`ApifyFacebookReactions.collect` yet; the truncation count is kept per call, so a resumed import
(after a crash) counts only the pages it imports itself.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

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
# Hosts a post link may come with; it is always sent as www.facebook.com.
POST_HOSTS = FB_HOSTS | {"mbasic.facebook.com"}
# Query parameters that carry the post's id (``_POST_ID`` in bot/acquisition/contracts.py, plus the owner ``id`` of
# ``permalink.php?story_fbid=…&id=…``); every other one (``__tn__``, ``ref``, ``mibextid``…) is dropped.
POST_ID_PARAMS = frozenset({"story_fbid", "id", "fbid", "multi_permalinks"})
# The only keys the actor ever gets: nothing else (cookies, sessions, proxies with accounts) can be passed.
INPUT_KEYS = frozenset({"startUrls", "resultsLimit"})


def min_charge_usd(max_results: int) -> float:
    """The least cap that pays for ``max_results`` results (rounded: 500 x 0.0062 is 3.10, not 3.1000000000000005)."""
    return round(max_results * USD_PER_RESULT, 6)


@dataclass(frozen=True, slots=True)
class ReactionsConfig:
    actor_id: str
    build: str                        # pinned build (number or tag, never «latest»)
    max_results: int                  # results of one run (maxItems; resultsLimit is its share per post)
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
    rows_by_post: Counter[str] = field(default_factory=Counter)   # post -> actor rows (not errors) on this page
    truncated: Counter[str] = field(default_factory=Counter)      # post -> rows, set when a post reached the limit


def normalize_post_url(url: object) -> str:
    """``https://www.facebook.com/<path>`` with only the post-id parameters (sorted), no fragment, no trailing ``/``.
    Not an http(s) Facebook link -> ValueError."""
    value = url.strip() if isinstance(url, str) else ""
    parts = urlsplit(value)
    path = parts.path.rstrip("/")
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in POST_HOSTS or not path:
        raise ValueError("a reactions post must be a facebook.com link")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if k in POST_ID_PARAMS))
    return f"https://www.facebook.com{path}" + (f"?{query}" if query else "")


def post_urls(urls: Iterable[str]) -> list[str]:
    """The posts to read in their canonical form, deduplicated and sorted (a stable input hash)."""
    out = {normalize_post_url(url) for url in urls}
    if not 1 <= len(out) <= MAX_POSTS_PER_RUN:
        raise ValueError(f"1-{MAX_POSTS_PER_RUN} posts per reactions run")
    return sorted(out)


def per_post_limit(max_results: int, posts: int) -> int:
    """``resultsLimit``: safe whether the actor counts it per post or per run (``maxItems`` stays ``max_results``)."""
    return max(1, int(max_results) // max(1, posts))


def actor_input(urls: Iterable[str], *, max_results: int) -> dict[str, Any]:
    """The whole actor input: ``startUrls`` and ``resultsLimit``, nothing else."""
    posts = post_urls(urls)
    return {"startUrls": [{"url": url} for url in posts], "resultsLimit": per_post_limit(max_results, len(posts))}


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


def _post(value: object) -> str | None:
    """The post a row is about, canonical when it is a Facebook link, as given when it is another http(s) one."""
    text = _text(value, 2048)
    if text is None:
        return None
    try:
        return normalize_post_url(text)
    except ValueError:
        parts = urlsplit(text)
        return text if parts.scheme in ("http", "https") and parts.hostname else None


_NO_ERROR = frozenset({"", "0", "false", "none", "null"})


def _error(value: object) -> str | None:
    """The row's error, or None: ``None``, ``False``, ``0`` and empty / «false» / «0» strings are no error."""
    if value is None or value is False:
        return None
    if value is True:
        return "error"
    if isinstance(value, int | float):
        return None if value == 0 else str(value)
    text = _text(value, 200)
    return None if text is None or text.lower() in _NO_ERROR else text


def _drop_reason(href: object) -> str:
    if not isinstance(href, str) or not href.strip():
        return "no_profile"
    parts = urlsplit(href.strip())
    if parts.scheme not in ("http", "https"):
        return "no_scheme"
    return "not_a_profile" if (parts.hostname or "").lower() in FB_HOSTS else "foreign_host"


def map_rows(rows: Iterable[Any]) -> ReactionPage:
    """Actor rows -> reactions, post errors and dropped rows with their reason."""
    page, seen = ReactionPage(), set()
    for row in rows:
        if not isinstance(row, dict):
            page.skipped["not_a_row"] += 1
            continue
        post = _post(row.get("inputUrl")) or _post(row.get("url"))
        error = _error(row.get("error"))
        if error is not None:
            page.errors.append(PostError(post, error, _text(row.get("errorDescription"), 500)))
            continue
        if post is not None:
            page.rows_by_post[post] += 1
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
        limit, rows = int(body["resultsLimit"]), Counter[str]()

        async def mapped(items: list[dict[str, Any]], run_id: str) -> int:
            page = map_rows(items)
            for post, count in page.rows_by_post.items():
                before = rows[post]
                rows[post] += count
                if before < limit <= rows[post]:  # the post reached the limit: more reactions may exist
                    page.truncated[post] = rows[post]
            if page.truncated:
                log.warning("apify_reactions.truncated", extra={
                    "campaign_id": campaign_id, "run_id": run_id, "posts": len(page.truncated),
                    "results_limit": limit})
            if page.skipped or page.errors or page.unknown_types:
                log.info("apify_reactions.page", extra={
                    "campaign_id": campaign_id, "run_id": run_id, "reactions": len(page.engagements),
                    "post_errors": len(page.errors), "skipped": dict(page.skipped),
                    "unknown_types": sorted(page.unknown_types)})
            return await sink(page, run_id)

        return await self._run.run(campaign_id, body, mapped, owner_user_id=owner_user_id)


__all__ = ["ACTOR", "INPUT_KEYS", "OTHER", "POST_HOSTS", "POST_ID_PARAMS", "REACTION_TYPES", "USD_PER_RESULT", "ApifyFacebookReactions", "PostError",
           "ReactionPage", "ReactionSink", "ReactionsConfig", "actor_input", "map_rows", "min_charge_usd", "normalize_post_url", "per_post_limit", "post_urls",
           "reaction_type"]
