"""Mock-only contract tests for the default-off Apify Facebook adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from bot.acquisition.evidence import Evidence
from bot.facebook_collector.apify.client import ApifyClient, RunHandle, RunStatus
from bot.facebook_collector.apify.mapper import apify_item_to_evidence
from bot.facebook_collector.apify.provider import (
    ApifyBatch,
    ApifyFacebookConfig,
    ApifyFacebookProvider,
)


@pytest.mark.asyncio
async def test_adapter_does_not_retry_on_launch_unknown() -> None:
    requests: list[httpx.Request] = []

    def fail(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, request=request)

    client = ApifyClient(
        "secret-token",
        client=httpx.AsyncClient(
            base_url="https://api.apify.com/v2/", transport=httpx.MockTransport(fail)
        ),
    )

    result = await client.run_actor("apify/facebook-groups-scraper", "build123", {}, {})

    assert result == RunHandle(None, None, "LAUNCH_UNKNOWN")
    assert len(requests) == 1
    assert "secret-token" not in str(requests[0].url)
    assert "secret-token" not in repr(client)


@pytest.mark.asyncio
async def test_client_retries_get_three_times_with_backoff() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            request=request,
            json={"data": {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "data1"}},
        )

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = ApifyClient(
        "token",
        client=httpx.AsyncClient(
            base_url="https://api.apify.com/v2/", transport=httpx.MockTransport(handler)
        ),
        sleep=sleep,
        jitter=lambda: 0,
    )

    status = await client.get_run_status("run1")

    assert status.status == "SUCCEEDED"
    assert attempts == 4
    assert sleeps == [2, 4, 8]


@pytest.mark.asyncio
async def test_client_uses_retry_after_with_separate_429_limit() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return httpx.Response(429, request=request, headers={"Retry-After": "7"})
        return httpx.Response(
            200, request=request, json={"data": {"id": "run1", "status": "SUCCEEDED"}}
        )

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = ApifyClient(
        "token",
        client=httpx.AsyncClient(
            base_url="https://api.apify.com/v2/", transport=httpx.MockTransport(handler)
        ),
        sleep=sleep,
        jitter=lambda: 0,
        retry_count=0,
    )

    assert (await client.get_run_status("run1")).status == "SUCCEEDED"
    assert attempts == 3
    assert sleeps == [7, 7]


@dataclass
class FakeStore:
    plan: ApifyBatch
    events: list[tuple[Any, ...]] = field(default_factory=list)

    async def load_plan(self, _batch_id: str) -> ApifyBatch:
        return self.plan

    async def save_run(self, *args: Any) -> None:
        self.events.append(("run", *args))

    async def save_launch_unknown(self, *args: Any) -> None:
        self.events.append(("unknown", *args))

    async def save_item(self, run_id: str, index: int, raw: dict[str, Any], evidence: Evidence) -> None:
        self.events.append(("item", run_id, index, raw, evidence))

    async def checkpoint(self, run_id: str, offset: int) -> None:
        self.events.append(("checkpoint", run_id, offset))

    async def finish(self, *args: Any) -> None:
        self.events.append(("finish", *args))


class FakeClient:
    configured = True

    def __init__(self, status: RunStatus, items: list[dict[str, Any]]) -> None:
        self.status, self.items = status, items
        self.dataset_calls: list[tuple[str, int, int]] = []
        self.launches = 0

    async def run_actor(self, *_args: Any, **_kwargs: Any) -> RunHandle:
        self.launches += 1
        return RunHandle(self.status.run_id, self.status.dataset_id, "READY")

    async def get_run_status(self, _run_id: str) -> RunStatus:
        return self.status

    async def iter_dataset_items(self, dataset_id: str, *, offset: int, page_size: int):
        self.dataset_calls.append((dataset_id, offset, page_size))
        for item in self.items:
            yield item


def config() -> ApifyFacebookConfig:
    return ApifyFacebookConfig("apify/facebook-groups-scraper", "build123", checkpoint_every=2)


@pytest.mark.asyncio
async def test_adapter_resumes_from_checkpoint() -> None:
    store = FakeStore(ApifyBatch("batch", "campaign", ("https://facebook.com/groups/a",), "run1", "data1", 4))
    client = FakeClient(RunStatus("run1", "data1", "SUCCEEDED", 0.25), [
        {"url": "https://facebook.com/groups/a/posts/5", "text": "post five"},
        {"url": "https://facebook.com/groups/a/posts/6", "text": "post six"},
    ])

    result = await ApifyFacebookProvider(store, client, config()).run("batch")  # type: ignore[arg-type]

    assert result == "succeeded"
    assert client.launches == 0
    assert client.dataset_calls == [("data1", 4, 1000)]
    assert [(event[0], event[2]) for event in store.events if event[0] == "item"] == [("item", 4), ("item", 5)]
    assert ("checkpoint", "run1", 6) in store.events


@pytest.mark.asyncio
async def test_adapter_distinguishes_empty_run_from_inaccessible_group() -> None:
    empty_store = FakeStore(ApifyBatch("empty", "campaign", ("https://facebook.com/groups/a",), "run1", "data1"))
    empty = FakeClient(RunStatus("run1", "data1", "SUCCEEDED", 0.01), [])
    blocked_store = FakeStore(ApifyBatch("blocked", "campaign", ("https://facebook.com/groups/b",), "run2"))
    blocked = FakeClient(RunStatus("run2", None, "FAILED", 0.02, "Private group is inaccessible"), [])

    assert await ApifyFacebookProvider(empty_store, empty, config()).run("empty") == "succeeded_empty"  # type: ignore[arg-type]
    assert await ApifyFacebookProvider(blocked_store, blocked, config()).run("blocked") == "group_inaccessible"  # type: ignore[arg-type]
    assert empty_store.events[-1][2] == "succeeded_empty"
    assert blocked_store.events[-1][2] == "group_inaccessible"


def test_adapter_maps_currency_missing_to_validation_error() -> None:
    item = {
        "url": "https://facebook.com/groups/a/posts/1",
        "text": "Apartment for EUR 1000",
        "listing": {"dealType": "rent", "priceAmount": 1000},
    }

    with pytest.raises(ValidationError, match="price_currency"):
        apify_item_to_evidence(
            item,
            campaign_id="campaign",
            actor_id="apify/facebook-groups-scraper",
            run_id="run1",
            group_url="https://facebook.com/groups/a",
        )


def test_mapper_marks_multiple_listings_without_combining_them() -> None:
    evidence = apify_item_to_evidence(
        {
            "url": "https://facebook.com/groups/a/posts/1",
            "text": "two apartments",
            "listings": [
                {"propertyType": "apartment", "areaM2": 50},
                {"propertyType": "studio", "areaM2": 20},
            ],
        },
        campaign_id="campaign",
        actor_id="apify/facebook-groups-scraper",
        run_id="run1",
        group_url="https://facebook.com/groups/a",
    )

    assert evidence.listing.property_type == "apartment"
    assert evidence.listing.area_m2 == 50
    assert evidence.evidence["needs_split"] == "true"
