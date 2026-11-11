"""Instagram through Apify (v3 P1.2, decision D2; ``APIFY_INSTAGRAM_ENABLED``, off by default).

Public posts and the people under them, without an account: the actors get hashtags, words or public links, never
a cookie or a session (D1). Nothing here posts, likes, follows or messages anybody.

``apify/instagram-scraper`` ($0.0027 per result, FREE tier) -- :class:`ApifyInstagramPosts`. Input (white list):
``search`` (a hashtag or words from the plan) or ``directUrls`` (public links), and ``resultsLimit``. One dataset row
is one post; fields read: ``url``, ``caption`` (the text), ``timestamp``, ``ownerUsername``, ``ownerFullName``.
Price and area are not fields of the actor: they stay in the caption, for the analysis worker to read with quotes
(``CLAUDE.md`` rule 9). ``locationName`` is the post's geotag, not the object's address, so it is not a fact here.

``apify/instagram-comment-scraper`` ($0.0026 per comment) -- :class:`ApifyInstagramComments`. Input (white list):
``directUrls`` (the posts), ``resultsLimit``, ``includeNestedComments`` (false: top-level comments only). One row is
one comment; fields read: ``id``, ``text``, ``ownerUsername`` (or ``owner.username``), ``postUrl`` (or
``inputUrl``), ``error``/``errorDescription``. The commenter's profile is ``instagram.com/<ownerUsername>``; the
actor gives no name (``full_name`` is null in its schema), so ``author`` stays None.

Field names come from the actors' output schemas (Apify Store, 2026-11-11), not from a run of ours: P1.7 checks them
on a live sample.
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
from bot.acquisition.people import IG_HOSTS, Engagement, profile_link

if TYPE_CHECKING:
    from bot.acquisition.apify_client import ApifyHttp
    from bot.acquisition.apify_source import SourceRunStore

POSTS_ACTOR = "apify/instagram-scraper"
COMMENTS_ACTOR = "apify/instagram-comment-scraper"
POSTS_INPUT_KEYS = frozenset({"search", "directUrls", "resultsLimit"})
COMMENTS_INPUT_KEYS = frozenset({"directUrls", "resultsLimit", "includeNestedComments"})
INCLUDE_NESTED_COMMENTS = False  # replies would share resultsLimit with the comments they answer


def instagram_profile(username: Any) -> tuple[str, str] | None:
    """``(ig:<username>, https://www.instagram.com/<username>)`` of a username as the actor wrote it, or None."""
    name = text(username)
    return profile_link("instagram", f"https://www.instagram.com/{name}") if name and "/" not in name else None


def post_input(*, search: str | None = None, direct_urls: Sequence[str] = (), max_results: int) -> dict[str, Any]:
    """The posts actor's input: ``search`` or ``directUrls`` (exactly one), and ``resultsLimit``."""
    if (search is None) == (not direct_urls):
        raise ValueError("an Instagram posts run needs search or direct_urls, not both")
    body: dict[str, Any] = ({"search": terms([search])[0]} if search is not None
                            else {"directUrls": links(direct_urls, IG_HOSTS)})
    body["resultsLimit"] = max_results
    return check_input(body, POSTS_INPUT_KEYS)


def comments_input(post_urls: Sequence[str], *, max_results: int) -> dict[str, Any]:
    return check_input({"directUrls": links(post_urls, IG_HOSTS), "resultsLimit": max_results,
                        "includeNestedComments": INCLUDE_NESTED_COMMENTS}, COMMENTS_INPUT_KEYS)


def instagram_post(item: dict[str, Any], *, campaign_id: str, actor_id: str, run_id: str,
                   collected_at: Any = None) -> SocialPost | None:
    """One posts-actor row as a post (and its author), or None: no caption, or a link off Instagram."""
    caption, url = text(item.get("caption")), host_url(item.get("url"), IG_HOSTS)
    if caption is None or url is None:
        return None
    profile = instagram_profile(item.get("ownerUsername"))
    author = Author(*profile, name=text(item.get("ownerFullName"))) if profile else None
    post = AcquiredPost(
        campaign_id=campaign_id,
        source=Provenance(provider="apify", platform="instagram", actor_id=actor_id, run_id=run_id,
                          group_url=author.profile_url if author else url, post_url=url,
                          published_at=when(item.get("timestamp")), collected_at=collected_at or collected_now()),
        raw_text=caption[:50_000])  # provider_facts stay empty: price and area are only in the caption
    return SocialPost(post, author)


def instagram_comment(item: dict[str, Any]) -> Engagement | None:
    """One comment-actor row as a comment, or None: no text, no usable username, or a post off Instagram."""
    owner = item.get("owner") if isinstance(item.get("owner"), dict) else {}
    profile = instagram_profile(item.get("ownerUsername") or owner.get("username"))
    body = text(item.get("text"))
    post_url = host_url(item.get("postUrl"), IG_HOSTS) or host_url(item.get("inputUrl"), IG_HOSTS)
    if profile is None or body is None or post_url is None:
        return None
    raw_id = item.get("id")
    return Engagement(platform="instagram", kind="comment", author=None, profile_key=profile[0],
                      profile_url=profile[1], text=body[:5000], post_url=post_url,
                      raw_id=str(raw_id)[:200] if isinstance(raw_id, str | int) and str(raw_id) else None)


class ApifyInstagramPosts(SocialSource[SocialPost]):
    purpose = "posts"
    text_key = "caption"

    async def run(self, campaign_id: str, *, save: Save[SocialPost], search: str | None = None,
                  direct_urls: Sequence[str] = (), owner_user_id: int | None = None) -> SocialResult:
        """Posts for ``search`` (a hashtag or words) or ``direct_urls``; each page of posts goes to ``save``."""
        actor_input = post_input(search=search, direct_urls=direct_urls, max_results=self.config.max_results)

        def mapper(item: dict[str, Any], run_id: str) -> SocialPost | None:
            return instagram_post(item, campaign_id=campaign_id, actor_id=self.actor_id, run_id=run_id)

        return await self._collect(campaign_id, actor_input, save, mapper, owner_user_id=owner_user_id)


class ApifyInstagramComments(SocialSource[Engagement]):
    purpose = "comments"

    async def run(self, campaign_id: str, post_urls: Sequence[str], *, save: Save[Engagement],
                  owner_user_id: int | None = None) -> SocialResult:
        """The comments under ``post_urls``; each page of comments goes to ``save``."""
        actor_input = comments_input(post_urls, max_results=self.config.max_results)
        return await self._collect(campaign_id, actor_input, save, lambda item, _run: instagram_comment(item),
                                   owner_user_id=owner_user_id)


@dataclass(frozen=True, slots=True)
class InstagramSources:
    posts: ApifyInstagramPosts
    comments: ApifyInstagramComments


def instagram_sources(config: SocialSourceConfig, store: SourceRunStore, http: ApifyHttp, **kw: Any) -> InstagramSources:
    if config.platform != "instagram":
        raise ValueError("not an Instagram config")
    return InstagramSources(ApifyInstagramPosts(store, http, config, **kw),
                            ApifyInstagramComments(store, http, config, **kw))


__all__ = ["COMMENTS_ACTOR", "COMMENTS_INPUT_KEYS", "POSTS_ACTOR", "POSTS_INPUT_KEYS", "ApifyInstagramComments",
           "ApifyInstagramPosts", "InstagramSources", "comments_input", "instagram_comment", "instagram_post",
           "instagram_profile", "instagram_sources", "post_input"]
