"""Reading a group is worth nothing if what was read is then thrown away.

The source opens a group, reads its newest posts, expands each "See more" and
hands the text to the ranker. Then two rules decide whether any of it survives,
and both were losing posts that had already been read:

1. A group whose newest post could not be *positively* dated was treated as
   inactive and dropped whole. The date parser understood "3 days" and "5 дн.",
   and Facebook renders "2 d", "12 h", "3 días", "hace 2 días", "Ayer",
   "1 week ago" and "2 ч." -- the last one being this repository's own DOM
   fixture. On a Spanish-language account, which is the market, essentially
   every group dated itself out of the results.

2. The localized in-group searches never ran. The recent feed was read first,
   up to max_posts_per_group, and the term loop begins with
   `if len(found) >= max_posts: break` -- so in any group with ten visible
   posts, searching for "terreno", "parcela" or "участок" was dead code.

Not being able to date a post is not evidence that it is old. Only a date we
can read and that is genuinely old is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from bot.config import FacebookSettings
from bot.models.enums import Mode
from bot.models.query import Location, ParsedQuery
from bot.services.facebook import client
from bot.services.facebook.activity import age_days, is_recent
from bot.services.facebook.browser import SessionState
from bot.services.facebook.groups import GroupAccess, GroupPost
from tests.conftest import FakePage, FakeSession

GROUP = "https://www.facebook.com/groups/madrid/"


def _query() -> ParsedQuery:
    return ParsedQuery(
        mode=Mode.LAND,
        location=Location(city="Madrid", country="Spain"),
        object_type="land plot",
        languages=["uk"],
    )


# --- what Facebook actually writes under a post ----------------------------


@pytest.mark.parametrize(
    "stamp",
    [
        "2 d", "12 h", "45 m", "3 w", "1 week ago", "3 hrs", "Yesterday",
        "3 días", "hace 2 días", "2 h", "Ayer", "hace 20 minutos",
        "2 ч.", "5 дн.", "3 нед.", "вчера", "только что",
        "2 год", "5 дн", "3 тиж", "вчора",
    ],
)
def test_the_timestamps_facebook_renders_are_understood(stamp: str) -> None:
    assert is_recent(stamp, max_age_days=30), stamp


@pytest.mark.parametrize("stamp", ["2 months", "3 mo", "6 meses", "2 года", "1 y", "8 нед."])
def test_an_old_post_is_still_old(stamp: str) -> None:
    assert not is_recent(stamp, max_age_days=30), stamp


def test_an_absolute_date_still_works() -> None:
    now = datetime(2026, 9, 21, tzinfo=UTC)

    assert is_recent("20.09.2026", max_age_days=30, now=now)
    assert not is_recent("01.01.2025", max_age_days=30, now=now)


def test_an_unreadable_timestamp_is_unknown_rather_than_old() -> None:
    """The distinction the group filter turns on."""
    assert age_days("21 de septiembre") is None
    assert age_days(None) is None
    assert age_days("") is None
    assert not is_recent("21 de septiembre", max_age_days=30)


# --- what the source does with them ----------------------------------------


def _source(monkeypatch, posts: list[GroupPost], searched: list[str] | None = None):
    session = FakeSession([SessionState.HEALTHY] * 10)
    session.page = FakePage()
    monkeypatch.setattr(client, "check_access", AsyncMock(return_value=GroupAccess.ACCESSIBLE))
    monkeypatch.setattr(client, "read_recent_posts", AsyncMock(return_value=posts))

    async def fake_search(_page, group_url, term, **_kwargs):
        if searched is not None:
            searched.append(term)
        return []

    monkeypatch.setattr(client, "search_posts", fake_search)
    monkeypatch.setattr(client, "join_group", AsyncMock(return_value=False))
    settings = FacebookSettings(enabled=True, group_urls=[GROUP], auto_join_groups=False)
    return client.FacebookSource(settings, session)


def _post(index: int, stamp: str | None) -> GroupPost:
    return GroupPost(
        group_url=GROUP,
        post_url=f"{GROUP}posts/{index}/",
        text=f"Terreno urbanizable {index} 2000 m2",
        posted_at_text=stamp,
    )


async def test_posts_survive_a_timestamp_we_cannot_read(monkeypatch) -> None:
    """A Spanish date is not a reason to discard ten posts already read."""
    source = _source(monkeypatch, [_post(1, "21 de septiembre"), _post(2, "hace 1 hora")])

    result = await source.search(_query())

    assert len(result.hits) == 2, [hit.url for hit in result.hits]


async def test_a_group_whose_posts_are_all_genuinely_old_is_still_skipped(monkeypatch) -> None:
    source = _source(monkeypatch, [_post(1, "8 months"), _post(2, "2 años")])

    result = await source.search(_query())

    assert result.hits == []


async def test_one_datable_recent_post_keeps_the_group(monkeypatch) -> None:
    """The freshest post decides, not whichever one the feed happened to list first."""
    source = _source(monkeypatch, [_post(1, "2 years"), _post(2, "2 d")])

    result = await source.search(_query())

    assert len(result.hits) == 2


async def test_the_localized_searches_run_even_when_the_feed_is_full(monkeypatch) -> None:
    """Three languages of in-group search, pre-empted by ten feed posts."""
    searched: list[str] = []
    feed = [_post(index, "2 d") for index in range(10)]
    source = _source(monkeypatch, feed, searched)

    result = await source.search(_query())

    assert searched, "no in-group search ran"
    assert len(result.hits) == 10
