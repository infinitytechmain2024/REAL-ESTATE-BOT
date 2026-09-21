"""Every post in the group, read from its HTML -- and what that costs.

The decision: no per-group post limit. The bot reads what the feed will give
up, and it reads it out of the DOM -- the post's own text, author and
permalink -- never from a screenshot, and no image ever reaches a model.

Two consequences have to hold for that to be a real capability rather than a
setting nobody can afford to turn on:

  - reading has to terminate. The Facebook browser is shared and
    single-threaded, so one group with ten thousand posts must not hold every
    other search behind it. A wall-clock budget per group ends it.
  - the ranking call takes one prompt. Three hundred posts do not fit in it, so
    something has to choose which are judged, and it must not be "whichever the
    feed listed first".
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

from bot.config import FacebookSettings, Settings
from bot.models.enums import Mode
from bot.models.query import Location, ParsedQuery
from bot.models.result import SearchHit
from bot.services.facebook import client, groups
from bot.services.facebook.browser import SessionState
from bot.services.facebook.groups import GroupAccess, GroupPost
from bot.services.relevance import most_promising, score
from tests.conftest import FakeLocator, FakePage, FakeSession

GROUP = "https://www.facebook.com/groups/madrid/"


def _query() -> ParsedQuery:
    return ParsedQuery(
        mode=Mode.LAND,
        location=Location(city="Madrid", country="Spain"),
        object_type="land plot",
        area_min=2000,
        buildable_required=True,
        languages=["uk"],
    )


# --- the reader ------------------------------------------------------------


async def test_the_feed_is_read_past_any_old_cap(monkeypatch) -> None:
    """No limit means no limit: 40 posts out of a feed that keeps giving."""
    page = FakePage()
    articles = FakeLocator()
    articles.elements = [FakeLocator(count=1)]
    page.locators["div[role='article']"] = articles
    produced = iter(range(40))

    async def one_more(*_args):
        index = next(produced, None)
        if index is None:
            return None  # The feed has nothing left to give.
        return GroupPost(
            group_url=GROUP, post_url=f"{GROUP}posts/{index}/", text=f"Terreno {index}"
        )

    monkeypatch.setattr(groups, "_extract_post", one_more)

    read = await groups.read_recent_posts(page, GROUP, max_posts=None)

    assert len(read) == 40


async def test_reading_one_group_cannot_run_forever(monkeypatch) -> None:
    """The browser is shared. A deadline is what makes "unlimited" safe."""
    page = FakePage()
    articles = FakeLocator()
    articles.elements = [FakeLocator(count=1)]
    page.locators["div[role='article']"] = articles
    counter = iter(range(10_000))
    monkeypatch.setattr(
        groups,
        "_extract_post",
        AsyncMock(
            side_effect=lambda *_args: GroupPost(
                group_url=GROUP, post_url=f"{GROUP}posts/{next(counter)}/", text="Terreno"
            )
        ),
    )

    started = time.monotonic()
    read = await groups.read_recent_posts(page, GROUP, max_posts=None, deadline=started)

    assert read == [], "a spent budget must stop the read before it starts"


async def test_one_unreadable_card_does_not_cost_the_group(monkeypatch) -> None:
    """Over hundreds of posts a malformed one is a certainty, not an edge case.

    In-group search used to let the RuntimeError out, which reached the source
    as a failed read and marked the whole group unreadable.
    """
    page = FakePage(
        url="https://www.facebook.com/groups/madrid/search/?q=terreno",
        placeholders={"Buscar en este grupo": 1},
    )
    articles = FakeLocator()
    articles.elements = [FakeLocator(count=1), FakeLocator(count=1)]
    page.locators["div[role='article']"] = articles
    good = GroupPost(group_url=GROUP, post_url=f"{GROUP}posts/7/", text="Terreno urbanizable")
    monkeypatch.setattr(
        groups,
        "_extract_post",
        AsyncMock(side_effect=[RuntimeError("no permalink"), good, RuntimeError("no permalink")]),
    )

    found = await groups.search_posts(page, GROUP, "terreno", max_posts=1)

    assert found == [good]


async def test_the_source_reads_without_a_post_limit(monkeypatch) -> None:
    """max_posts_per_group=0 reaches the reader as "no limit", not as zero."""
    session = FakeSession([SessionState.HEALTHY] * 10)
    session.page = FakePage()
    monkeypatch.setattr(client, "check_access", AsyncMock(return_value=GroupAccess.ACCESSIBLE))
    monkeypatch.setattr(client, "join_group", AsyncMock(return_value=False))
    limits: list[int | None] = []

    async def fake_recent(_page, group_url, *, max_posts=None, deadline=None):
        limits.append(max_posts)
        assert deadline is not None, "reading must always carry a deadline"
        return [
            GroupPost(
                group_url=group_url,
                post_url=f"{group_url}posts/{index}/",
                text=f"Terreno urbanizable {index} 2500 m2 Madrid",
                posted_at_text="2 d",
            )
            for index in range(25)
        ]

    monkeypatch.setattr(client, "read_recent_posts", fake_recent)
    monkeypatch.setattr(client, "search_posts", AsyncMock(return_value=[]))
    source = client.FacebookSource(
        FacebookSettings(enabled=True, group_urls=[GROUP], auto_join_groups=False), session
    )

    result = await source.search(_query())

    assert limits == [None], limits
    assert len(result.hits) == 25


# --- what reaches the model ------------------------------------------------


def test_the_prompt_budget_keeps_the_best_posts_not_the_first() -> None:
    noise = [
        (SearchHit(url=f"https://facebook.com/p/{index}", title="Продам диван, самовывоз"), None)
        for index in range(200)
    ]
    wanted = (
        SearchHit(
            url="https://facebook.com/p/wanted",
            title="Vendo parcela urbanizable 2.500 m2 en Madrid, 180.000 €",
        ),
        None,
    )

    kept = most_promising([*noise, wanted], _query(), limit=60)

    assert len(kept) == 60
    assert wanted in kept


def test_a_post_that_says_nothing_useful_is_ranked_last_not_dropped() -> None:
    """A badly written post about the right plot is still about the right plot."""
    bare = (SearchHit(url="https://facebook.com/p/bare", title="Vendo"), None)

    kept = most_promising([bare], _query(), limit=60)

    assert kept == [bare]
    assert score(bare[0], None, _query()) == 0.0


def test_the_place_outranks_everything_else() -> None:
    here = SearchHit(url="https://facebook.com/p/1", title="Parcela en Madrid")
    elsewhere = SearchHit(url="https://facebook.com/p/2", title="Parcela urbanizable 3000 m2 90.000 €")

    assert score(here, None, _query()) > 0
    kept = most_promising([(elsewhere, None), (here, None)], _query(), limit=1)

    assert kept == [(here, None)]


def test_the_ranking_budget_is_configurable() -> None:
    assert Settings().pipeline.max_candidates_to_rank >= 1


# --- structure, not pictures ----------------------------------------------


def test_nothing_reads_the_screen_instead_of_the_page() -> None:
    """The decision, written down: posts are parsed from the DOM.

    A screenshot would need a vision model on every post, cost per image, and
    would lose the permalink and the author -- both of which are attributes,
    not pixels.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "bot"
    offenders = [
        path.relative_to(root.parent).as_posix()
        for path in root.rglob("*.py")
        if ".screenshot(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == [], f"these read the screen rather than the page: {offenders}"
