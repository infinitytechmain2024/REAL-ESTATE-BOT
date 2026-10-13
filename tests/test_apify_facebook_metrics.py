"""T7: the Apify adapter's metrics (structured log events) on mocked runs, and no secret in any log line."""

from __future__ import annotations

import logging

import httpx
import pytest

from bot.facebook_collector.apify.metrics import METRICS, emit
from bot.facebook_collector.apify.provider import ApifyRun
from bot.utils import costs
from tests.test_apify_facebook import G1, G2, TOKEN, Api, Store, item, provider


@pytest.fixture
def ledger():
    sink = costs.MemoryLedger()
    costs.install(sink, budget_usd=5)
    try:
        yield sink
    finally:
        costs.install(None)


def metrics(caplog: pytest.LogCaptureFixture) -> dict[str, list[logging.LogRecord]]:
    out: dict[str, list[logging.LogRecord]] = {}
    for record in caplog.records:
        if getattr(record, "metric", None):
            out.setdefault(record.metric, []).append(record)
    return out


async def test_a_mocked_run_emits_every_metric_with_its_correlation_ids(ledger, caplog: pytest.LogCaptureFixture) -> None:
    dataset = [item(1), item(2), {"facebookUrl": G2, "error": "This group is private"}, {"facebookUrl": G1, "text": "x"}]
    with caplog.at_level(logging.INFO):
        await provider(Api(dataset=dataset, usage=0.25), Store()).run("batch-1")
    seen = metrics(caplog)
    assert set(seen) == METRICS - {"apify_run_failed_total", "apify_checkpoint_resumed_total"}
    assert seen["apify_items_imported_total"][0].value == 2
    assert seen["apify_billed_usd_total"][0].value == 0.25
    assert seen["apify_import_duration_seconds"][0].value >= 0
    rejected = {(r.reason, r.value) for r in seen["apify_items_rejected_total"]}
    assert rejected == {("group_private", 1), ("apify_invalid_item", 1)}
    private = next(r for r in seen["apify_items_rejected_total"] if r.reason == "group_private")
    assert private.source_url == G2
    for records in seen.values():
        for r in records:
            assert r.campaign_id == "c1" and r.actor_id == "apify/facebook-groups-scraper" and r.run_id == "RUN1"
    launched = seen["apify_run_launched_total"][0]
    assert launched.batch_id == "batch-1" and "metric apify_run_launched_total=1.0" in launched.getMessage()


async def test_failures_and_resumes_are_counted_with_their_reason(ledger, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        await provider(Api(launch=httpx.ConnectError("x")), Store()).run("batch-1")
        await provider(Api(status="TIMED-OUT"), Store()).run("batch-1")
        await provider(Api(launch=403), Store()).run("batch-1")
        resumed = Store(ApifyRun("batch-1", "RUN1", "RUNNING", "DS1", import_checkpoint=1, items_imported=1))
        await provider(Api(dataset=[item(1), item(2)]), resumed).run("batch-1")
    seen = metrics(caplog)
    assert [r.reason for r in seen["apify_run_failed_total"]] == [
        "apify_launch_unknown", "apify_run_timed_out", "apify_http_403"]
    assert len(seen["apify_checkpoint_resumed_total"]) == 1


async def test_no_secret_reaches_any_log_line(ledger, caplog: pytest.LogCaptureFixture) -> None:
    """grep-style: the token, a Bearer header, cookies or session data never appear in what was logged."""
    leaky = Api(launch=httpx.ConnectError(f"Authorization: Bearer {TOKEN}"))
    with caplog.at_level(logging.DEBUG):
        await provider(leaky, Store()).run("batch-1")
        await provider(Api(dataset=[item(1, cookies="c_user=1; xs=secret")]), Store()).run("batch-1")
        await provider(Api(launch=500), Store()).run("batch-1")
    lines = "\n".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    for secret in (TOKEN, "Bearer", "c_user", "xs=secret"):
        assert secret not in lines


async def test_session_fields_of_a_raw_item_are_never_stored(ledger) -> None:
    from bot.facebook_collector.apify.provider import scrub

    store = Store()
    await provider(Api(dataset=[item(1, cookies="c_user=1", user={"name": "Ana", "sessionId": "s", "authToken": "t"})]),
                   store).run("batch-1")
    raw = next(iter(store.raw.values()))
    assert "cookies" not in raw and raw["user"] == {"name": "Ana"} and raw["text"].startswith("Vendo")
    assert scrub([{"Set-Cookie": "x", "ok": 1}]) == [{"ok": 1}]


def test_unknown_metrics_and_labels_are_refused() -> None:
    with pytest.raises(ValueError):
        emit("apify_made_up_total")
    with pytest.raises(ValueError):
        emit("apify_run_launched_total", token="x")
