from unittest.mock import AsyncMock

import pytest

from bot.config import FacebookSettings
from bot.models.enums import Mode
from bot.models.query import Location, ParsedQuery
from bot.models.result import StructuredResult
from bot.services.facebook import client
from bot.services.facebook.browser import SessionState
from bot.services.facebook.groups import GroupAccess, GroupPost
from tests.conftest import FakePage, FakeSession


def _madrid() -> ParsedQuery:
    """A request with a place in it -- group discovery needs one to search."""
    return ParsedQuery(
        mode=Mode.LAND, location=Location(city="Madrid", country="Spain"), keywords=["land"]
    )


async def test_flip_preserves_complete_hits_but_never_writes_partial_job(
    monkeypatch, pipeline_factory
):
    session = FakeSession([SessionState.HEALTHY, SessionState.LOGIN_NEEDED])
    session.page = FakePage()
    groups = ["https://facebook.com/groups/1", "https://facebook.com/groups/2"]
    access = AsyncMock(return_value=GroupAccess.ACCESSIBLE)
    posts = AsyncMock(
        return_value=[
            GroupPost(
                group_url=groups[0],
                post_url=groups[0] + "/posts/1",
                text="Land in Spain",
            )
        ]
    )
    monkeypatch.setattr(client, "check_access", access)
    monkeypatch.setattr(client, "search_posts", posts)
    source = client.FacebookSource(FacebookSettings(enabled=True, group_urls=groups), session)
    pipeline, repo = pipeline_factory(source)
    pipeline.rank.return_value = [
        StructuredResult(
            url=groups[0] + "/posts/1",
            title="Land",
            summary="Land",
            score=90,
        )
    ]
    outcome = await pipeline.run(user_id=1, mode=Mode.LAND, text="land")
    assert outcome.failed_sources == ["facebook"]
    assert outcome.hits_found == len(outcome.results) == 1
    assert outcome.results[0].id is None
    assert access.await_count == 1
    # One group reached before the flip, however many phrases it was searched
    # with -- the point is that the second group was never opened.
    assert len({call.args[1] for call in posts.await_args_list}) == 1
    assert session.probe_calls == 1 and session.observed_unlocked == 0
    repo.save_results.assert_not_awaited()


@pytest.mark.parametrize("access_state", [GroupAccess.UNKNOWN_ERROR, GroupAccess.LOGIN_REQUIRED])
async def test_failed_group_read_is_not_no_matches(monkeypatch, access_state):
    session = FakeSession([SessionState.HEALTHY])
    session.page = FakePage()
    monkeypatch.setattr(client, "check_access", AsyncMock(return_value=access_state))
    source = client.FacebookSource(FacebookSettings(enabled=True, group_urls=["group"]), session)
    result = await source.search(ParsedQuery(mode=Mode.LAND, keywords=["land"]))
    assert result.failed is True


async def test_read_exception_skips_one_group_and_reads_the_next(monkeypatch):
    session = FakeSession([SessionState.HEALTHY] * 4)
    session.page = FakePage()
    monkeypatch.setattr(client, "check_access", AsyncMock(return_value=GroupAccess.ACCESSIBLE))
    groups = ["first", "second"]
    async def posts_for(_page, group_url, _term, **_kwargs):
        if group_url == "first":
            raise RuntimeError("selector changed in first group")
        return [GroupPost(group_url="second", post_url="second/posts/1", text="Land")]

    posts = AsyncMock(side_effect=posts_for)
    monkeypatch.setattr(client, "search_posts", posts)
    source = client.FacebookSource(FacebookSettings(enabled=True, group_urls=groups), session)
    result = await source.search(ParsedQuery(mode=Mode.LAND, keywords=["land"]))
    assert result.failed is False
    assert [hit.url for hit in result.hits] == ["second/posts/1"]
    # Location-first planning sends two short local terms instead of the old
    # full-query variants; the failed group stops after its first failed term.
    assert posts.await_count == 3
    assert {call.args[1] for call in posts.await_args_list} == {"first", "second"}


async def test_missing_search_box_is_a_failed_read():
    from bot.services.facebook.groups import search_posts

    with pytest.raises(RuntimeError, match="search box"):
        await search_posts(FakePage(), "group", "land", max_posts=5)


async def test_healthy_empty_group_is_a_successful_read(monkeypatch):
    session = FakeSession([SessionState.HEALTHY] * 2)
    session.page = FakePage()
    monkeypatch.setattr(client, "check_access", AsyncMock(return_value=GroupAccess.ACCESSIBLE))
    monkeypatch.setattr(client, "search_posts", AsyncMock(return_value=[]))
    source = client.FacebookSource(FacebookSettings(enabled=True, group_urls=["first"]), session)
    result = await source.search(ParsedQuery(mode=Mode.LAND, keywords=["land"]))
    assert result.failed is False
    assert result.hits == []


async def test_no_configured_groups_are_discovered_and_read(monkeypatch):
    session = FakeSession([SessionState.HEALTHY, SessionState.HEALTHY])
    session.page = FakePage()
    monkeypatch.setattr(
        client,
        "discover_groups",
        AsyncMock(return_value=[("https://facebook.com/groups/public", "Public land Madrid")]),
    )
    monkeypatch.setattr(client, "check_access", AsyncMock(return_value=GroupAccess.ACCESSIBLE))
    monkeypatch.setattr(
        client,
        "search_posts",
        AsyncMock(return_value=[
            GroupPost(
                group_url="https://facebook.com/groups/public",
                post_url="https://facebook.com/groups/public/posts/1/",
                author="Author",
                text="Land in Spain",
            )
        ]),
    )
    source = client.FacebookSource(FacebookSettings(enabled=True), session)
    result = await source.search(_madrid())
    assert result.hits[0].author == "Author"
    assert [(group.title, group.access) for group in result.groups] == [
        ("Public land Madrid", "accessible")
    ]


async def test_discovered_groups_are_location_filtered_and_return_post_links(monkeypatch):
    session = FakeSession([SessionState.HEALTHY] * 3)
    session.page = FakePage()
    monkeypatch.setattr(
        client,
        "discover_groups",
        AsyncMock(
            return_value=[
                ("https://facebook.com/groups/vinnytsia", "Bazar Vinnytsia объявления"),
                ("https://facebook.com/groups/madrid", "Terrenos Madrid y alrededores"),
            ]
        ),
    )
    monkeypatch.setattr(client, "check_access", AsyncMock(return_value=GroupAccess.ACCESSIBLE))
    monkeypatch.setattr(client, "read_recent_posts", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        client,
        "search_posts",
        AsyncMock(
            return_value=[
                GroupPost(
                    group_url="https://facebook.com/groups/madrid",
                    post_url="https://facebook.com/groups/madrid/posts/42/",
                    text="Terreno urbanizable en Madrid",
                )
            ]
        ),
    )
    source = client.FacebookSource(FacebookSettings(enabled=True), session)

    result = await source.search(
        ParsedQuery(
            mode=Mode.LAND,
            location=Location(city="Madrid", country="Spain"),
            keywords=["terreno"],
        )
    )

    assert [group.url for group in result.groups] == ["https://facebook.com/groups/madrid"]
    assert [hit.url for hit in result.hits] == ["https://facebook.com/groups/madrid/posts/42/"]


async def test_browser_started_for_a_healthy_job_stays_available(monkeypatch):
    session = FakeSession([SessionState.HEALTHY, SessionState.HEALTHY])
    session.has_live_context = False
    session.page = FakePage()
    monkeypatch.setattr(
        client,
        "discover_groups",
        AsyncMock(return_value=[("https://facebook.com/groups/public", "Public land Madrid")]),
    )
    monkeypatch.setattr(client, "check_access", AsyncMock(return_value=GroupAccess.ACCESSIBLE))
    monkeypatch.setattr(client, "search_posts", AsyncMock(return_value=[]))
    source = client.FacebookSource(FacebookSettings(enabled=True), session)
    result = await source.search(_madrid())
    assert not result.failed
    assert session.start_calls == 1
    assert session.stop_calls == 0


@pytest.mark.parametrize(
    "access_state",
    [GroupAccess.MEMBERSHIP_REQUIRED, GroupAccess.PENDING_APPROVAL, GroupAccess.UNAVAILABLE],
)
async def test_known_group_access_limits_do_not_fail_other_groups(
    monkeypatch, pipeline_factory, access_state
):
    session = FakeSession([SessionState.HEALTHY] * 4)
    session.page = FakePage()
    groups = [f"https://facebook.com/groups/{index}" for index in range(3)]
    access = AsyncMock(side_effect=[GroupAccess.ACCESSIBLE, access_state, GroupAccess.ACCESSIBLE])
    # Keyed by group rather than by call order: each accessible group yields
    # its post however many localized phrases it is searched with.
    async def posts_for(_page, group_url, _term, **_kwargs):
        return [
            GroupPost(group_url=group_url, post_url=group_url + "/posts/1", text="Land in Spain")
        ]

    posts = AsyncMock(side_effect=posts_for)
    monkeypatch.setattr(client, "check_access", access)
    monkeypatch.setattr(client, "search_posts", posts)
    source = client.FacebookSource(FacebookSettings(enabled=True, group_urls=groups), session)
    pipeline, repo = pipeline_factory(source)
    expected_urls = [group + "/posts/1" for group in (groups[0], groups[2])]
    pipeline.rank.return_value = [
        StructuredResult(url=url, title="Land", summary="Land", score=90)
        for url in expected_urls
    ]

    outcome = await pipeline.run(user_id=1, mode=Mode.LAND, text="land")

    assert outcome.failed_sources == []
    assert [row.url for row in outcome.results] == expected_urls
    assert [call.args[1] for call in access.await_args_list] == groups
    # Distinct groups, not call count: each group is now searched once per
    # localized phrase, and what this protects is that the restricted group in
    # the middle was skipped while the other two were still read.
    searched = list(dict.fromkeys(call.args[1] for call in posts.await_args_list))
    assert searched == [groups[0], groups[2]]
    repo.save_results.assert_awaited_once()
    assert [row.url for row in repo.save_results.call_args.args[0]] == expected_urls
    assert session.observed_unlocked == 0
