"""Bridges Facebook group reading to the research pipeline's SearchHit shape.

The source is wired into the pipeline when ``FACEBOOK_ENABLED`` is true. It
discovers public groups in Facebook itself when no explicit group list is
configured, then reads their matching posts under the shared session lock.
"""

from __future__ import annotations

import time

from bot.config import FacebookSettings
from bot.logging_conf import get_logger
from bot.models.query import ParsedQuery
from bot.models.result import SearchHit
from bot.services.facebook.activity import age_days
from bot.services.facebook.browser import FacebookSession, SessionState
from bot.services.facebook.groups import (
    GroupAccess,
    GroupPost,
    check_access,
    discover_groups,
    join_group,
    read_recent_posts,
    search_posts,
)
from bot.services.facebook.query import group_query, location_matches, post_terms
from bot.services.facebook.store import FacebookGroupStore
from bot.services.pipeline import SourceGroup, SourceSearchResult
from bot.utils.places import place_tokens

log = get_logger(__name__)

NO_LOCATION_NOTE = (
    "Facebook: в запросе не указано место, поэтому поиск групп пропущен — "
    "без него Facebook возвращает случайные группы со всего мира."
)


class FacebookSource:
    """Reads configured groups for one parsed query and returns pipeline-ready hits.

    Mirrors :class:`bot.services.search.client.SearXNGClient` closely enough
    that :class:`~bot.services.pipeline.ResearchPipeline` can eventually treat
    this as a second, optional hit source: ``search()`` returns a
    :class:`SourceSearchResult` containing :class:`SearchHit` objects with
    ``content`` already filled in. Completed reads use the existing pipeline
    stages; aborted reads remain display-only (see
    ``pipeline._collect_content``, which already skips fetching any hit that
    arrives with ``content`` set).
    """

    def __init__(
        self, settings: FacebookSettings, session: FacebookSession,
        store: FacebookGroupStore | None = None,
    ) -> None:
        self.settings = settings
        self.session = session
        self.store = store

    async def search(self, parsed: ParsedQuery) -> SourceSearchResult:
        if not self.settings.enabled:
            return SourceSearchResult()

        # Use the target location's language, not the language of the user.
        # A Ukrainian request about Madrid must search Spanish Facebook terms.
        terms = post_terms(parsed, limit=self.settings.max_search_terms)
        if not terms:
            fallback = parsed.human_summary() or " ".join(parsed.keywords)
            terms = [fallback] if fallback else []
        if not terms:
            return SourceSearchResult()
        query_text = group_query(parsed)

        # Facebook's group search always answers. Without a place the query is
        # a bare "land property", and what comes back is the property groups of
        # the whole world -- which is how a request for plots outside Madrid
        # was once answered with two Bulgarian groups and one from Malaysia.
        # An explicitly configured group list is still read: that is a choice.
        place = place_tokens(parsed.location)
        if not place and not self.settings.group_urls:
            log.info("facebook.source.no_location", query=parsed.summary())
            return SourceSearchResult(notes=[NO_LOCATION_NOTE])

        hits: list[SearchHit] = []
        groups: list[SourceGroup] = []
        if not self.session.has_live_context:
            await self.session.start()
        async with self.session.lock:
            try:
                state = await self.session.probe_state()
                if state != SessionState.HEALTHY:
                    log.warning("facebook.source.session_not_healthy", state=state.value)
                    return SourceSearchResult(failed=True)

                configured = [(url, f"Группа Facebook {url.rstrip('/').rsplit('/', 1)[-1]}")
                              for url in self.settings.group_urls]
                discovered = not configured
                if discovered:
                    configured = await discover_groups(
                        self.session.page,
                        query_text,
                        max_groups=self.settings.max_discovered_groups,
                    )
                if discovered:
                    configured = [
                        (url, title) for url, title in configured if location_matches(title, parsed)
                    ]
                for index, (group_url, title) in enumerate(configured):
                    group = SourceGroup(url=group_url, title=title)
                    groups.append(group)
                    if index and await self.session.observe_state() != SessionState.HEALTHY:
                        return SourceSearchResult(hits=hits, failed=True, groups=groups)
                    access = await check_access(self.session.page, group_url)
                    group.access = access.value
                    if access != GroupAccess.ACCESSIBLE:
                        if access is GroupAccess.MEMBERSHIP_REQUIRED and self.settings.auto_join_groups:
                            requested = await join_group(self.session.page)
                            if requested and self.store is not None:
                                self.store.record(
                                    url=group_url,
                                    title=title,
                                    last_post_text=None,
                                    access_state=access.value,
                                    membership_state="join_requested",
                                    active=False,
                                )
                            log.info(
                                "facebook.source.group.join_requested",
                                group_url=group_url,
                                requested=requested,
                            )
                        log.info(
                            "facebook.source.group_skipped", group_url=group_url, access=access.value
                        )
                        if access in (GroupAccess.LOGIN_REQUIRED, GroupAccess.UNKNOWN_ERROR):
                            return SourceSearchResult(hits=hits, failed=True, groups=groups)
                        # Known group restrictions are not a broken source; keep
                        # reading and allow completed hits from other groups to save.
                        continue

                    # Search the group in each language first: those posts
                    # were matched against the request, while the feed is only
                    # whatever is newest. Reading the feed first spent the whole
                    # budget before a single search ran, in any group with
                    # max_posts_per_group visible posts -- which is all of them.
                    # The feed then fills whatever room is left, because in-group
                    # search is literal and a natural request often misses.
                    # 0 means every post the group will give up. What stops
                    # the reader then is the clock: the browser is shared and
                    # single-threaded, so a group with ten thousand posts must
                    # not hold every other search behind it.
                    budget = self.settings.max_posts_per_group or None
                    deadline = time.monotonic() + self.settings.group_read_seconds
                    found: dict[str, GroupPost] = {}
                    try:
                        for term in terms:
                            room = None if budget is None else budget - len(found)
                            if (room is not None and room <= 0) or time.monotonic() >= deadline:
                                break
                            posts = await search_posts(
                                self.session.page,
                                group_url,
                                term,
                                max_posts=room,
                                deadline=deadline,
                            )
                            for post in posts:
                                found.setdefault(post.post_url, post)
                        room = None if budget is None else budget - len(found)
                        if (room is None or room > 0) and time.monotonic() < deadline:
                            for post in await read_recent_posts(
                                self.session.page,
                                group_url,
                                max_posts=room,
                                deadline=deadline,
                            ):
                                found.setdefault(post.post_url, post)
                    except Exception:
                        # A selector failure or an empty unrecognised layout is
                        # local to this group. Do not abandon the remaining
                        # discovered groups unless the shared session flipped.
                        if await self.session.observe_state() != SessionState.HEALTHY:
                            return SourceSearchResult(hits=hits, failed=True, groups=groups)
                        if not found:
                            group.access = GroupAccess.UNKNOWN_ERROR.value
                            log.exception("facebook.source.group_read_failed", group_url=group_url)
                            continue
                    # The freshest post we can date decides, not whichever one
                    # the feed listed first -- a pinned rules post is often the
                    # oldest thing in the group.
                    ages = [
                        age
                        for age in (age_days(post.posted_at_text) for post in found.values())
                        if age is not None
                    ]
                    newest = min(ages, default=None)
                    latest = next(iter(found.values()), None)
                    # Recorded as active, and asked to join, only on a date we
                    # could actually read: both outlive this search.
                    active = newest is not None and newest <= self.settings.group_activity_days
                    membership = "accessible"
                    if active and self.settings.auto_join_groups and await join_group(self.session.page):
                        membership = "join_requested"
                    if self.store is not None:
                        self.store.record(
                            url=group_url,
                            title=title,
                            last_post_text=latest.posted_at_text if latest else None,
                            access_state=access.value,
                            membership_state=membership,
                            active=active,
                        )
                    # Dropping posts already read needs positive evidence that
                    # they are old. A stamp in a language the parser does not
                    # know is not that evidence -- it used to be, and on a
                    # Spanish-language account it discarded every group.
                    if newest is not None and newest > self.settings.group_activity_days:
                        log.info(
                            "facebook.source.group_inactive",
                            group_url=group_url,
                            newest_days=round(newest, 1),
                        )
                        continue
                    if not found:
                        continue
                    hits.extend(_post_to_hit(post) for post in found.values())

                # Also catch a flip during the last (or only) group.
                failed = await self.session.observe_state() != SessionState.HEALTHY
                return SourceSearchResult(hits=hits, failed=failed, groups=groups)
            except Exception:
                log.exception("facebook.source.read_failed")
                return SourceSearchResult(hits=hits, failed=True, groups=groups)


def _post_to_hit(post: GroupPost) -> SearchHit:
    return SearchHit(
        url=post.post_url,
        title=post.text[:90] or post.group_url,
        snippet=post.text[:300],
        content=post.text,
        author=post.author,
        engines=["facebook_group"],
    )
