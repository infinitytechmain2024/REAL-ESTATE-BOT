from unittest.mock import AsyncMock

import pytest

from bot.config import FacebookSettings
from bot.handlers.search import _send_results
from bot.models.enums import Mode
from bot.models.query import ParsedQuery
from bot.models.result import SearchHit, StructuredResult
from bot.services.facebook.browser import SessionState
from bot.services.facebook.client import FacebookSource
from tests.conftest import FakeSession, FakeSource


@pytest.mark.parametrize("failed", [False, True])
async def test_empty_source_failure_reaches_user(pipeline_factory, failed):
    from bot.services.pipeline import SourceSearchResult

    pipeline, repo = pipeline_factory(FakeSource(SourceSearchResult(failed=failed)))
    outcome = await pipeline.run(user_id=1, mode=Mode.LAND, text="land")
    message, status = AsyncMock(), AsyncMock()
    await _send_results(message, status, outcome, pipeline.settings, Mode.LAND)
    text = status.edit_text.call_args.args[0]
    assert outcome.failed_sources == (["facebook"] if failed else [])
    if failed:
        assert "Facebook" in text and "недоступ" in text and "позже" in text
    else:
        assert "недоступ" not in text
    repo.save_results.assert_not_awaited()


async def test_results_include_unavailable_source_notice(pipeline_factory):
    from bot.services.pipeline import SourceSearchResult

    pipeline, _ = pipeline_factory(FakeSource(SourceSearchResult(failed=True)))
    hit = SearchHit(url="https://example.com/land", title="Land", content="Land")
    pipeline.search.search_many.return_value = [hit]
    pipeline.rank.return_value = [
        StructuredResult(url=hit.url, title="Land", summary="Land", score=90)
    ]
    outcome = await pipeline.run(user_id=1, mode=Mode.LAND, text="land")
    message = AsyncMock()
    await _send_results(message, AsyncMock(), outcome, pipeline.settings, Mode.LAND)
    assert "Facebook" in message.answer.call_args.args[0]
    assert "недоступ" in message.answer.call_args.args[0]


async def test_unhealthy_facebook_is_not_an_empty_success():
    session = FakeSession([SessionState.LOGIN_NEEDED])
    source = FacebookSource(
        FacebookSettings(enabled=True, group_urls=["https://facebook.com/groups/1"]), session
    )
    result = await source.search(ParsedQuery(mode=Mode.LAND, keywords=["land"]))
    assert result.failed is True
    assert result.hits == []
    assert session.observed_unlocked == 0


async def test_failed_source_survives_all_seen_early_return(pipeline_factory):
    from bot.services.pipeline import SourceSearchResult

    pipeline, repo = pipeline_factory(FakeSource(SourceSearchResult(failed=True)))
    pipeline.settings.pipeline.skip_seen_results = True
    pipeline.search.search_many.return_value = [SearchHit(url="https://example.com/seen")]
    repo.filter_unseen = AsyncMock(return_value=set())
    outcome = await pipeline.run(user_id=1, mode=Mode.LAND, text="land")
    assert outcome.failed_sources == ["facebook"]
    assert outcome.duplicates_skipped == 1
    repo.save_results.assert_not_awaited()


async def test_only_completed_source_results_are_persisted(pipeline_factory):
    from bot.services.pipeline import SourceSearchResult

    partial = SearchHit(url="https://facebook.com/groups/1/posts/1", content="Land")
    web = SearchHit(url="https://example.com/land", content="Land")
    pipeline, repo = pipeline_factory(FakeSource(SourceSearchResult(hits=[partial], failed=True)))
    pipeline.search.search_many.return_value = [web]
    pipeline.rank.return_value = [
        StructuredResult(url=hit.url, title="Land", summary="Land", score=90)
        for hit in [partial, web]
    ]
    outcome = await pipeline.run(user_id=1, mode=Mode.LAND, text="land")
    assert len(outcome.results) == 2
    assert [row.url for row in repo.save_results.call_args.args[0]] == [web.url]
