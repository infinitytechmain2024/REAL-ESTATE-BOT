"""Bridges Facebook group reading to the research pipeline's SearchHit shape.

Wired into :class:`bot.services.pipeline.ResearchPipeline` as a second hit
source alongside SearXNG: both run for the same request and their hits are
merged into one ranked list, so the user gets a single answer rather than a
web list and a Facebook list to reconcile by hand.

It stays optional and non-fatal. ``FACEBOOK_ENABLED=false``, no configured
groups, an unhealthy session or an extraction that finds nothing all return
an empty list, and the web half of the answer is unaffected -- the selectors
in ``groups.py`` are still a documented starting point rather than a
contract, so this source is expected to come back empty sometimes.
"""

from __future__ import annotations

from urllib.parse import urljoin

from bot.config import FacebookSettings
from bot.logging_conf import get_logger
from bot.models.enums import HitSource
from bot.models.query import ParsedQuery
from bot.models.result import SearchHit
from bot.services.facebook.browser import FACEBOOK_HOME, FacebookSession, SessionState
from bot.services.facebook.groups import GroupAccess, GroupPost, check_access, search_posts
from bot.utils.text import truncate

log = get_logger(__name__)


class FacebookSource:
    """Reads configured groups for one parsed query and returns pipeline-ready hits.

    Mirrors :class:`bot.services.search.client.SearXNGClient` closely enough
    that :class:`~bot.services.pipeline.ResearchPipeline` treats it as a
    second, optional hit source: ``search()`` returns :class:`SearchHit`
    objects with ``content`` already filled in, so the pipeline's existing
    merge/rank/persist machinery applies unchanged (see
    ``pipeline._collect_content``, which skips fetching any hit that arrives
    with ``content`` set).
    """

    def __init__(self, settings: FacebookSettings, session: FacebookSession) -> None:
        self.settings = settings
        self.session = session

    async def search(self, parsed: ParsedQuery) -> list[SearchHit]:
        """Read every configured group for *parsed* and return pipeline hits.

        Never raises: the pipeline runs this concurrently with the web search
        and one dead source must not cost the user the other one.
        """
        if not self.settings.enabled or not self.settings.group_urls:
            return []

        query_text = parsed.human_summary() or " ".join(parsed.keywords)
        if not query_text:
            return []

        async with self.session.lock:
            # Idempotent: the browser is started lazily, on the first search
            # that needs it, rather than at boot. Configuring groups and
            # setting FACEBOOK_ENABLED is the operator opting in to exactly
            # this, so a search does not have to be preceded by /facebook.
            await self.session.start()
            state = await self.session.check_state()
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
                hits.extend(_post_to_hit(post, query_text) for post in posts)

            log.info(
                "facebook.source.done",
                groups=len(self.settings.group_urls),
                hits=len(hits),
            )
            return hits


def _post_to_hit(post: GroupPost, query: str) -> SearchHit:
    """One group post as a hit the pipeline can rank next to a web result.

    The score is a modest constant rather than zero: group posts carry no
    engine ranking, and the LLM re-scores everything anyway, so this only
    decides where a post sits in the candidate list handed to the ranker.
    """
    return SearchHit(
        url=_absolute(post.post_url),
        title=truncate(post.text, 90) or post.group_url,
        snippet=truncate(post.text, 300),
        content=post.text,
        author=post.author,
        engines=["facebook_group"],
        score=1.0,
        query=query,
        source=HitSource.FACEBOOK,
    )


def _absolute(url: str) -> str:
    """Facebook permalinks come out of the DOM as site-relative paths.

    A relative URL would be stored, sent and clicked as a broken link, and
    would hash differently from the same post found again later.
    """
    if url.startswith(("http://", "https://")):
        return url
    return urljoin(FACEBOOK_HOME, url)
