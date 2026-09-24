from unittest.mock import AsyncMock

import pytest

from bot.exceptions import SearchError
from bot.handlers.search import _send_results
from bot.models.enums import Mode
from bot.models.query import Location, ParsedQuery
from bot.models.result import SearchHit, StructuredResult

QUERY = ParsedQuery(
    mode=Mode.LAND,
    location=Location(city="Valencia", country="Spain"),
    keywords=["terreno"],
    languages=["es", "en"],
    budget_max=120000,
)
GROUP = "https://www.facebook.com/groups/valencialand/"
POST = GROUP + "posts/123/"


async def test_discovers_groups_then_matching_posts_without_configured_groups(
    public_source_factory,
):
    source, search = public_source_factory(
        [
            [SearchHit(url=GROUP, title="Land in Valencia")],
            [],
            [
                SearchHit(
                    url=POST + "?ref=share", title="Terreno", snippet="Parcela Valencia 100000 EUR"
                )
            ],
        ]
    )
    outcome = await source.search(QUERY)
    assert not outcome.failed
    assert [hit.url for hit in outcome.hits] == [POST]
    assert outcome.hits[0].content is None  # A snippet must not pretend to be full post text.
    assert "facebook_public" in outcome.hits[0].engines
    assert outcome.notes and "поисков" in outcome.notes[0]
    queries = [call.args[0].query for call in search.search.await_args_list]
    assert len(queries) == 3
    assert all("Valencia" in query for query in queries)
    assert queries[0].startswith("site:facebook.com/groups ")
    assert queries[2].startswith("site:facebook.com/groups/valencialand/ ")


async def test_only_post_links_survive_and_duplicates_are_normalised(public_source_factory):
    source, _ = public_source_factory(
        [
            [
                SearchHit(url=POST + "?ref=share", snippet="Land"),
                SearchHit(
                    url="https://m.facebook.com/groups/valencialand/permalink/123/", snippet="Land"
                ),
                SearchHit(
                    url="https://facebook.com.evil.test/groups/foo/posts/1", snippet="Wrong host"
                ),
                SearchHit(
                    url="https://evil.test/facebook.com/groups/foo/posts/1", snippet="Wrong host"
                ),
                SearchHit(
                    url="https://user@facebook.com/groups/foo/posts/1", snippet="Credentials"
                ),
                SearchHit(url="https://www.facebook.com/login/", snippet="Login"),
                SearchHit(url=GROUP, snippet="Group description"),
            ],
            [],
            [],
        ]
    )
    result = await source.search(QUERY)
    assert [hit.url for hit in result.hits] == [POST]


async def test_group_discovery_is_bounded(public_source_factory):
    source, search = public_source_factory(
        [
            [SearchHit(url=f"https://www.facebook.com/groups/group{i}/") for i in range(20)],
            [],
            [],
            [],
        ],
        max_discovered_groups=2,
    )
    result = await source.search(QUERY)
    assert result.hits == [] and not result.failed
    assert search.search.await_count == 4  # Two discovery queries plus two groups.


async def test_disabled_public_search_does_not_make_requests(public_source_factory):
    source, search = public_source_factory([], public_search_enabled=False)
    result = await source.search(QUERY)
    assert not result.hits and not result.failed
    search.search.assert_not_awaited()


async def test_failed_discovery_is_not_a_successful_empty_search(public_source_factory):
    source, _ = public_source_factory([SearchError("offline"), SearchError("offline")])
    result = await source.search(QUERY)
    assert result.failed and not result.hits


async def test_failed_group_search_keeps_previous_posts_and_reports_failure(public_source_factory):
    source, _ = public_source_factory(
        [
            [SearchHit(url=POST, snippet="Land")],
            [],
            SearchError("offline"),
        ]
    )
    result = await source.search(QUERY)
    assert result.failed and [hit.url for hit in result.hits] == [POST]


@pytest.mark.parametrize("mode", [Mode.LAND, Mode.INVESTORS])
async def test_discovery_queries_follow_the_request_intent(public_source_factory, mode):
    source, search = public_source_factory([[], []])
    query = QUERY.model_copy(update={"mode": mode, "keywords": []})
    await source.search(query)
    query_text = " ".join(call.args[0].query for call in search.search.await_args_list)
    expected = "terreno" if mode == Mode.LAND else "inversores"
    assert expected in query_text


async def test_public_posts_reach_pipeline_without_fetching_login_pages(
    public_source_factory,
    pipeline_factory,
):
    source, _ = public_source_factory(
        [
            [SearchHit(url=POST, title="Terreno", snippet="Parcela en Valencia")],
            [],
            [],
        ]
    )
    pipeline, repo = pipeline_factory(source)
    pipeline.extract_query.return_value = QUERY
    pipeline.search.search_many.return_value = [SearchHit(url=POST, title="Duplicate")]
    pipeline.fetcher.fetch_many = AsyncMock(return_value={})
    pipeline.rank.return_value = [
        StructuredResult(url=POST, title="Terreno", summary="Parcela en Valencia", score=90),
    ]
    outcome = await pipeline.run(user_id=1, mode=Mode.LAND, text="terreno Valencia")
    assert outcome.hits_found == 1
    assert len(outcome.results) == 1
    assert outcome.source_notes
    pipeline.fetcher.fetch_many.assert_not_awaited()
    candidates = pipeline.rank.call_args.args[1]
    assert candidates[0][1] is None
    assert candidates[0][0].snippet == "Parcela en Valencia"
    repo.save_results.assert_awaited_once()
    message = AsyncMock()
    await _send_results(message, AsyncMock(), outcome, pipeline.settings, Mode.LAND)
    assert outcome.source_notes[0] in message.answer.call_args.args[0]


async def test_saved_public_post_details_do_not_fetch_a_login_wall(
    public_source_factory,
    pipeline_factory,
):
    source, _ = public_source_factory(
        [
            [SearchHit(url=POST, snippet="Parcela en Valencia")],
            [],
            [],
        ]
    )
    pipeline, _ = pipeline_factory(source)
    pipeline.extract_query.return_value = QUERY
    pipeline.rank.return_value = [
        StructuredResult(url=POST, title="Terreno", summary="Parcela en Valencia", score=90),
    ]
    pipeline.fetcher.fetch = AsyncMock()
    outcome = await pipeline.run(user_id=1, mode=Mode.LAND, text="terreno Valencia")
    assert outcome.results[0].raw["source_kind"] == "facebook_public"
    details = await pipeline.details(QUERY, outcome.results[0])
    assert "фрагмент" in details and "не проверен" in details
    pipeline.fetcher.fetch.assert_not_awaited()


async def test_discovery_uses_real_client_json_conversion_despite_web_domain_filter():
    import httpx

    from bot.config import FacebookSettings, SearxngSettings
    from bot.services.facebook.discovery import FacebookPublicSource
    from bot.services.search import QueryBuilder, SearXNGClient

    settings = SearxngSettings()
    client = SearXNGClient(settings)
    assert client.is_blocked(POST)  # The ordinary web-search filter stays intact.
    await client.aclose()
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": POST,
                        "title": "Terreno",
                        "content": "Valencia 100000 EUR",
                        "engine": "bing",
                    },
                ]
            },
        )

    client._client = httpx.AsyncClient(
        base_url=settings.url,
        transport=httpx.MockTransport(respond),
    )
    try:
        source = FacebookPublicSource(FacebookSettings(), client, QueryBuilder(settings))
        result = await source.search(QUERY)
        assert not result.failed
        assert [hit.url for hit in result.hits] == [POST]
        assert result.hits[0].snippet == "Valencia 100000 EUR"
        assert len(requests) == 3
        assert all(
            request.url.params["q"].startswith("site:facebook.com/groups") for request in requests
        )
    finally:
        await client.aclose()


async def test_discovered_groups_are_visible_even_without_matching_posts(
    public_source_factory, pipeline_factory,
):
    source, _ = public_source_factory([
        [SearchHit(url=GROUP, title='Terrenos & Parcelas <Valencia>')], [], [],
    ])
    pipeline, _ = pipeline_factory(source)
    pipeline.extract_query.return_value = QUERY
    outcome = await pipeline.run(user_id=1, mode=Mode.LAND, text='terreno Valencia')
    assert [(group.url, group.title) for group in outcome.source_groups] == [
        (GROUP, 'Terrenos & Parcelas <Valencia>'),
    ]
    message = AsyncMock()
    await _send_results(message, AsyncMock(), outcome, pipeline.settings, Mode.LAND)
    text = message.answer.call_args.args[0]
    assert GROUP in text
    assert 'Terrenos &amp; Parcelas &lt;Valencia&gt;' in text
    assert 'Группы Facebook' in text
