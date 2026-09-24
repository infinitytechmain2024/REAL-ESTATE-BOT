"""Safety and normalization tests for the Orchestra Scrapling worker."""

from __future__ import annotations

import httpx
import pytest

from bot.orchestra.models import AcquisitionMethod
from bot.orchestra.parser import parse_run
from bot.scrapling_connector.connector import ScraplingConnector
from bot.scrapling_connector.models import ScraplingOutcome, ScraplingTask

URL = "https://listing.example/property/1"
HTML = b"""<html><head><title>Villa listing</title></head><body>
<nav>navigation</nav><h1>Villa in Sofia</h1><p>EUR 250000, 120 m2</p><script>secret()</script>
</body></html>"""


async def _public_dns(_: str) -> list[str]:
    return ["93.184.216.34"]


def _task(**changes: object) -> ScraplingTask:
    values: dict[str, object] = {
        "run_id": "run-1", "source_id": "source-1", "target": URL,
        "max_runtime_seconds": 30, "max_pages": 1,
    }
    values.update(changes)
    return ScraplingTask(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_public_page_is_http_fetched_then_scrapling_normalized() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL(URL)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=HTML, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    connector = ScraplingConnector(client=client, resolver=_public_dns)
    result = await connector.fetch(_task())

    assert result.outcome is ScraplingOutcome.COMPLETED
    assert result.page is not None
    assert result.page.canonical_url == URL
    assert result.page.platform == "website"
    assert result.page.source_type == "public_page"
    assert result.page.title == "Villa listing"
    assert "Villa in Sofia" in result.page.text
    assert "secret" not in result.page.text
    await client.aclose()


@pytest.mark.asyncio
async def test_timeout_and_http_error_have_structured_safe_reasons() -> None:
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    timeout_client = httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler))
    timeout = await ScraplingConnector(client=timeout_client, resolver=_public_dns).fetch(_task())
    assert timeout.outcome is ScraplingOutcome.STOPPED_LIMIT
    assert timeout.reason == "request_timeout"
    await timeout_client.aclose()

    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    error_client = httpx.AsyncClient(transport=httpx.MockTransport(error_handler))
    failure = await ScraplingConnector(client=error_client, resolver=_public_dns).fetch(_task())
    assert failure.outcome is ScraplingOutcome.FAILED
    assert failure.reason == "http_503"
    await error_client.aclose()


@pytest.mark.asyncio
async def test_private_targets_redirects_and_content_limits_are_refused() -> None:
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url == httpx.URL(URL):
            return httpx.Response(302, headers={"location": "https://127.0.0.1/internal"}, request=request)
        return httpx.Response(200, content=HTML, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    connector = ScraplingConnector(client=client, resolver=_public_dns)
    redirected = await connector.fetch(_task())
    direct_private = await connector.fetch(_task(target="https://127.0.0.1/private"))
    assert redirected.reason == "private_target_forbidden"
    assert direct_private.reason == "private_target_forbidden"
    assert calls == [URL]
    await client.aclose()

    async def oversized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html", "content-length": "99999"}, content=HTML)

    limited_client = httpx.AsyncClient(transport=httpx.MockTransport(oversized))
    limited = await ScraplingConnector(client=limited_client, resolver=_public_dns, max_content_bytes=10_000).fetch(_task())
    assert limited.outcome is ScraplingOutcome.FAILED
    assert limited.reason == "content_size_limit"
    await limited_client.aclose()


def test_dispatcher_routes_only_ordinary_websites_to_scrapling() -> None:
    website = parse_run(f"website {URL}")
    instagram = parse_run("instagram https://www.instagram.com/example")
    facebook = parse_run("facebook https://www.facebook.com/example")
    assert website.method is AcquisitionMethod.SCRAPLING
    assert instagram.method is AcquisitionMethod.AGENT_REACH
    assert facebook.method is AcquisitionMethod.AGENT_REACH


@pytest.mark.asyncio
async def test_run_limits_are_enforced_before_any_network_request() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("network must not run")))
    connector = ScraplingConnector(client=client, resolver=_public_dns)
    page_limit = await connector.fetch(_task(max_pages=2))
    runtime_limit = await connector.fetch(_task(max_runtime_seconds=301))
    assert page_limit.reason == "invalid_page_limit"
    assert runtime_limit.reason == "invalid_runtime_limit"
    await client.aclose()
