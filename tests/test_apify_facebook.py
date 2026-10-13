"""T3: the Apify Facebook adapter against a mocked Apify API (no live calls).

The dataset items below follow the output schema of ``apify/facebook-groups-scraper`` (``url``, ``text``, ``time``,
``facebookUrl``, ``inputUrl``), not a live run.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from bot.acquisition.apify_client import (
    ApifyHttp,
    LaunchUnknown,
    PermanentApifyError,
    TemporaryApifyError,
)
from bot.facebook_collector.apify import (
    ApifyFacebookConfig,
    ApifyFacebookProvider,
    ApifyRun,
    apify_item_to_post,
)
from bot.facebook_collector.apify.provider import LAUNCH_UNKNOWN
from bot.facebook_collector.models import BatchItem, BatchPlan, GroupState
from bot.utils import costs

TOKEN = "apify_api_SECRETtoken123"
G1, G2 = "https://www.facebook.com/groups/pisosmadrid", "https://www.facebook.com/groups/terrenosmadrid"
PLAN = BatchPlan("batch-1", "", "", "", 20, (BatchItem("item-1", "src-1", G1, 1), BatchItem("item-2", "src-2", G2, 2)))
CONFIG = ApifyFacebookConfig(build="1.2.3", page_size=2, checkpoint_every=1, max_usd_per_run=1.0)


def item(n: int, group: str = G1, **kw: Any) -> dict[str, Any]:
    return {"url": f"{group}/posts/{1000000 + n}/", "text": f"Vendo parcela {n} de 2.000 m² en Boadilla, 300.000 €",
            "time": "2026-10-12T10:00:00.000Z", "facebookUrl": group, **kw}


class Store:
    def __init__(self, run: ApifyRun | None = None) -> None:
        self.run, self.raw, self.posts, self.items, self.batch = run, {}, [], {}, None
        self.updates: list[dict[str, Any]] = []
        self.spent_usd = 0.0

    async def load_plan(self, batch_id):
        return PLAN

    async def campaign_of(self, batch_id):
        return "c1"

    async def start(self, plan):
        return "batch-run"

    async def start_item(self, item, batch_run_id):
        return f"run-{item.id}"

    async def get_run(self, batch_id):
        return self.run

    async def save_run(self, batch_id, campaign_id, *, actor_id, build, run_id, status, input_hash, dataset_id=None):
        self.run = ApifyRun(batch_id, run_id, status, dataset_id)

    async def update_run(self, run_id, **fields):
        self.updates.append(fields)

    async def spent(self, campaign_id):
        return self.spent_usd

    async def save_raw(self, run_id, index, payload):
        if (run_id, index) in self.raw:
            return False
        self.raw[(run_id, index)] = payload
        return True

    async def save_post(self, source_id, acquisition_run_id, row):
        self.posts.append((source_id, row))

    async def finish_item(self, item, run_id, state, group_state, detail=None):
        self.items[item.id] = (state, group_state, detail)

    async def finish_batch(self, plan, batch_run_id, state, reason=None):
        self.batch = (state, reason)

    async def queue_browser_fallback(self, plan, items, campaign_id):
        self.fallbacks = [*getattr(self, "fallbacks", []), [i.id for i in items]]
        return "fallback-batch"


class Api:
    """A scripted Apify API: launch answers, run states and dataset pages; every request is recorded."""

    def __init__(self, *, launch: Any = None, status: str = "SUCCEEDED", dataset: list[Any] | None = None,
                 usage: float | None = 0.4) -> None:
        self.launch, self.status, self.dataset, self.usage = launch, status, dataset or [], usage
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            if isinstance(self.launch, Exception):
                raise self.launch
            if isinstance(self.launch, int):
                return httpx.Response(self.launch, json={"error": "x"})
            return httpx.Response(201, json={"data": {"id": "RUN1", "defaultDatasetId": "DS1", "status": "READY"}})
        if "/actor-runs/" in path:
            return httpx.Response(200, json={"data": {"id": "RUN1", "status": self.status, "defaultDatasetId": "DS1",
                                                      "usageTotalUsd": self.usage}})
        if "/datasets/DS1/items" in path:
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            return httpx.Response(200, json=self.dataset[offset:offset + limit])
        return httpx.Response(404)

    def launches(self) -> int:
        return sum(1 for r in self.calls if r.method == "POST" and r.url.path.endswith("/runs"))


def provider(api: Api, store: Store, config: ApifyFacebookConfig = CONFIG) -> ApifyFacebookProvider:
    async def no_sleep(_: float) -> None:
        return None

    http = ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(api)), sleep=no_sleep, jitter=lambda: 0)
    return ApifyFacebookProvider(store, http, config)


@pytest.fixture
def ledger():
    sink = costs.MemoryLedger()
    costs.install(sink, budget_usd=5)
    try:
        yield sink
    finally:
        costs.install(None)


async def test_adapter_imports_a_run_into_collected_posts(ledger) -> None:
    store, api = Store(), Api(dataset=[item(1), item(2), item(3, G2)])
    assert await provider(api, store).run("batch-1") == "succeeded"
    assert [s for s, _ in store.posts] == ["src-1", "src-1", "src-2"]
    row = store.posts[0][1]
    assert row["platform_post_id"] == "1000001" and row["raw_payload"]["run_id"] == "RUN1"
    assert row["raw_payload"]["provider"] == "apify" and row["state"] == "normalised"
    assert store.items == {"item-1": ("succeeded", GroupState.ACTIVE, None), "item-2": ("succeeded", GroupState.ACTIVE, None)}
    assert store.batch == ("succeeded", None) and store.run.run_id == "RUN1"
    launch = next(r for r in api.calls if r.method == "POST")
    assert launch.url.params["build"] == "1.2.3" and launch.url.params["maxTotalChargeUsd"] == "1.0"
    assert json.loads(launch.content)["startUrls"] == [{"url": G1}, {"url": G2}]
    summary = await ledger.summary("c1")
    assert summary.by_stage == {"api": pytest.approx(0.4)}


async def test_posts_match_their_group_by_the_start_url(ledger) -> None:
    # the actor's output schema: ``facebookUrl`` may carry the numeric group id, ``inputUrl`` is the URL we sent
    by_id = "https://www.facebook.com/groups/874728723021553"
    store = Store()
    api = Api(dataset=[item(1, facebookUrl=by_id, inputUrl=G1), item(2, G2, facebookUrl=by_id, inputUrl=G2 + "/")])
    assert await provider(api, store).run("batch-1") == "succeeded"
    assert [s for s, _ in store.posts] == ["src-1", "src-2"]


async def test_adapter_does_not_retry_on_launch_unknown(ledger) -> None:
    store, api = Store(), Api(launch=httpx.ConnectError("reset"))
    p = provider(api, store)
    assert await p.run("batch-1") == "failed"
    assert api.launches() == 1 and store.run.status == LAUNCH_UNKNOWN
    assert store.batch == ("failed", "apify_launch_unknown")
    assert await p.run("batch-1") == "failed" and api.launches() == 1  # the same batch never launches again
    summary = await ledger.summary("c1")
    assert summary.by_stage == {"api": pytest.approx(1.0)} and summary.errors == {"api:apify_launch_unknown": 1}
    with pytest.raises(LaunchUnknown):  # a 5xx on the launch is just as uncertain
        await provider(Api(launch=503), Store()).http.launch("apify/facebook-groups-scraper", {}, max_items=1,
                                                              max_charge_usd=1, timeout_seconds=60)


async def test_adapter_resumes_from_checkpoint(ledger) -> None:
    dataset = [item(n) for n in range(1, 6)]
    store = Store(ApifyRun("batch-1", "RUN1", "RUNNING", "DS1", import_checkpoint=3, items_imported=3))
    api = Api(dataset=dataset)
    assert await provider(api, store).run("batch-1") == "succeeded"
    assert api.launches() == 0  # a known run is resumed, never launched again
    assert [row["platform_post_id"] for _, row in store.posts] == ["1000004", "1000005"]
    first_page = next(r for r in api.calls if "/datasets/" in r.url.path)
    assert first_page.url.params["offset"] == "3"
    assert store.updates[-1]["import_checkpoint"] == 5 and store.updates[-1]["items_imported"] == 5
    await provider(api, store).run("batch-1")
    assert (await ledger.summary("c1")).by_stage == {"api": pytest.approx(0.4)}  # one run, booked once


async def test_adapter_distinguishes_empty_run_from_inaccessible_group(ledger) -> None:
    store = Store()
    api = Api(dataset=[{"facebookUrl": G2, "error": "This group is private. Join the group to see posts."}])
    assert await provider(api, store).run("batch-1") == "succeeded"
    assert store.items["item-1"] == ("succeeded", GroupState.ACTIVE, "no_posts")      # reachable, nothing new
    assert store.items["item-2"] == ("failed", GroupState.INACCESSIBLE, "group_private")  # closed: not «empty»
    failed = Store()
    assert await provider(Api(status="FAILED"), failed).run("batch-1") == "failed"
    assert failed.batch == ("failed", "apify_run_failed") and failed.items["item-1"][1] is GroupState.UNKNOWN


def test_adapter_maps_currency_missing_to_validation_error() -> None:
    with pytest.raises(ValidationError, match="price_amount without price_currency"):
        apify_item_to_post(item(1, price={"amount": 300000}), campaign_id="c1", actor_id="a/b", run_id="R", group_url=G1)
    post = apify_item_to_post(item(1, price={"amount": 300000, "currency": "eur"}), campaign_id="c1", actor_id="a/b",
                              run_id="R", group_url=G1)
    assert post.provider_facts.price_currency == "EUR"
    plain = apify_item_to_post(item(1), campaign_id="c1", actor_id="a/b", run_id="R", group_url=G1)
    assert plain.provider_facts.price_amount is None  # the 300.000 € in the text is not guessed into a fact
    two = apify_item_to_post(item(1, text="Piso 1: 200.000 €. Piso 2: 250.000 €."), campaign_id="c1", actor_id="a/b",
                             run_id="R", group_url=G1)
    assert two.quotes == {"needs_split": "true"}


async def test_invalid_items_are_counted_not_lost(ledger) -> None:
    store = Store()
    api = Api(dataset=[item(1), {"facebookUrl": G1, "text": "sin enlace"}, item(2, price={"amount": 5})])
    await provider(api, store).run("batch-1")
    assert len(store.posts) == 1
    assert (await ledger.summary("c1")).skips == {"api:apify_invalid_item": 2}


async def test_budgets_stop_a_launch(ledger) -> None:
    store, api = Store(), Api()
    store.spent_usd = 9.5  # + a 1.0 run cap would pass the provider's 10 USD per campaign
    assert await provider(api, store).run("batch-1") == "failed"
    assert api.launches() == 0 and store.batch == ("failed", "apify_budget_cap")
    await costs.record("llm", cost_usd=6, campaign_id="c1")  # CAMPAIGN_BUDGET_USD=5 spent
    over = Store()
    assert await provider(api, over).run("batch-1") == "failed" and over.batch == ("failed", "budget_cap")


async def test_reads_retry_with_backoff_and_respect_retry_after() -> None:
    answers = [httpx.Response(503), httpx.ConnectError("x"), httpx.Response(429, headers={"Retry-After": "7"}),
               httpx.Response(200, json={"data": {"id": "R", "status": "SUCCEEDED"}})]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    http = ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), sleep=sleep, jitter=lambda: 0)
    assert (await http.run("R"))["status"] == "SUCCEEDED"
    assert sleeps == [2, 4, 7]
    always = ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502))),
                       sleep=sleep, jitter=lambda: 0)
    with pytest.raises(TemporaryApifyError, match="apify_http_502"):
        await always.run("R")
    refused = ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(401))),
                        sleep=sleep)
    with pytest.raises(PermanentApifyError, match="apify_http_401"):
        await refused.run("R")


async def test_no_token_cookie_or_session_in_logs_or_errors(ledger, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        await provider(Api(launch=httpx.ConnectError(f"boom {TOKEN}")), Store()).run("batch-1")
        await provider(Api(dataset=[item(1)]), Store()).run("batch-1")
        await provider(Api(launch=401), Store()).run("batch-1")
    text = caplog.text + repr(provider(Api(), Store())) + repr(provider(Api(), Store()).http)
    assert TOKEN not in text and "cookie" not in text.lower() and "session" not in text.lower()
    assert all("Bearer" not in str(e) for e in (await ledger.summary("c1")).errors)


def test_the_config_requires_a_pinned_build() -> None:
    with pytest.raises(ValueError, match="pinned build"):
        ApifyFacebookConfig()
    with pytest.raises(ValueError, match="pinned build"):
        ApifyFacebookConfig(build="latest")
    with pytest.raises(ValueError, match="unsafe"):
        ApifyFacebookConfig(build="1.0", max_usd_per_run=20, max_usd_per_campaign=10)


async def test_closed_groups_go_to_the_browser_collector_once(ledger) -> None:
    store = Store()
    api = Api(dataset=[item(1), {"facebookUrl": G2, "error": "This group is private. Join the group to see posts."}])
    await provider(api, store).run("batch-1")
    assert store.fallbacks == [["item-2"]]  # only the closed group, not the one Apify read
    gone, quiet = Store(), Store()
    await provider(Api(dataset=[{"facebookUrl": G2, "error": "Page not found"}]), gone).run("batch-1")
    assert not hasattr(gone, "fallbacks")  # a removed group is not retried in the browser
    off = ApifyFacebookConfig(build="1.2.3", browser_fallback=False)
    await provider(Api(dataset=[{"facebookUrl": G2, "error": "private group"}]), quiet, off).run("batch-1")
    assert not hasattr(quiet, "fallbacks")
