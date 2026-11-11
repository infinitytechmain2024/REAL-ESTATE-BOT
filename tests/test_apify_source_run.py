"""P1.1: the life of one paid Apify run of a social source (``ApifySocialRun``) against a mocked Apify API.

No live calls: every answer comes from ``httpx.MockTransport``. Dataset items are placeholders shaped like
``apify/instagram-comment-scraper`` rows (``id``, ``text``, ``ownerUsername``, ``postUrl``), without real people.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from bot.acquisition.apify_client import ApifyHttp
from bot.acquisition.apify_source import (
    ApifySocialRun,
    MemorySourceRunStore,
    SocialRunConfig,
    SourceRunStore,
    input_hash,
)
from bot.utils import costs

TOKEN = "apify_api_SECRETtoken123"
CAMPAIGN = "00000000-0000-0000-0000-000000000001"
CONFIG = SocialRunConfig(platform="instagram", purpose="comments", actor_id="apify/instagram-comment-scraper",
                         build="0.1.2", max_charge_usd=0.2, max_items=50, page_size=2,
                         build_setting="APIFY_INSTAGRAM_ACTOR_BUILD")
INPUT = {"directUrls": ["https://www.instagram.com/p/C9xYz12AbCd/"], "resultsLimit": 50,
         "includeNestedComments": False}


def comment(n: int, **kw: Any) -> dict[str, Any]:
    return {"id": f"1800000000000000{n}", "text": f"comentario {n}", "ownerUsername": f"user_{n}",
            "postUrl": "https://www.instagram.com/p/C9xYz12AbCd/", **kw}


class Api:
    """A scripted Apify API: the launch answer, run states (one per GET, the last repeats) and the dataset."""

    def __init__(self, *, launch: Any = None, states: list[Any] | None = None, dataset: list[Any] | None = None,
                 usage: Any = 0.05, message: Any = None) -> None:
        self.launch, self.states, self.dataset, self.usage = launch, states or ["SUCCEEDED"], dataset or [], usage
        self.message = message  # the run's statusMessage, when set
        self.calls: list[httpx.Request] = []
        self.abort_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            if isinstance(self.launch, Exception):
                raise self.launch
            if isinstance(self.launch, int):
                return httpx.Response(self.launch, json={"error": {"type": "x"}})
            return httpx.Response(201, json={"data": {"id": "RUN1", "defaultDatasetId": "DS1", "status": "READY"}})
        if path.endswith("/actor-runs/RUN1/abort") and request.method == "POST":
            return httpx.Response(self.abort_status, json={"data": {"id": "RUN1", "status": "ABORTING"}})
        if "/actor-runs/RUN1" in path and request.method == "GET":
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            if isinstance(state, int):
                return httpx.Response(state)
            data = {"id": "RUN1", "status": state, "defaultDatasetId": "DS1"}
            if self.usage is not None:
                data["usageTotalUsd"] = self.usage
            if self.message is not None:
                data["statusMessage"] = self.message
            return httpx.Response(200, json={"data": data})
        if "/datasets/DS1/items" in path:
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            return httpx.Response(200, json=self.dataset[offset:offset + limit])
        return httpx.Response(404)

    def launches(self) -> list[httpx.Request]:
        return [r for r in self.calls if r.method == "POST" and r.url.path.endswith("/runs")]


class Sink:
    def __init__(self) -> None:
        self.pages: list[list[dict[str, Any]]] = []

    async def __call__(self, items: list[dict[str, Any]], run_id: str) -> int:
        assert run_id == "RUN1"
        self.pages.append(items)
        return len(items)

    def items(self) -> list[dict[str, Any]]:
        return [i for page in self.pages for i in page]


def runner(api: Api, store: SourceRunStore, config: SocialRunConfig = CONFIG) -> ApifySocialRun:
    async def no_sleep(_: float) -> None:
        return None

    http = ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(api)), sleep=no_sleep,
                     jitter=lambda: 0, retries=1)
    return ApifySocialRun(store, http, config)


@pytest.fixture
def ledger():
    sink = costs.MemoryLedger()
    costs.install(sink, budget_usd=5)
    try:
        yield sink
    finally:
        costs.install(None)


def row(store: MemorySourceRunStore) -> dict[str, Any]:
    (only,) = store.rows.values()
    return only


def apify_costs(ledger: costs.MemoryLedger) -> list[tuple[str, float, str]]:
    by_index = {index: key for key, index in ledger.keys.items()}
    return [(by_index[i], e.cost_usd, e.code) for i, e in enumerate(ledger.entries) if i in by_index]


async def test_launched_exactly_once_imported_and_billed_once(ledger) -> None:
    api, store, sink = Api(dataset=[comment(n) for n in range(5)]), MemorySourceRunStore(), Sink()
    source = runner(api, store)
    first = await source.run(CAMPAIGN, INPUT, sink, owner_user_id=42)
    again = await source.run(CAMPAIGN, INPUT, sink, owner_user_id=42)
    assert (first.status, first.run_id, first.items_imported, first.cost_usd) == ("succeeded", "RUN1", 5, 0.05)
    assert again.status == "succeeded" and again.items_imported == 5
    assert len(api.launches()) == 1
    assert [len(p) for p in sink.pages] == [2, 2, 1]  # page by page, never a page twice
    assert apify_costs(ledger) == [("apify:RUN1", 0.05, "")]
    saved = row(store)
    assert (saved["status"], saved["run_id"], saved["import_checkpoint"], saved["items_imported"],
            saved["billed_usd"], saved["owner_user_id"]) == ("succeeded", "RUN1", 5, 5, 0.05, 42)
    assert ledger.entries[0].campaign_id == CAMPAIGN


async def test_launch_carries_the_charge_cap_and_the_pinned_build(ledger) -> None:
    api = Api()
    await runner(api, MemorySourceRunStore()).run(CAMPAIGN, INPUT, Sink())
    (launch,) = api.launches()
    params = launch.url.params
    assert params["maxTotalChargeUsd"] == "0.2"
    assert params["build"] == "0.1.2"
    assert params["maxItems"] == "50"
    assert launch.url.path == "/v2/acts/apify~instagram-comment-scraper/runs"
    assert json.loads(launch.content) == INPUT  # the source's input as is: nothing added, no cookie or session


async def test_launch_unknown_never_launches_again_and_books_the_cap(ledger) -> None:
    api, store = Api(launch=httpx.ConnectError("lost")), MemorySourceRunStore()
    source = runner(api, store)
    first = await source.run(CAMPAIGN, INPUT, Sink())
    assert (first.status, first.error, first.cost_usd) == ("failed", "launch_unconfirmed", 0.2)
    api.launch = None  # Apify answers again: still no second paid run for this input
    again = await source.run(CAMPAIGN, INPUT, Sink())
    assert again.status == "failed" and again.error == "launch_unconfirmed"
    assert len(api.launches()) == 1
    saved = row(store)
    assert saved["status"] == "failed" and saved["run_id"].startswith("pending:") and saved["billed_usd"] == 0.2
    ((key, cost, code),) = apify_costs(ledger)
    assert key == f"apify:{saved['run_id']}" and cost == 0.2 and code == "estimated"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_a_fresh_placeholder_is_busy_and_an_old_one_is_closed_at_its_cap(ledger) -> None:
    clock = Clock()
    api, store = Api(), MemorySourceRunStore(clock=clock)
    source = runner(api, store)
    claimed = await store.claim(CAMPAIGN, CONFIG, input_hash(CONFIG.actor_id, INPUT))
    token = await store.mark_launching(claimed)  # another worker's POST in flight, or a crash right after it
    clock.now += CONFIG.launch_grace_seconds - 1
    fresh = await source.run(CAMPAIGN, INPUT, Sink())
    assert fresh.status == "busy"
    assert apify_costs(ledger) == [] and row(store)["status"] == "launching"  # nothing booked, nothing closed
    clock.now += 2  # past the grace: the launch is taken as lost
    outcome = await source.run(CAMPAIGN, INPUT, Sink())
    assert outcome.status == "failed" and outcome.error == "launch_unconfirmed"
    assert api.launches() == []
    assert apify_costs(ledger) == [(f"apify:{token}", 0.2, "estimated")]


def test_grace_below_the_request_timeout_is_refused() -> None:
    from dataclasses import replace

    http = ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(Api())), timeout_seconds=60)
    with pytest.raises(ValueError, match="launch_grace_seconds"):
        ApifySocialRun(MemorySourceRunStore(), http, replace(CONFIG, launch_grace_seconds=30))


def aborts(api: Api) -> list[httpx.Request]:
    return [r for r in api.calls if r.method == "POST" and r.url.path.endswith("/abort")]


@pytest.mark.parametrize("abort_fails", [False, True])
async def test_a_lost_run_id_is_aborted_and_never_launched_twice(ledger, abort_fails) -> None:
    api, store = Api(), MemorySourceRunStore(fail_save_run=True)
    if abort_fails:
        api.abort_status = 500
    source = runner(api, store)
    assert (await source.run(CAMPAIGN, INPUT, Sink())).error == "launch_unconfirmed"
    store.fail_save_run = False
    assert (await source.run(CAMPAIGN, INPUT, Sink())).status == "failed"
    assert len(api.launches()) == 1
    assert [r.url.path for r in aborts(api)] == ["/v2/actor-runs/RUN1/abort"]
    assert [c[1:] for c in apify_costs(ledger)] == [(0.2, "estimated")]


async def test_a_second_tick_resumes_the_same_run_and_bills_it_once(ledger) -> None:
    # The first tick cannot read the run (5xx): it is booked at its cap and left running.
    api, store = Api(states=[503, 503, "SUCCEEDED"], dataset=[comment(1)]), MemorySourceRunStore()
    source = runner(api, store)
    first = await source.run(CAMPAIGN, INPUT, Sink())
    assert first.status == "retry" and first.run_id == "RUN1"
    assert row(store)["status"] == "running"
    assert apify_costs(ledger) == [("apify:RUN1", 0.2, "estimated")]
    second = await source.run(CAMPAIGN, INPUT, Sink())
    third = await source.run(CAMPAIGN, INPUT, Sink())
    assert (second.status, second.items_imported, third.status) == ("succeeded", 1, "succeeded")
    assert len(api.launches()) == 1
    assert apify_costs(ledger) == [("apify:RUN1", 0.05, "")]  # the real usage replaced the estimate, one row


async def test_import_resumes_from_its_checkpoint(ledger) -> None:
    api, store, sink = Api(dataset=[comment(n) for n in range(5)]), MemorySourceRunStore(), Sink()
    claimed = await store.claim(CAMPAIGN, CONFIG, input_hash(CONFIG.actor_id, INPUT))
    await store.mark_launching(claimed)
    await store.save_run(claimed, "RUN1", "DS1")
    await store.checkpoint(claimed, offset=2, items=2)
    outcome = await runner(api, store).run(CAMPAIGN, INPUT, sink)
    assert api.launches() == []
    assert [i["id"] for i in sink.items()] == [comment(n)["id"] for n in (2, 3, 4)]
    assert outcome.items_imported == 5


async def test_over_budget_launches_nothing(ledger) -> None:
    await ledger.add(costs.Entry(CAMPAIGN, "llm", cost_usd=5.0))
    api, store = Api(), MemorySourceRunStore()
    outcome = await runner(api, store).run(CAMPAIGN, INPUT, Sink())
    assert (outcome.status, outcome.error) == ("skipped", "budget_cap")
    assert api.calls == []
    assert row(store)["status"] == "claimed" and row(store)["run_id"] is None  # a later tick may still launch it
    assert (await ledger.summary(CAMPAIGN)).skips == {"api:budget_cap": 1}


async def test_empty_dataset_is_a_successful_run_with_nothing(ledger) -> None:
    api, sink = Api(dataset=[]), Sink()
    outcome = await runner(api, MemorySourceRunStore()).run(CAMPAIGN, INPUT, sink)
    assert (outcome.status, outcome.items_imported, outcome.cost_usd) == ("succeeded", 0, 0.05)
    assert sink.pages == []


@pytest.mark.parametrize(("code", "outcome"), [(402, "failed"), (429, "retry")])
async def test_a_refusal_that_may_pass_drops_the_placeholder_and_is_tried_again(ledger, code, outcome) -> None:
    api, store = Api(launch=code), MemorySourceRunStore()
    source = runner(api, store)
    result = await source.run(CAMPAIGN, INPUT, Sink())
    assert (result.status, result.error) == (outcome, f"apify_http_{code}")
    assert row(store)["run_id"] is None
    assert apify_costs(ledger) == []  # nothing started, nothing paid
    api.launch = None  # nothing was started, so the input may be launched later
    assert (await source.run(CAMPAIGN, INPUT, Sink())).status == "succeeded"
    assert len(api.launches()) == 2


@pytest.mark.parametrize("code", [400, 401, 404, 422])
async def test_a_plain_4xx_fails_for_good(ledger, code) -> None:
    api, store = Api(launch=code), MemorySourceRunStore()
    source = runner(api, store)
    assert (await source.run(CAMPAIGN, INPUT, Sink())).error == f"apify_http_{code}"
    api.launch = None
    again = await source.run(CAMPAIGN, INPUT, Sink())
    assert (again.status, len(api.launches())) == ("failed", 1)  # the second tick POSTs nothing
    assert row(store)["status"] == "failed" and row(store)["error_code"] == f"apify_http_{code}"
    assert row(store)["run_id"] is None and apify_costs(ledger) == []


@pytest.mark.parametrize("code", [402, 429])
async def test_retryable_refusals_stop_after_max_attempts(ledger, code) -> None:
    api, store = Api(launch=code), MemorySourceRunStore()
    source = runner(api, store)
    for _ in range(CONFIG.max_attempts + 2):
        await source.run(CAMPAIGN, INPUT, Sink())
    assert len(api.launches()) == CONFIG.max_attempts
    assert row(store)["attempts"] == CONFIG.max_attempts and row(store)["status"] == "failed"


async def test_failed_run_is_billed_and_closed(ledger) -> None:
    api, store = Api(states=["TIMED-OUT"], usage=0.11), MemorySourceRunStore()
    outcome = await runner(api, store).run(CAMPAIGN, INPUT, Sink())
    assert (outcome.status, outcome.error, outcome.cost_usd) == ("failed", "apify_run_timed_out", 0.11)
    assert row(store)["status"] == "failed" and row(store)["billed_usd"] == 0.11
    assert (await runner(api, store).run(CAMPAIGN, INPUT, Sink())).status == "failed"
    assert len(api.launches()) == 1


@pytest.mark.parametrize("usage", [None, "0.05", -1, True])
async def test_unknown_usage_books_the_cap_as_estimated(ledger, usage) -> None:
    outcome = await runner(Api(usage=usage), MemorySourceRunStore()).run(CAMPAIGN, INPUT, Sink())
    assert outcome.cost_usd == 0.2
    assert apify_costs(ledger) == [("apify:RUN1", 0.2, "estimated")]


async def test_session_keys_never_reach_the_sink(ledger) -> None:
    sink = Sink()
    api = Api(dataset=[comment(1, cookies="x", sessionId="y", owner={"username": "user_1", "authToken": "z"})])
    await runner(api, MemorySourceRunStore()).run(CAMPAIGN, INPUT, sink)
    (item,) = sink.items()
    assert "cookies" not in item and "sessionId" not in item and item["owner"] == {"username": "user_1"}


async def test_another_input_is_its_own_run(ledger) -> None:
    api, store = Api(), MemorySourceRunStore()
    source = runner(api, store)
    await source.run(CAMPAIGN, INPUT, Sink())
    await source.run(CAMPAIGN, {**INPUT, "resultsLimit": 10}, Sink())
    assert len(api.launches()) == 2 and len(store.rows) == 2


async def test_the_token_never_reaches_a_log_line(ledger, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    for api in (Api(dataset=[comment(1)]), Api(launch=httpx.ConnectError("lost")), Api(launch=401),
                Api(states=[500, 500, "FAILED"])):
        source = runner(api, MemorySourceRunStore())
        await source.run(CAMPAIGN, INPUT, Sink())
        assert TOKEN not in repr(source) and TOKEN not in repr(source.http)
    assert caplog.records  # something was logged
    text = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    assert TOKEN not in text and "SECRET" not in text


@pytest.mark.parametrize("kw", [{"build": ""}, {"build": "latest"}, {"build": "<pin me>"}, {"max_charge_usd": 0},
                                {"max_charge_usd": float("inf")}, {"actor_id": "bad actor"}, {"purpose": "Posts!"},
                                {"max_items": 0}, {"page_size": 0}])
def test_config_refuses_unsafe_values(kw) -> None:
    data = {"platform": "tiktok", "purpose": "posts", "actor_id": "clockworks/tiktok-scraper", "build": "1.0.3",
            "max_charge_usd": 0.15, "max_items": 30}
    with pytest.raises(ValueError):
        SocialRunConfig(**{**data, **kw})


def test_input_hash_is_stable_and_key_order_free() -> None:
    a = input_hash("apify/instagram-scraper", {"search": "piso", "resultsLimit": 5})
    assert a == input_hash("apify/instagram-scraper", {"resultsLimit": 5, "search": "piso"})
    assert a != input_hash("apify/instagram-scraper", {"search": "piso", "resultsLimit": 6})
    assert len(a) == 64


# --- P1.3a: a run stopped by its own cap («max charge reached») ------------------------------------------------


async def test_cap_reached_with_no_rows_is_cap_too_low_not_an_empty_success(ledger, caplog) -> None:
    caplog.set_level(logging.INFO)
    api, store, sink = Api(dataset=[], usage=0.0, message="max charge reached"), MemorySourceRunStore(), Sink()
    source = runner(api, store)
    first = await source.run(CAMPAIGN, INPUT, sink)
    assert (first.status, first.error, first.items_imported, first.cost_usd, first.capped) == (
        "failed", "apify_cap_too_low", 0, 0.0, True)
    saved = row(store)
    assert (saved["status"], saved["error_code"], saved["billed_usd"]) == ("failed", "apify_cap_too_low", 0.0)
    assert apify_costs(ledger) == [("apify:RUN1", 0.0, "")]  # the real usageTotalUsd, not the cap
    assert [(e.kind, e.code) for e in ledger.entries if e.kind == "error"] == [("error", "apify_cap_too_low")]
    again = await source.run(CAMPAIGN, INPUT, sink)  # never relaunched with the same cap
    assert (again.status, again.error) == ("failed", "apify_cap_too_low") and len(api.launches()) == 1
    (warning,) = [r for r in caplog.records if r.getMessage() == "apify_source.cap_too_low"]
    assert warning.levelname == "WARNING"
    assert (warning.actor_id, warning.max_charge_usd) == ("apify/instagram-comment-scraper", 0.2)
    assert sink.pages == []


async def test_cap_reached_with_rows_is_a_capped_success(ledger, caplog) -> None:
    caplog.set_level(logging.INFO)
    api = Api(dataset=[comment(n) for n in range(3)], usage=0.2, message="Max charge reached")
    store, sink = MemorySourceRunStore(), Sink()
    outcome = await runner(api, store).run(CAMPAIGN, INPUT, sink)
    assert (outcome.status, outcome.items_imported, outcome.cost_usd, outcome.capped, outcome.error) == (
        "succeeded", 3, 0.2, True, None)
    assert row(store)["status"] == "succeeded" and apify_costs(ledger) == [("apify:RUN1", 0.2, "")]
    (warning,) = [r for r in caplog.records if r.getMessage() == "apify_source.capped"]
    assert (warning.levelname, warning.capped, warning.max_charge_usd, warning.rows) == ("WARNING", True, 0.2, 3)
    (info,) = [r for r in caplog.records if r.getMessage() == "apify_source.imported"]
    assert info.capped is True


async def test_an_empty_run_without_the_cap_message_stays_an_empty_success(ledger) -> None:
    for message in (None, "Finished", 42):
        outcome = await runner(Api(message=message), MemorySourceRunStore()).run(CAMPAIGN, INPUT, Sink())
        assert (outcome.status, outcome.items_imported, outcome.capped) == ("succeeded", 0, False)


async def test_cap_reached_on_a_failed_run_keeps_the_run_status(ledger) -> None:
    outcome = await runner(Api(states=["FAILED"], message="max charge reached"), MemorySourceRunStore()).run(
        CAMPAIGN, INPUT, Sink())
    assert (outcome.status, outcome.error) == ("failed", "apify_run_failed")


async def test_resume_after_rows_were_imported_and_an_empty_last_page_is_a_capped_success(ledger) -> None:
    """Rows came in before a failure; the next call reads only the empty last page: still a capped success."""

    class Flaky(Api):
        failures = 2  # the page at offset 2 answers 500 twice (one retry), then the rest of the dataset (none)

        def __call__(self, request: httpx.Request) -> httpx.Response:
            if "/datasets/DS1/items" in request.url.path and request.url.params["offset"] == "2" and self.failures:
                self.calls.append(request)
                self.failures -= 1
                return httpx.Response(500)
            return super().__call__(request)

    api = Flaky(dataset=[comment(1), comment(2)], usage=0.2, message="max charge reached")
    store, sink = MemorySourceRunStore(), Sink()
    source = runner(api, store)
    first = await source.run(CAMPAIGN, INPUT, sink)
    assert first.status == "retry" and row(store)["status"] == "running"
    assert (row(store)["import_checkpoint"], row(store)["items_imported"]) == (2, 2)
    again = await source.run(CAMPAIGN, INPUT, sink)
    assert (again.status, again.items_imported, again.capped, again.error) == ("succeeded", 2, True, None)
    assert row(store)["status"] == "succeeded" and len(api.launches()) == 1
    assert [len(p) for p in sink.pages] == [2]  # the empty last page reached no sink
    assert apify_costs(ledger) == [("apify:RUN1", 0.2, "")]
