"""TikTok through Apify (v3 P1.3, decision D2; ``APIFY_TIKTOK_ENABLED``, off by default).

Public videos and the people under them, without an account: the actors get hashtags, words or public links, never
a cookie or a session (D1; neither actor's input has a cookie field). Nothing here posts, likes, follows or messages.

``clockworks/tiktok-scraper`` ($0.0037 per result + $0.001 per start, FREE tier) -- :class:`ApifyTikTokPosts`.
Input (white list): ``hashtags``, ``searchQueries`` (with ``searchSection`` ``/video``) or ``postURLs``, and
``resultsPerPage``; ``maxFollowersPerProfile`` and ``maxFollowingPerProfile`` are sent as 0, ``commentsPerPost`` as 0
(comments are a paid add-on here; the comments actor reads them). The paid add-ons and downloads
(:data:`PAID_KEYS`: date filter, popularity, followers, video/cover/avatar downloads, subtitles, AI description and
summary) are never sent. One row is one video; fields read: ``webVideoUrl`` (or ``url``), ``text``,
``createTimeISO``, ``authorMeta{name, nickName, profileUrl}``, ``error``/``errorCode``. Price and area are not fields:
they stay in the text for the analysis worker (``CLAUDE.md`` rule 9); ``locationMeta`` is the video's place, not the
object's address.

``clockworks/tiktok-comments-scraper`` ($0.00125 per comment) -- :class:`ApifyTikTokComments`. Input (white list):
``postURLs``, ``commentsPerPost``, ``maxRepliesPerComment`` (0: top-level comments only). One row is one comment;
fields read: ``cid``, ``text``, ``uniqueId`` (the commenter's username), ``videoWebUrl`` (or ``submittedVideoUrl``).
The commenter's profile is ``tiktok.com/@<uniqueId>``. ``authorMeta`` in a comment row is the **video's** author, not
the commenter's, and is never read here; the actor gives no commenter name, so ``author`` stays None.

Field names come from the actors' input and output schemas (Apify Store, 2026-11-11), not from a run of ours: P1.7
checks them on a live sample.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bot.acquisition.apify_social import (
    Author,
    Save,
    SocialPost,
    SocialResult,
    SocialSource,
    SocialSourceConfig,
    check_input,
    collected_now,
    host_url,
    links,
    terms,
    text,
    when,
)
from bot.acquisition.contracts import AcquiredPost, Provenance
from bot.acquisition.people import TT_HOSTS, Engagement, profile_link

if TYPE_CHECKING:
    from bot.acquisition.apify_client import ApifyHttp
    from bot.acquisition.apify_source import SourceRunStore

POSTS_ACTOR = "clockworks/tiktok-scraper"
COMMENTS_ACTOR = "clockworks/tiktok-comments-scraper"
POSTS_INPUT_KEYS = frozenset({"hashtags", "searchQueries", "searchSection", "postURLs", "resultsPerPage",
                              "maxFollowersPerProfile", "maxFollowingPerProfile", "commentsPerPost"})
COMMENTS_INPUT_KEYS = frozenset({"postURLs", "commentsPerPost", "maxRepliesPerComment"})
# Paid add-ons ($) and downloads of clockworks/tiktok-scraper: never part of an input (kept here for the tests).
PAID_KEYS = frozenset({
    "oldestPostDateUnified", "newestPostDate",                   # date filter
    "mostDiggs", "leastDiggs",                                   # popularity filter
    "videoSearchSorting", "videoSearchDateFilter",
    "shouldDownloadVideos", "shouldDownloadCovers", "shouldDownloadSlideshowImages", "shouldDownloadAvatars",
    "shouldDownloadMusicCovers", "videoKvStoreIdOrName", "downloadSubtitlesOptions",
    "aiVideoDescription", "aiVideoSummary",
})
ZERO_KEYS = ("maxFollowersPerProfile", "maxFollowingPerProfile", "commentsPerPost")  # sent, always 0
VIDEO_SECTION = "/video"
MAX_REPLIES_PER_COMMENT = 0


def tiktok_profile(unique_id: Any) -> tuple[str, str] | None:
    """``(tt:<uniqueId>, https://www.tiktok.com/@<uniqueId>)`` of a username as the actor wrote it, or None."""
    name = text(unique_id)
    if name is None or "/" in name:
        return None
    return profile_link("tiktok", f"https://www.tiktok.com/@{name.removeprefix('@')}")


def post_input(*, hashtags: Sequence[str] = (), search_queries: Sequence[str] = (), post_urls: Sequence[str] = (),
               max_results: int) -> dict[str, Any]:
    """The posts actor's input: one kind of target (hashtags, search words or links) and ``resultsPerPage``."""
    given = [kind for kind, values in (("hashtags", hashtags), ("searchQueries", search_queries),
                                       ("postURLs", post_urls)) if values]
    if len(given) != 1:
        raise ValueError("a TikTok posts run needs exactly one of hashtags, search_queries, post_urls")
    body: dict[str, Any]
    if hashtags:
        body = {"hashtags": terms(hashtags, strip="#")}
    elif search_queries:
        body = {"searchQueries": terms(search_queries), "searchSection": VIDEO_SECTION}
    else:
        body = {"postURLs": links(post_urls, TT_HOSTS)}
    body["resultsPerPage"] = max_results
    body.update(dict.fromkeys(ZERO_KEYS, 0))
    return check_input(body, POSTS_INPUT_KEYS)


def comments_input(post_urls: Sequence[str], *, max_results: int) -> dict[str, Any]:
    return check_input({"postURLs": links(post_urls, TT_HOSTS), "commentsPerPost": max_results,
                        "maxRepliesPerComment": MAX_REPLIES_PER_COMMENT}, COMMENTS_INPUT_KEYS)


def _author(meta: Any) -> Author | None:
    """The video's author: ``authorMeta.profileUrl`` when it is a TikTok profile, else from ``authorMeta.name``;
    a ``profileUrl`` on another host (or not a profile) is not trusted, and then there is no author."""
    if not isinstance(meta, dict):
        return None
    url = text(meta.get("profileUrl"))
    profile = profile_link("tiktok", url) if url is not None else tiktok_profile(meta.get("name"))
    return Author(*profile, name=text(meta.get("nickName"))) if profile else None


def tiktok_post(item: dict[str, Any], *, campaign_id: str, actor_id: str, run_id: str,
                collected_at: Any = None) -> SocialPost | None:
    """One posts-actor row as a post (and its author), or None: no text, or a link off TikTok."""
    body = text(item.get("text"))
    url = host_url(item.get("webVideoUrl"), TT_HOSTS) or host_url(item.get("url"), TT_HOSTS)
    if body is None or url is None:
        return None
    author = _author(item.get("authorMeta"))
    post = AcquiredPost(
        campaign_id=campaign_id,
        source=Provenance(provider="apify", platform="tiktok", actor_id=actor_id, run_id=run_id,
                          group_url=author.profile_url if author else url, post_url=url,
                          published_at=when(item.get("createTimeISO")), collected_at=collected_at or collected_now()),
        raw_text=body[:50_000])  # provider_facts stay empty: price and area are only in the text
    return SocialPost(post, author)


def tiktok_comment(item: dict[str, Any]) -> Engagement | None:
    """One comment-actor row as a comment, or None: no text, no usable ``uniqueId``, or a video off TikTok.
    ``authorMeta`` (the video's author) is never taken for the commenter."""
    profile = tiktok_profile(item.get("uniqueId"))
    body = text(item.get("text"))
    post_url = host_url(item.get("videoWebUrl"), TT_HOSTS) or host_url(item.get("submittedVideoUrl"), TT_HOSTS)
    if profile is None or body is None or post_url is None:
        return None
    raw_id = item.get("cid")
    return Engagement(platform="tiktok", kind="comment", author=None, profile_key=profile[0],
                      profile_url=profile[1], text=body[:5000], post_url=post_url,
                      raw_id=str(raw_id)[:200] if isinstance(raw_id, str | int) and str(raw_id) else None)


class ApifyTikTokPosts(SocialSource[SocialPost]):
    purpose = "posts"

    async def run(self, campaign_id: str, *, save: Save[SocialPost], hashtags: Sequence[str] = (),
                  search_queries: Sequence[str] = (), post_urls: Sequence[str] = (),
                  owner_user_id: int | None = None) -> SocialResult:
        """Videos for ``hashtags``, ``search_queries`` or ``post_urls``; each page of posts goes to ``save``."""
        actor_input = post_input(hashtags=hashtags, search_queries=search_queries, post_urls=post_urls,
                                 max_results=self.config.max_results)

        def mapper(item: dict[str, Any], run_id: str) -> SocialPost | None:
            return tiktok_post(item, campaign_id=campaign_id, actor_id=self.actor_id, run_id=run_id)

        return await self._collect(campaign_id, actor_input, save, mapper, owner_user_id=owner_user_id)


class ApifyTikTokComments(SocialSource[Engagement]):
    purpose = "comments"

    async def run(self, campaign_id: str, post_urls: Sequence[str], *, save: Save[Engagement],
                  owner_user_id: int | None = None) -> SocialResult:
        """The comments under ``post_urls``; each page of comments goes to ``save``."""
        actor_input = comments_input(post_urls, max_results=self.config.max_results)
        return await self._collect(campaign_id, actor_input, save, lambda item, _run: tiktok_comment(item),
                                   owner_user_id=owner_user_id)


@dataclass(frozen=True, slots=True)
class TikTokSources:
    posts: ApifyTikTokPosts
    comments: ApifyTikTokComments


def tiktok_sources(config: SocialSourceConfig, store: SourceRunStore, http: ApifyHttp, **kw: Any) -> TikTokSources:
    if config.platform != "tiktok":
        raise ValueError("not a TikTok config")
    return TikTokSources(ApifyTikTokPosts(store, http, config, **kw), ApifyTikTokComments(store, http, config, **kw))


__all__ = ["COMMENTS_ACTOR", "COMMENTS_INPUT_KEYS", "PAID_KEYS", "POSTS_ACTOR", "POSTS_INPUT_KEYS",
           "ApifyTikTokComments", "ApifyTikTokPosts", "TikTokSources", "comments_input", "post_input",
           "tiktok_comment", "tiktok_post", "tiktok_profile", "tiktok_sources"]
