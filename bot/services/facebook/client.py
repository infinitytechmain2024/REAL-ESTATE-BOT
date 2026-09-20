"""Bridges Facebook group reading to the research pipeline's SearchHit shape.

Not wired into :class:`bot.services.pipeline.ResearchPipeline` yet -- see the
module docstring in ``bot/services/facebook/groups.py``. Wire it in only
after ``scripts/facebook_probe.py`` has proven real group search against at
least one real, accessible group; until then this is validated in isolation,
not inside the live bot, per the implementation plan's "prove real group
search before building a polished UI."
"""

from __future__ import annotations

from bot.config import FacebookSettings
from bot.logging_conf import get_logger
from bot.models.query import ParsedQuery
from bot.models.result import SearchHit
from bot.services.facebook.browser import FacebookSession, SessionState
from bot.services.facebook.groups import GroupAccess, GroupPost, check_access, search_posts

log = get_logger(__name__)


class FacebookSource:
    """Reads configured groups for one parsed query and returns pipeline-ready hits.

    Mirrors :class:`bot.services.search.client.SearXNGClient` closely enough
    that :class:`~bot.services.pipeline.ResearchPipeline` can eventually treat
    this as a second, optional hit source: ``search()`` returns
    :class:`SearchHit` objects with ``content`` already filled in, so the
    pipeline's existing rank/dedupe/persist machinery applies unchanged (see
    ``pipeline._collect_content``, which already skips fetching any hit that
    arrives with ``content`` set).
    """

    def __init__(self, settings: FacebookSettings, session: FacebookSession) -> None:
        self.settings = settings
        self.session = session

    async def search(self, parsed: ParsedQuery) -> list[SearchHit]:
        if not self.settings.enabled or not self.settings.group_urls:
            return []

        query_text = parsed.human_summary() or " ".join(parsed.keywords)
        if not query_text:
            return []

        async with self.session.lock:
            state = await self.session.probe_state()
            if state != SessionState.HEALTHY:
                log.warning("facebook.source.session_not_healthy", state=state.value)
                return []

            hits: list[SearchHit] = []
            for group_url in self.settings.group_urls:
                access = await check_access(self.session.page, group_url)
                if access != GroupAccess.ACCESSIBLE:
                    log.info(
                        "facebook.source.group_skipped", group_url=group_url, access=access.value
                    )
                    continue

                posts = await search_posts(
                    self.session.page,
                    group_url,
                    query_text,
                    max_posts=self.settings.max_posts_per_group,
                )
                hits.extend(_post_to_hit(post) for post in posts)

            return hits


def _post_to_hit(post: GroupPost) -> SearchHit:
    return SearchHit(
        url=post.post_url,
        title=post.text[:90] or post.group_url,
        snippet=post.text[:300],
        content=post.text,
        author=post.author,
        engines=["facebook_group"],
    )
