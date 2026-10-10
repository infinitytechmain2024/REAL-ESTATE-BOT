"""Synthetic actor output reflecting public README; no live actor/schema validation."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import httpx
import pytest

from bot.utils import costs
from bot.web_search.queries import QueryTask
from bot.web_search.sources.apify import (
    ApifyIdealistaSource,
    PermanentApifyError,
    SourceRunContext,
    TemporaryApifyError,
    normalize_listing,
    source_run_scope,
)


def task(**kwargs):
    base = QueryTask("terreno", "terreno", "Madrid", {}, "real_estate",
                     constraints={"deal": "sale"}, country_code="ES")
    return replace(base, **kwargs)


def row(**kwargs):
    return {"url": "https://www.idealista.com/inmueble/123456/", "country": "es",
            "municipality": "Madrid", "province": "Madrid", "address": "Calle Uno",
            "propertyType": "land", "operation": "sale", "price": 100000, "size": 2000,
            "description": "Parcela de 2000 m², sin edificación.", **kwargs}


def context(**kwargs):
    return SourceRunContext(campaign_id="campaign", source_name="apify_idealista",
                            run_id=kwargs.get("run_id"), dataset_id=None,
                            max_charge_usd=.05, launch_allowed=kwargs.get("launch_allowed", True),
                            save_run=AsyncMock(), reconcile_usage=AsyncMock())


def source(handler, **kwargs):
    return ApifyIdealistaSource("secret-token", location_id="configured-exact-city-id",
                               client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=AsyncMock(), **kwargs)


@pytest.mark.parametrize("change", [
    {"country_code": "IT"}, {"place_level": "province"}, {"location": "Barcelona"},
    {"vertical": "investors"}, {"constraints": {"deal": "rent"}},
    {"task_text": "apartamento", "goal": "apartamento"},
    {"blocked_hosts": frozenset({"idealista.com"})},
])
def test_supports_exact_configured_land_sale(change):
    adapter = source(lambda _: httpx.Response(200))
    assert adapter.supports(task())
    assert not adapter.supports(task(**change))


def test_no_location_id_no_support_and_repr_no_token():
    adapter = ApifyIdealistaSource("secret-token")
    assert not adapter.supports(task())
    assert "secret" not in repr(adapter)


async def test_settlement_aborts_existing_run_and_never_starts_an_actor():
    checkpoint = context(run_id="run1", launch_allowed=False)
    requests, aborted = [], False

    def handler(request):
        nonlocal aborted
        requests.append(request)
        assert "/acts/" not in request.url.path and "/datasets/" not in request.url.path
        if request.method == "POST":
            assert request.url.path.endswith("/run1/abort")
            aborted = True
        return httpx.Response(200, json={"data": {"id": "run1", "status": "ABORTED" if aborted else "RUNNING",
                                                 "usageTotalUsd": .02}})

    adapter = source(handler)
    await adapter.settle(checkpoint)
    checkpoint.reconcile_usage.assert_awaited_once_with("run1", .02, estimated=False)
    assert [r.method for r in requests] == ["GET", "POST", "GET"]
    await adapter.aclose()


@pytest.mark.parametrize("change", [
    {"url": "https://idealista.com.evil.test/inmueble/123/"},
    {"url": "https://idealista.com/news/123/"},
    {"url": "https://user:pass@idealista.com/inmueble/123/"},
    {"url": "http://127.0.0.1/inmueble/123/"},
    {"operation": "rent"}, {"operation": None}, {"country": "pt"},
    {"propertyType": "countryHouse"}, {"municipality": "Alcobendas"},
])
def test_wrong_rows_are_rejected(change):
    assert normalize_listing(row(**change), task(), municipality="Madrid") is None


@pytest.mark.parametrize("price", [True, -1, float("nan"), float("inf"), "100000", 10**1000])
def test_invalid_price_is_unknown(price):
    result = normalize_listing(row(price=price), task(), municipality="Madrid")
    assert result.price is None and result.currency is None


def test_ambiguous_size_omitted_and_description_preserved():
    result = normalize_listing(row(plot_m2=2000, constructedArea=2000, rooms=2.5), task(), municipality="Madrid")
    assert result.area_m2 is None and result.plot_m2 is None and result.rooms is None
    assert result.description == row()["description"]
    assert result.property_type == "landparcel" and result.deal == "sale"
    assert result.price == 100000 and result.currency == "EUR"


async def test_launch_checkpoint_billing_dataset_bounded():
    requests = []
    checkpoint = context()

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer secret-token"
        assert "secret-token" not in str(request.url)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body == {"country": "es", "locationName": "Madrid", "locationId": "configured-exact-city-id",
                            "propertyType": "lands", "operation": "sale", "maxItems": 2, "numPage": 1}
            assert request.url.params["maxTotalChargeUsd"] == "0.05"
            assert request.url.params["maxItems"] == "2"
            return httpx.Response(201, json={"data": {"id": "run1", "status": "SUCCEEDED",
                                                      "defaultDatasetId": "data1", "usageTotalUsd": .002}})
        if "actor-runs" in request.url.path:
            return httpx.Response(200, json={"data": {"usageTotalUsd": .002}})
        assert checkpoint.save_run.await_count == 2
        assert request.url.params["limit"] == "2"
        return httpx.Response(200, json=[row(), row(), row()])

    adapter = source(handler)
    with source_run_scope(checkpoint):
        results = await adapter.search(task(), limit=2)
    assert len(results) == 2 and len(requests) == 3
    checkpoint.reconcile_usage.assert_awaited_once_with("run1", .002, estimated=False)
    await adapter.aclose()


async def test_no_context_no_paid_request():
    adapter = source(lambda _: pytest.fail("network request without claim"))
    with pytest.raises(PermanentApifyError, match="apify_claim_required"):
        await adapter.search(task(), limit=1)
    with source_run_scope(context(launch_allowed=False)), pytest.raises(PermanentApifyError, match="apify_launch_uncertain"):
        await adapter.search(task(), limit=1)
    await adapter.aclose()


@pytest.mark.parametrize("status", ["FAILED", "TIMED-OUT", "ABORTED", "SUCCEEDED"])
async def test_resume_bills_failed_and_successful_run_without_new_launch(status):
    checkpoint = context(run_id="run1", launch_allowed=False)

    def handler(request):
        assert request.method == "GET"
        if "/actor-runs/" in request.url.path:
            return httpx.Response(200, json={"data": {"id": "run1", "status": status,
                                                      "defaultDatasetId": "data1", "usageTotalUsd": .01}})
        return httpx.Response(200, json=[])

    adapter = source(handler)
    with source_run_scope(checkpoint):
        if status == "SUCCEEDED":
            assert await adapter.search(task(), limit=1) == []
        else:
            with pytest.raises(PermanentApifyError, match="apify_run_"):
                await adapter.search(task(), limit=1)
    checkpoint.reconcile_usage.assert_awaited_once_with("run1", .01, estimated=False)
    await adapter.aclose()


@pytest.mark.parametrize("http_status,error", [(401, PermanentApifyError), (403, PermanentApifyError),
                                               (429, TemporaryApifyError), (500, TemporaryApifyError)])
async def test_safe_http_codes_unknown_launch_conservatively_estimated(http_status, error):
    checkpoint = context()
    adapter = source(lambda _: httpx.Response(http_status, text="secret-token provider payload"))
    with source_run_scope(checkpoint), pytest.raises(error) as caught:
        await adapter.search(task(), limit=1)
    assert caught.value.code == f"apify_http_{http_status}"
    assert "secret" not in str(caught.value) and caught.value.__cause__ is None
    if http_status in {401, 403, 429}:
        checkpoint.reconcile_usage.assert_not_awaited()
    else:
        checkpoint.reconcile_usage.assert_awaited_once_with("", .05, estimated=True)
    await adapter.aclose()


async def test_transport_unknown_launch_no_token_chain():
    checkpoint = context()

    def handler(request):
        raise httpx.ReadTimeout("secret-token", request=request)

    adapter = source(handler)
    with source_run_scope(checkpoint), pytest.raises(TemporaryApifyError, match="apify_transport") as caught:
        await adapter.search(task(), limit=1)
    assert caught.value.__suppress_context__
    checkpoint.reconcile_usage.assert_awaited_once_with("", .05, estimated=True)
    await adapter.aclose()


async def test_timeout_aborts_and_reconciles_same_run():
    requests = []
    checkpoint = context(run_id="run1", launch_allowed=False)

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/abort"):
            return httpx.Response(200, json={"data": {"status": "ABORTED", "usageTotalUsd": .01}})
        return httpx.Response(200, json={"data": {"id": "run1", "status": "RUNNING", "usageTotalUsd": .01}})

    adapter = source(handler, timeout_seconds=.01)
    with source_run_scope(checkpoint), pytest.raises(TemporaryApifyError, match="apify_wait_timeout"):
        await adapter.search(task(), limit=1)
    assert any(r.method == "POST" and r.url.path.endswith("/run1/abort") for r in requests)
    checkpoint.reconcile_usage.assert_awaited_once_with("run1", .01, estimated=False)
    await adapter.aclose()


async def test_missing_billing_conservative_explicit_estimate():
    checkpoint = context(run_id="run1", launch_allowed=False)
    adapter = source(lambda _: httpx.Response(200, json={"data": {"status": "FAILED"}}))
    with source_run_scope(checkpoint), pytest.raises(PermanentApifyError):
        await adapter.search(task(), limit=1)
    checkpoint.reconcile_usage.assert_awaited_once_with("run1", .05, estimated=True)
    await adapter.aclose()


async def test_checkpoint_before_poll_and_exact_run_resume():
    checkpoint = context()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(201, json={"data": {"id": "run1", "status": "RUNNING",
                                                      "defaultDatasetId": "data1"}})
        checkpoint.save_run.assert_awaited()
        if "actor-runs" in request.url.path:
            return httpx.Response(200, json={"data": {"id": "run1", "status": "SUCCEEDED",
                                                      "defaultDatasetId": "data1", "usageTotalUsd": .001}})
        return httpx.Response(200, json=[row()])

    adapter = source(handler)
    with source_run_scope(checkpoint):
        assert len(await adapter.search(task(), limit=1)) == 1
    assert paths == ["/v2/acts/axlymxp~idealista-scraper/runs", "/v2/actor-runs/run1", "/v2/actor-runs/run1", "/v2/datasets/data1/items"]
    await adapter.aclose()


async def test_poll_transport_failure_estimates_known_run_without_new_launch():
    checkpoint = context(run_id="run1", launch_allowed=False)

    def handler(request):
        assert request.method == "GET"
        raise httpx.ReadTimeout("secret-token", request=request)

    adapter = source(handler)
    with source_run_scope(checkpoint), pytest.raises(TemporaryApifyError):
        await adapter.search(task(), limit=1)
    checkpoint.reconcile_usage.assert_awaited_once_with("run1", .05, estimated=True)
    await adapter.aclose()


async def test_dataset_invalid_schema_after_billing():
    checkpoint = context(run_id="run1", launch_allowed=False)

    def handler(request):
        if "actor-runs" in request.url.path:
            return httpx.Response(200, json={"data": {"status": "SUCCEEDED", "defaultDatasetId": "data1", "usageTotalUsd": .01}})
        return httpx.Response(200, json={"malformed": True})

    adapter = source(handler)
    with source_run_scope(checkpoint), pytest.raises(PermanentApifyError, match="apify_dataset_schema"):
        await adapter.search(task(), limit=1)
    checkpoint.reconcile_usage.assert_awaited_once_with("run1", .01, estimated=False)
    await adapter.aclose()


async def test_budget_stops_launch(monkeypatch):
    monkeypatch.setattr("bot.web_search.sources.apify.costs.over_budget", AsyncMock(return_value=True))
    adapter = source(lambda _: pytest.fail("launch over budget"))
    with source_run_scope(context()), pytest.raises(PermanentApifyError, match="apify_budget_cap"):
        await adapter.search(task(), limit=1)
    await adapter.aclose()


def test_bounded_facts_zero_price_and_implausible_rooms():
    result = normalize_listing(row(price=0, rooms=100, title="x" * 1000,
                                    address="y" * 1000, description="z" * 2000), task(), municipality="Madrid")
    assert result.price is None and result.rooms is None
    assert len(result.title) == 300 and len(result.address) == 300 and len(result.description) == 1500


async def test_stream_size_cap():
    checkpoint = context()
    adapter = source(lambda _: httpx.Response(201, content=b"x" * 100), max_response_bytes=50)
    with source_run_scope(checkpoint), pytest.raises(PermanentApifyError, match="apify_response_too_large"):
        await adapter.search(task(), limit=1)
    checkpoint.reconcile_usage.assert_awaited_once_with("", .05, estimated=True)
    await adapter.aclose()


async def test_failed_checkpoint_aborts_known_run_and_safe_error():
    checkpoint = context()
    checkpoint.save_run.side_effect = RuntimeError("secret database credentials")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={"data": {"id": "run1", "status": "ABORTED", "usageTotalUsd": .01}})

    adapter = source(handler)
    with source_run_scope(checkpoint), pytest.raises(PermanentApifyError, match="apify_checkpoint_failed") as caught:
        await adapter.search(task(), limit=1)
    assert caught.value.__suppress_context__
    assert requests[1].url.path.endswith("/run1/abort")
    checkpoint.reconcile_usage.assert_awaited_once_with("run1", .01, estimated=False)
    await adapter.aclose()


async def test_settled_billing_supersedes_completion_payload():
    checkpoint = context(run_id="run1", launch_allowed=False)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(200, json={"data": {"status": "FAILED", "usageTotalUsd": .001 if count == 1 else .015}})

    adapter = source(handler)
    with source_run_scope(checkpoint), pytest.raises(PermanentApifyError, match="apify_run_failed"):
        await adapter.search(task(), limit=1)
    checkpoint.reconcile_usage.assert_awaited_once_with("run1", .015, estimated=False)
    adapter._sleep.assert_awaited_once_with(10)
    await adapter.aclose()


async def test_billing_reread_failed_keeps_known_run_resumable():
    checkpoint = context(run_id="run1", launch_allowed=False)
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return httpx.Response(200, json={"data": {"status": "SUCCEEDED", "usageTotalUsd": .001}})
        return httpx.Response(500)

    adapter = source(handler)
    with source_run_scope(checkpoint), pytest.raises(TemporaryApifyError, match="apify_billing_pending"):
        await adapter.search(task(), limit=1)
    checkpoint.reconcile_usage.assert_awaited_once_with("run1", .05, estimated=True)
    await adapter.aclose()


async def test_filtered_rows_and_provider_overflow_record_skip_counts(monkeypatch):
    ledger = costs.MemoryLedger()
    monkeypatch.setitem(costs._state, "ledger", ledger)
    monkeypatch.setitem(costs._state, "budget", 0.0)
    checkpoint = context(run_id="run1", launch_allowed=False)

    def handler(request):
        if "actor-runs" in request.url.path:
            return httpx.Response(200, json={"data": {"status": "SUCCEEDED", "defaultDatasetId": "data1", "usageTotalUsd": .01}})
        return httpx.Response(200, json=[row(), row(country="pt"), {}, row(), row()])

    adapter = source(handler)
    with source_run_scope(checkpoint):
        result = await adapter.search(task(), limit=3)
    assert len(result) == 1
    assert [(entry.stage, entry.kind, entry.code, entry.units, entry.campaign_id)
            for entry in ledger.entries] == [
                ("api", "skip", "apify_result_limit", 2, "campaign"),
                ("api", "skip", "apify_invalid_listing", 2, "campaign"),
            ]
    await adapter.aclose()
