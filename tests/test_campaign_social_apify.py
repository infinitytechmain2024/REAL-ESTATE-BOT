"""P1.8: Instagram / TikTok posts and LinkedIn people through Apify wired into the campaign (mocks only).

The Apify API is ``httpx.MockTransport``; the stores are the in-memory twins. SYNTHETIC rows: field names from the
actors' output schemas (``tests/test_apify_social.py``, ``tests/test_apify_linkedin.py``), values invented.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from bot.acquisition.apify_client import ApifyHttp
from bot.acquisition.apify_instagram import ApifyInstagramPosts
from bot.acquisition.apify_source import MemorySourceRunStore
from bot.acquisition.apify_tiktok import ApifyTikTokPosts
from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign import runner as runner_module
from bot.campaign.final_report import FinalReporter
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore, SocialActivity, SourceCount
from bot.campaign.social_apify import (
    ApifySocialWorker,
    ApifyTasks,
    MemoryApifySocialStore,
    deal_targets,
)
from bot.campaign.summary import summary_text
from bot.campaign.tolerance import Request
from bot.social_search.queries import QueryPlanner
from bot.utils import costs
from tests.test_apify_linkedin import settings as runner_settings
from tests.test_apify_social import IG, TT, TT_VIDEO, ig_post_row
from tests.test_campaign_runner import Clock, FakeMessenger
from tests.test_social_search import FakeDiscovery

TOKEN = "apify_api_SECRETtoken123"
LAND = "Ищу участок под строительство дома в Марбелье до 300000 евро"


class Api:
    """Every launch starts a run of its own (RUN1, RUN2 ...), SUCCEEDED, whose dataset is ``rows(n)``.

    ``gate``: launches wait for it (a POST in flight); ``launches``: statuses in order (the last repeats)."""

    def __init__(self, rows: Any = None, *, statuses: list[int] | None = None, gate: asyncio.Event | None = None,
                 run_status: str = "SUCCEEDED") -> None:
        self.rows = rows or (lambda n: [])
        self.statuses, self.gate, self.run_status = list(statuses or [201]), gate, run_status
        self.calls: list[httpx.Request] = []
        self.posts = 0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        await asyncio.sleep(0)  # a real request yields to the loop: a run polled as RUNNING must not starve it
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if self.gate is not None:
                await self.gate.wait()
            if status != 201:
                return httpx.Response(status, json={"error": {"type": "rate-limit-exceeded"}})
            self.posts += 1
            n = self.posts
            return httpx.Response(201, json={"data": {"id": f"RUN{n}", "defaultDatasetId": f"DS{n}", "status": "READY"}})
        if "/actor-runs/RUN" in path:
            n = int(path.rsplit("RUN", 1)[1].split("/")[0])
            return httpx.Response(200, json={"data": {"id": f"RUN{n}", "status": self.run_status,
                                                      "defaultDatasetId": f"DS{n}", "usageTotalUsd": 0.04}})
        if "/datasets/DS" in path:
            n = int(path.rsplit("DS", 1)[1].split("/")[0])
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            return httpx.Response(200, json=self.rows(n)[offset:offset + limit])
        return httpx.Response(404)

    def launches(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.calls if r.method == "POST" and r.url.path.endswith("/runs")]


def apify_http(api: Api) -> ApifyHttp:
    async def no_sleep(_: float) -> None:
        return None

    return ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(api)), sleep=no_sleep,
                     jitter=lambda: 0, retries=1)


def ig_rows(n: int) -> list[dict[str, Any]]:
    return [ig_post_row(url=f"https://www.instagram.com/p/C1aBcDeFg{n:02d}/")]


def tt_rows(n: int) -> list[dict[str, Any]]:
    return [{"webVideoUrl": TT_VIDEO.replace("0001", f"{n:04d}"), "text": f"Parcela en Marbella {n}",
             "createTimeISO": "2026-10-20T09:30:00.000Z",
             "authorMeta": {"name": "parcelas_demo", "nickName": "Parcelas Demo",
                            "profileUrl": "https://www.tiktok.com/@parcelas_demo"}}]


def rows(n: int) -> list[dict[str, Any]]:
    return ig_rows(n) + tt_rows(n)


@pytest.fixture
def ledger():
    sink = costs.MemoryLedger()
    costs.install(sink, budget_usd=5)
    try:
        yield sink
    finally:
        costs.install(None)


async def world(api: Api, *, platforms: tuple[str, ...] = ("instagram", "tiktok"), max_targets: int = 3,
                budget_usd: float = 5.0):
    campaigns = MemoryCampaignStore()
    store = MemoryApifySocialStore()
    tasks = ApifyTasks(apify_http(api))
    runs = MemorySourceRunStore()
    built = {"instagram": lambda: ApifyInstagramPosts(runs, tasks.http, IG, sleep=tasks.sleep, poll_seconds=0),
             "tiktok": lambda: ApifyTikTokPosts(runs, tasks.http, TT, sleep=tasks.sleep, poll_seconds=0)}
    worker = ApifySocialWorker(store, campaigns, {p: built[p]() for p in platforms}, QueryPlanner(None), tasks,
                               max_targets=max_targets, budget_usd=budget_usd)
    return campaigns, store, tasks, runs, worker


async def new_campaign(campaigns: MemoryCampaignStore, store: MemoryApifySocialStore, text: str = LAND) -> str:
    cid = await campaigns.create(plan_campaign(text), chat_id=-1, requested_by=7, source_text=text, actor="t")
    store.campaigns[cid] = "planned"
    return cid


# --- the wiring: which worker the runner builds -----------------------------------------------------------------


IG_ENV = {"APIFY_TOKEN": TOKEN, "APIFY_INSTAGRAM_ENABLED": "true", "APIFY_INSTAGRAM_ACTOR_BUILD": "0.3.1"}


async def test_apify_mode_with_a_flag_builds_the_apify_worker_and_never_the_browser_one() -> None:
    settings = runner_settings(ACQUISITION_MODE="apify", BROWSER_SESSION_API_TOKEN="b", **IG_ENV)
    assert settings.apify_social_max_targets == 3 and settings.apify_social_on()
    assert runner_module._social_worker(settings, None, MemoryCampaignStore()) == (None, None)
    tasks = runner_module._apify_tasks(settings)
    worker, generator = runner_module._apify_social_worker(settings, None, MemoryCampaignStore(), tasks)
    try:
        assert worker is not None and worker.platforms == ("instagram",) and generator is None
        assert worker.max_targets == 3 and worker.budget_usd == settings.budget_usd
    finally:
        await tasks.http.aclose()


@pytest.mark.parametrize("env", [{"ACQUISITION_MODE": "legacy", **IG_ENV}, {"ACQUISITION_MODE": "apify"}])
async def test_legacy_or_no_flag_changes_nothing(env: dict[str, str]) -> None:
    settings = runner_settings(BROWSER_SESSION_API_TOKEN="b", **env)
    assert not settings.apify_social_on() and runner_module._apify_tasks(settings) is None
    assert runner_module._apify_social_worker(settings, None, MemoryCampaignStore(), None) == (None, None)
    if env["ACQUISITION_MODE"] == "legacy":  # the browser social search is built as before
        worker, generator = runner_module._social_worker(settings, None, MemoryCampaignStore())
        assert worker is not None and generator is None


def test_max_targets_is_bounded() -> None:
    for bad in ("0", "11"):
        with pytest.raises(ValueError):
            runner_settings(APIFY_SOCIAL_MAX_TARGETS=bad)
    assert runner_settings(APIFY_SOCIAL_MAX_TARGETS="10").apify_social_max_targets == 10


def test_targets_are_dealt_in_turn_and_never_above_the_budget() -> None:
    caps = {"instagram": 0.20, "tiktok": 0.15}
    assert deal_targets(("instagram", "tiktok"), caps, 3, 0) == {"instagram": 2, "tiktok": 1}
    assert deal_targets(("tiktok",), caps, 3, 5) == {"tiktok": 3}
    assert deal_targets(("instagram", "tiktok"), caps, 3, 0.36) == {"instagram": 1, "tiktok": 1}
    assert deal_targets(("instagram",), caps, 3, 0.1) == {"instagram": 0}


# --- the Apify social worker ------------------------------------------------------------------------------------


async def test_targets_run_once_per_campaign_one_per_launch_and_posts_go_to_analysis(ledger) -> None:
    api = Api(rows)
    campaigns, store, tasks, _runs, worker = await world(api)
    cid = await new_campaign(campaigns, store)
    assert await worker.tick() == 1
    assert await worker.tick() == 0  # its task runs: no second one
    await tasks.wait()
    launches = api.launches()
    assert len(launches) == 3  # APIFY_SOCIAL_MAX_TARGETS, Instagram 2 + TikTok 1
    for body in launches:  # one target per run: one search, one hashtag or one query
        targets = [body.get("search")] if "search" in body else body.get("hashtags") or body.get("searchQueries")
        assert len(targets) == 1 and targets[0]
    assert sum("search" in b for b in launches) == 2
    # posts: collected for the campaign, ready for the analysis worker, each URL once
    assert store.links and all(link[0] == cid for link in store.links)
    assert {p["platform"] for p in store.posts.values()} == {"instagram", "tiktok"}
    assert all(p["state"] == "normalised" for p in store.posts.values())
    assert {row["state"] for row in store.social.values()} == {"done"}
    assert {q.state for q in store.queries} == {"done"}
    # a later tick and a restarted worker launch nothing more
    assert await worker.tick() == 0
    restarted = ApifySocialWorker(store, campaigns, worker.sources, QueryPlanner(None), tasks, max_targets=3)
    assert await restarted.tick() == 0
    await tasks.wait()
    assert len(api.launches()) == 3
    await tasks.http.aclose()


async def test_over_budget_launches_nothing(ledger) -> None:
    api = Api(rows)
    campaigns, store, tasks, runs, worker = await world(api)
    cid = await new_campaign(campaigns, store)
    await costs.record("api", cost_usd=5.0, campaign_id=cid)  # the campaign's budget is spent
    await worker.tick()
    await tasks.wait()
    assert api.launches() == [] and runs.rows == {}
    assert {q.state for q in store.queries} == {"skipped"}
    assert {row["state"] for row in store.social.values()} == {"done"}
    await tasks.http.aclose()


async def test_a_budget_below_one_run_cap_plans_no_target(ledger) -> None:
    api = Api(rows)
    campaigns, store, tasks, _runs, worker = await world(api, platforms=("instagram",), budget_usd=0.1)
    await new_campaign(campaigns, store)
    assert await worker.tick() == 0
    assert api.launches() == [] and store.queries == []
    assert {row["state"] for row in store.social.values()} == {"done"}
    await tasks.http.aclose()


class Broken:
    """A source that raises (a refused input, «all links rejected»): the target fails, nothing stops."""

    def __init__(self, exc: Exception) -> None:
        self.exc, self.calls = exc, 0
        self.config = IG
        self.actor_id = IG.posts_actor

    async def run(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise self.exc


@pytest.mark.parametrize("exc", [ValueError("all links rejected"), RuntimeError("db down")])
async def test_a_source_error_is_an_empty_result_and_the_campaign_goes_on(ledger, exc: Exception) -> None:
    campaigns = MemoryCampaignStore()
    store = MemoryApifySocialStore()
    tasks = ApifyTasks(apify_http(Api()))
    broken = Broken(exc)
    worker = ApifySocialWorker(store, campaigns, {"instagram": broken}, QueryPlanner(None), tasks, max_targets=2)
    cid = await new_campaign(campaigns, store)
    await worker.tick()
    await tasks.wait()
    assert broken.calls == 2
    assert {q.state for q in store.queries} == {"failed"}
    assert store.social[(cid, "instagram")]["state"] == "done"
    assert (await campaigns.get(cid)).state == "planned"  # the campaign itself is untouched
    await tasks.http.aclose()


async def test_a_failed_apify_run_is_logged_and_the_next_target_still_runs(ledger) -> None:
    api = Api(rows, run_status="FAILED")
    campaigns, store, tasks, _runs, worker = await world(api, platforms=("tiktok",), max_targets=2)
    await new_campaign(campaigns, store)
    await worker.tick()
    await tasks.wait()
    assert len(api.launches()) == 2 and {q.state for q in store.queries} == {"failed"} and store.posts == {}
    await tasks.http.aclose()


# --- out of the tick: shutdown never cuts a launch --------------------------------------------------------------


async def test_shutdown_waits_for_the_launch_in_flight_then_stops(ledger) -> None:
    gate = asyncio.Event()
    api = Api(rows, gate=gate, run_status="RUNNING")
    campaigns, store, tasks, runs, worker = await world(api, platforms=("instagram",), max_targets=2)
    await new_campaign(campaigns, store)
    await worker.tick()  # returns at once: the run is a background task
    for _ in range(50):
        if tasks.http.launching:
            break
        await asyncio.sleep(0)
    assert tasks.http.launching == 1
    closing = asyncio.create_task(tasks.aclose(timeout=5))
    await asyncio.sleep(0.01)
    assert not closing.done()  # waits for the POST's answer
    gate.set()
    await closing
    [row] = runs.rows.values()
    assert row["status"] == "running" and row["run_id"] == "RUN1"  # saved: resumed later, never launch_unconfirmed
    assert len(api.launches()) == 1  # the second target never started
    # the next start plans the stopped target again; its run is resumed, not launched a second time
    assert await store.recover_apify() == 1
    assert [q.state for q in store.queries] == ["planned", "planned"]
    await tasks.http.aclose()


async def test_shutdown_during_a_429_pause_ends_at_once_without_spending_the_attempt(ledger) -> None:
    api = Api(rows, statuses=[429, 201])
    campaigns, store, tasks, runs, worker = await world(api, platforms=("instagram",), max_targets=1)
    await new_campaign(campaigns, store)
    await worker.tick()
    for _ in range(50):
        if api.calls:
            break
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.01)
    await tasks.aclose(timeout=5)  # the 15 s pause is not waited for
    [row] = runs.rows.values()
    assert row["status"] == "claimed" and row["run_id"] is None and row["attempts"] == 0
    assert len(api.launches()) == 1
    await tasks.http.aclose()


# --- the runner's wait for the Apify runs is bounded ------------------------------------------------------------


class SharedRunStore(MemoryRunStore):
    """The runner reads ``campaign_social_state`` the Apify worker writes (as the Postgres stores share it)."""

    def __init__(self, campaigns: MemoryCampaignStore, social: MemoryApifySocialStore) -> None:
        super().__init__(campaigns)
        self.apify = social

    async def social_activity(self, campaign_id: str) -> SocialActivity:
        rows = [row for (cid, _p), row in self.apify.social.items() if cid == campaign_id]
        return SocialActivity(pending=any(r["state"] in ("pending", "running") for r in rows))


async def test_the_campaign_waits_for_apify_runs_but_not_past_the_grace(ledger) -> None:
    gate = asyncio.Event()
    api = Api(rows, gate=gate)
    campaigns, social, tasks, _runs, worker = await world(api, platforms=("instagram",), max_targets=1)
    store = SharedRunStore(campaigns, social)
    clock = Clock()
    runner = CampaignRunner(campaigns, store, FakeMessenger(), FakeDiscovery(campaigns, store, 5), now=clock,
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=30,
                                                analysis_grace_seconds=0, social_grace_seconds=900))
    cid = await campaigns.create(plan_campaign(LAND), chat_id=-1, requested_by=7, source_text=LAND, actor="t")
    social.campaigns[cid] = "planned"
    await runner.tick()
    await worker.tick()  # the Apify run starts and hangs on its POST
    store.finish_batch(next(b for _, b, s in store.windows[cid] if s == "active"))
    await runner.tick()
    clock.advance(31)
    await runner.tick()  # the queue is exhausted: the drain waits for the Apify run
    clock.advance(600)
    await runner.tick()
    assert (await campaigns.get(cid)).state == "running"
    clock.advance(301)  # past CAMPAIGN_SOCIAL_GRACE_SECONDS: the campaign completes, the run does not hold it
    await runner.tick()
    assert (await campaigns.get(cid)).state == "completed"
    assert social.social[(cid, "instagram")]["state"] in ("pending", "running")  # the run itself goes on
    gate.set()
    await tasks.wait()
    await tasks.http.aclose()


async def test_the_campaign_completes_once_the_apify_runs_are_done(ledger) -> None:
    api = Api(rows)
    campaigns, social, tasks, _runs, worker = await world(api, platforms=("instagram",), max_targets=1)
    store = SharedRunStore(campaigns, social)
    clock = Clock()
    runner = CampaignRunner(campaigns, store, FakeMessenger(), FakeDiscovery(campaigns, store, 5), now=clock,
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=30,
                                                analysis_grace_seconds=0, social_grace_seconds=900))
    cid = await campaigns.create(plan_campaign(LAND), chat_id=-1, requested_by=7, source_text=LAND, actor="t")
    social.campaigns[cid] = "planned"
    await runner.tick()
    await worker.tick()
    store.finish_batch(next(b for _, b, s in store.windows[cid] if s == "active"))
    await runner.tick()
    clock.advance(31)
    await runner.tick()
    assert (await campaigns.get(cid)).state == "running"
    await tasks.wait()
    await runner.tick()
    assert (await campaigns.get(cid)).state == "completed"
    await tasks.http.aclose()


# --- the report knows the new platforms -------------------------------------------------------------------------


async def test_instagram_and_tiktok_sources_in_the_summary_and_the_final_report() -> None:
    sources = [SourceCount("instagram", "instagram", 1, 3, 1, 1, 0), SourceCount("tiktok", "tiktok", 1, 2, 0, 0, 1),
               SourceCount("linkedin", "linkedin", 1, 1, 0, 0, 0)]
    text = summary_text("Участок в Марбелье", sources, [])
    assert "Instagram — 3 поста" in text and "TikTok — 2 поста" in text
    report = await FinalReporter().build("Участок в Марбелье", Request(), [], [], sources, [])
    assert report
