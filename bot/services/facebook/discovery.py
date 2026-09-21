"""Discover groups and indexed public posts through web search, without a login.

This source does not drive the browser reader or join groups. Coverage is
limited to search-engine indexing, and summaries use snippets only.
The ordinary web client's social-domain blocklist stays unchanged: this dedicated
source accepts only verified Facebook group/post URL shapes from raw results.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from bot.config import FacebookSettings
from bot.exceptions import SearchError
from bot.logging_conf import get_logger
from bot.models.query import ParsedQuery, SearchQuery
from bot.models.result import SearchHit
from bot.services.pipeline import SourceGroup, SourceSearchResult
from bot.services.search import QueryBuilder, SearXNGClient
from bot.utils.places import mentions_any, place_tokens

log = get_logger(__name__)

PUBLIC_SEARCH_NOTE = (
    "Facebook: публичные публикации найдены через веб-поиск. "
    "Описания основаны на поисковых фрагментах; полный текст не проверен."
)
_FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com"}
_GROUP_PATH = re.compile(r"/groups/([A-Za-z0-9_.-]+)(?:/(posts|permalink)/([A-Za-z0-9]+))?/?$")


def _group_and_post(url: str) -> tuple[str, str | None] | None:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname not in _FACEBOOK_HOSTS
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            return None
        match = _GROUP_PATH.fullmatch(parsed.path)
    except ValueError:
        return None
    if not match or match[1].lower() in {"feed", "discover", "joins", "create"}:
        return None
    group = f"https://www.facebook.com/groups/{match[1]}/"
    post = f"{group}posts/{match[3]}/" if match[3] else None
    return group, post


class FacebookPublicSource:
    """Two intent queries discover groups; at most a few groups get a follow-up."""

    def __init__(
        self,
        settings: FacebookSettings,
        client: SearXNGClient,
        query_builder: QueryBuilder,
    ) -> None:
        self.settings = settings
        self.client = client
        self.query_builder = query_builder

    async def search(self, parsed: ParsedQuery) -> SourceSearchResult:
        if not self.settings.public_search_enabled:
            return SourceSearchResult()
        # A group name rarely contains a listing's exact budget or area.
        discovery_query = parsed.model_copy(
            update={
                "budget_min": None,
                "budget_max": None,
                "area_min": None,
                "area_max": None,
            }
        )
        queries = self.query_builder.build(discovery_query)[:2]
        groups: dict[str, SourceGroup] = {}
        posts: dict[str, SearchHit] = {}
        # Whatever a search engine showed us about each group. A group is
        # reported only if one of these lines names the place that was asked
        # for -- a `site:` operator is a hint to an engine, not a filter, and
        # the groups it returns are otherwise sent to the user unjudged.
        seen_text: dict[str, list[str]] = {}
        failed = False

        async def collect(query: SearchQuery, expected_group: str | None = None) -> None:
            nonlocal failed
            try:
                # search_many would drop Facebook via the general web blocklist.
                batch = await self.client.search(query)
            except SearchError as exc:
                log.warning("facebook.discovery.failed", error=str(exc))
                failed = True
                return
            for hit in batch:
                links = _group_and_post(hit.url)
                if links is None:
                    continue
                group, post = links
                if expected_group is not None and group != expected_group:
                    continue
                if group not in groups:
                    groups[group] = SourceGroup(
                        url=group, title=f"Группа Facebook {group.rstrip('/').rsplit('/', 1)[-1]}",
                    )
                seen_text.setdefault(group, []).extend(
                    text for text in (hit.title, hit.snippet) if text.strip()
                )
                if post is None and hit.title.strip():
                    groups[group].title = hit.title.strip()
                if post is None or not hit.snippet.strip():
                    continue
                previous = posts.get(post)
                if previous is None or len(hit.snippet) > len(previous.snippet):
                    posts[post] = hit.model_copy(
                        update={
                            "url": post,
                            "content": None,
                            "engines": list(dict.fromkeys([*hit.engines, "facebook_public"])),
                        }
                    )

        for query in queries:
            await collect(
                query.model_copy(
                    update={
                        "query": f"site:facebook.com/groups {query.query}",
                    }
                )
            )
        for group in list(groups)[: self.settings.max_discovered_groups]:
            query = queries[0]
            scope = group.removeprefix("https://www.")
            await collect(
                query.model_copy(
                    update={
                        "query": f"site:{scope} {query.query}",
                    }
                ),
                expected_group=group,
            )
        hits = list(posts.values())[: self.client.settings.max_hits]
        place = place_tokens(parsed.location)
        relevant = [
            group
            for url, group in groups.items()
            if mentions_any([group.title, *seen_text.get(url, [])], place)
        ]
        log.info(
            "facebook.discovery.done",
            groups=len(relevant),
            elsewhere=len(groups) - len(relevant),
            posts=len(hits),
            failed=failed,
        )
        return SourceSearchResult(
            hits=hits,
            failed=failed,
            notes=[PUBLIC_SEARCH_NOTE] if hits else [],
            groups=relevant[: self.settings.max_discovered_groups],
        )
