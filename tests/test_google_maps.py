from __future__ import annotations

import httpx
import pytest

from bot.config import GoogleMapsSettings
from bot.models.enums import Mode
from bot.models.query import Location, ParsedQuery
from bot.services.search.google_maps import GoogleMapsSource

QUERY = ParsedQuery(
    mode=Mode.LAND,
    location=Location(city="Madrid", country="Spain"),
    languages=["es", "en"],
    keywords=["terreno urbanizable"],
)


async def _source_with_responses(responses, **options):
    settings = GoogleMapsSettings(
        enabled=True,
        latitude=40.4168,
        longitude=-3.7038,
        poll_seconds=0.001,
        timeout_seconds=0.2,
        **options,
    )
    source = GoogleMapsSource(settings)
    await source._client.aclose()
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    source._client = httpx.AsyncClient(
        base_url=settings.base_url,
        transport=httpx.MockTransport(respond),
    )
    return source, calls


@pytest.mark.asyncio
async def test_create_poll_and_download_csv():
    source, calls = await _source_with_responses(
        [
            httpx.Response(201, json={"id": "job-1"}),
            httpx.Response(200, json={"Status": "running"}),
            httpx.Response(200, json={"Status": "ok"}),
            httpx.Response(
                200,
                text=(
                    "title,address,website,phone,category,emails\n"
                    "Agencia Madrid,""Calle Mayor 1"",https://agency.example,+34123,Agency,info@example\n"
                ),
            ),
        ]
    )
    try:
        result = await source.search(QUERY)
    finally:
        await source.aclose()

    assert not result.failed
    assert len(result.hits) == 1
    assert result.hits[0].url == "https://agency.example"
    assert result.hits[0].title == "Agencia Madrid"
    assert "+34123" in result.hits[0].snippet
    assert [request.method for request in calls] == ["POST", "GET", "GET", "GET"]
    assert calls[0].url.path == "/api/v1/jobs"
    assert calls[-1].url.path == "/api/v1/jobs/job-1/download"
    assert calls[0].content and b"terreno urbanizable" in calls[0].content
    assert b'"email":false' in calls[0].content


@pytest.mark.asyncio
async def test_email_extraction_can_be_enabled_explicitly():
    source, calls = await _source_with_responses(
        [
            httpx.Response(201, json={"id": "job-1"}),
            httpx.Response(200, json={"Status": "ok"}),
            httpx.Response(200, text="title,address,website\nAgency,Address,https://agency.example\n"),
        ],
        extract_emails=True,
    )
    try:
        result = await source.search(QUERY)
    finally:
        await source.aclose()

    assert not result.failed
    assert b'"email":true' in calls[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "responses",
    [
        [httpx.Response(503, text="offline")],
        [httpx.Response(201, json={"id": "job-1"}), httpx.Response(200, json={"Status": "failed"})],
        [httpx.Response(201, json={"id": "job-1"}), httpx.Response(200, json=[])],
    ],
)
async def test_sidecar_errors_become_failed_source(responses):
    source, _ = await _source_with_responses(responses)
    try:
        result = await source.search(QUERY)
    finally:
        await source.aclose()

    assert result.failed
    assert result.hits == []
    assert result.notes == ["Google Maps Scraper Kit недоступен; остальные источники продолжили поиск."]


@pytest.mark.asyncio
async def test_duplicate_rows_are_removed_before_result_limit():
    source, _ = await _source_with_responses(
        [
            httpx.Response(201, json={"id": "job-1"}),
            httpx.Response(200, json={"Status": "ok"}),
            httpx.Response(
                200,
                text=(
                    "title,address,website,phone,category,emails\n"
                    "A,Address A,https://a.example,+34111,Agency,\n"
                    "A duplicate,Address A,https://a.example/?utm_source=maps,+34111,Agency,\n"
                    "B,Address B,https://b.example,+34222,Agency,\n"
                    "C,Address C,https://c.example,+34333,Agency,\n"
                ),
            ),
        ],
        max_results=2,
    )
    try:
        result = await source.search(QUERY)
    finally:
        await source.aclose()

    assert [hit.url for hit in result.hits] == ["https://a.example", "https://b.example"]


@pytest.mark.asyncio
async def test_missing_coordinates_is_a_configured_source_failure():
    source = GoogleMapsSource(GoogleMapsSettings(enabled=True))
    try:
        result = await source.search(QUERY)
    finally:
        await source.aclose()

    assert result.failed
    assert not result.hits
    assert "LATITUDE" in result.notes[0]
