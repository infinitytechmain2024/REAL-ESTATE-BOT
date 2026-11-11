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
from bot.acquisition.apify_linkedin import (
    ApifyLinkedInCompanies,
    ApifyLinkedInPeople,
    LinkedInSources,
)
from bot.acquisition.apify_source import MemorySourceRunStore
from bot.acquisition.apify_tiktok import ApifyTikTokPosts
from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign import runner as runner_module
from bot.campaign.final_report import FinalReporter
from bot.campaign.reach import (
    MemoryReachStore,
    ReachCampaign,
    ReachConfig,
    ReachQuery,
    ReachWorker,
    keep_query,
)
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
from tests.test_apify_linkedin import CAMPAIGN as LI_CAMPAIGN
from tests.test_apify_linkedin import CONFIG as LI_CONFIG
from tests.test_apify_linkedin import FakeJudge, company, person
from tests.test_apify_linkedin import settings as runner_settings
from tests.test_apify_social import IG, TT, TT_VIDEO, ig_post_row
from tests.test_campaign_runner import Clock, FakeMessenger
from tests.test_investor_reach import FakeSearcher
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
                budget_usd: float = 5.0, linkedin_usd: float = 0.0, runs: MemorySourceRunStore | None = None,
                store: MemoryApifySocialStore | None = None):
    campaigns = MemoryCampaignStore()
    store = store or MemoryApifySocialStore()
    tasks = ApifyTasks(apify_http(api))
    runs = runs or MemorySourceRunStore()
    guarded = tasks.runs(runs)
    built = {"instagram": lambda: ApifyInstagramPosts(guarded, tasks.http, IG, sleep=tasks.sleep, poll_seconds=0),
             "tiktok": lambda: ApifyTikTokPosts(guarded, tasks.http, TT, sleep=tasks.sleep, poll_seconds=0)}
    worker = ApifySocialWorker(store, campaigns, {p: built[p]() for p in platforms}, QueryPlanner(None), tasks,
                               max_targets=max_targets, budget_usd=budget_usd, linkedin_usd=linkedin_usd)
    return campaigns, store, tasks, runs, worker


async def new_campaign(campaigns: MemoryCampaignStore, store: MemoryApifySocialStore, text: str = LAND,
                       vertical: str = "real_estate") -> str:
    plan = plan_campaign(text, vertical=vertical)  # type: ignore[arg-type]
    cid = await campaigns.create(plan, chat_id=-1, requested_by=7, source_text=text, actor="t")
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
    assert settings.reach_config().linkedin_apify is False
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
    assert deal_targets(("instagram", "tiktok"), caps, 3, None) == {"instagram": 2, "tiktok": 1}
    assert deal_targets(("tiktok",), caps, 3, 5) == {"tiktok": 3}
    assert deal_targets(("instagram", "tiktok"), caps, 3, 0.36) == {"instagram": 1, "tiktok": 1}
    assert deal_targets(("instagram",), caps, 3, 0.1) == {"instagram": 0}
    assert deal_targets(("instagram",), caps, 3, -1.0) == {"instagram": 0}  # the budget is spent already


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
    await costs.record("api", cost_usd=5.0, campaign_id=cid)  # the campaign's budget is spent before planning
    await worker.tick()
    await tasks.wait()
    assert api.launches() == [] and runs.rows == {} and store.queries == []
    assert {row["state"] for row in store.social.values()} == {"done"}

    cid2 = await new_campaign(campaigns, store)
    assert await worker.tick() == 1  # planned within the budget ...
    await costs.record("api", cost_usd=5.0, campaign_id=cid2)  # ... spent by another stage before the runs
    await tasks.wait()
    assert api.launches() == [] and runs.rows == {}
    assert {q.state for q in store.queries} == {"skipped"}
    await tasks.http.aclose()


async def test_targets_fit_what_is_left_and_leave_room_for_linkedin(ledger) -> None:
    api = Api(rows)
    campaigns, store, tasks, _runs, worker = await world(api, linkedin_usd=0.30)
    homes = await new_campaign(campaigns, store)
    # 0.70 left: one Instagram (0.20) + one TikTok posts run at the actor's minimum cap (0.50, P1.3a)
    await costs.record("api", cost_usd=4.3, campaign_id=homes)
    investors = await new_campaign(campaigns, store, "Ищу инвесторов в недвижимость в Марбелье", vertical="investors")
    await costs.record("api", cost_usd=4.6, campaign_id=investors)  # 0.40 - LinkedIn's 0.30: no Instagram run fits
    await worker.tick()
    await tasks.wait()
    planned = {(q.campaign_id, q.platform) for q in store.queries}
    assert sorted(planned) == sorted({(homes, "instagram"), (homes, "tiktok")})
    assert len(api.launches()) == 2
    await tasks.http.aclose()


async def test_the_tiktok_posts_cap_counted_is_the_actor_minimum(ledger) -> None:
    api = Api(rows)
    campaigns, store, tasks, _runs, worker = await world(api)
    assert worker.sources["tiktok"].runner.config.max_charge_usd == 0.50  # P1.3a: below it the actor refuses
    homes = await new_campaign(campaigns, store)
    await costs.record("api", cost_usd=4.6, campaign_id=homes)  # 0.40 left: Instagram 0.20, TikTok 0.50 does not fit
    await worker.tick()
    await tasks.wait()
    assert {(q.campaign_id, q.platform) for q in store.queries} == {(homes, "instagram")}
    assert len(api.launches()) == 1
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


async def test_a_refused_input_is_an_empty_result_and_the_campaign_goes_on(ledger) -> None:
    exc = ValueError("all links rejected")
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
        if tasks.launching:
            break
        await asyncio.sleep(0)
    assert tasks.launching == 1
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


# --- LinkedIn in the reach --------------------------------------------------------------------------------------


def test_keep_query_skips_linkedin_only_with_the_apify_source() -> None:
    li = ReachQuery("linkedin", "en", 'site:linkedin.com/in "real estate investor" Madrid')
    hidden = ReachQuery("web", "en", "site:es.linkedin.com/company inmobiliaria Madrid")
    assert keep_query(li, "apify") and keep_query(hidden, "apify")  # the default: as before
    assert not keep_query(li, "apify", linkedin_apify=True) and not keep_query(hidden, "apify", linkedin_apify=True)
    assert keep_query(li, "legacy", linkedin_apify=True)
    assert keep_query(ReachQuery("telegram", "ru", "site:t.me инвестиции Мадрид"), "apify", linkedin_apify=True)


def li_rows(n: int) -> list[dict[str, Any]]:
    return [person(1), person(2)] if n == 1 else [company(1)]


async def reach_world(api: Api, *, linkedin_apify: bool = True, campaign: ReachCampaign = LI_CAMPAIGN):
    tasks = ApifyTasks(apify_http(api))
    runs = MemorySourceRunStore()
    guarded = tasks.runs(runs)
    sources = LinkedInSources(ApifyLinkedInPeople(guarded, tasks.http, LI_CONFIG, sleep=tasks.sleep, poll_seconds=0),
                              ApifyLinkedInCompanies(guarded, tasks.http, LI_CONFIG, sleep=tasks.sleep, poll_seconds=0))
    store = MemoryReachStore([campaign])
    searcher = FakeSearcher()
    worker = ReachWorker(store, searcher, FakeJudge(),
                         config=ReachConfig(acquisition_mode="apify", linkedin_apify=linkedin_apify,
                                            queries_per_tick=10, queries_per_campaign=100),
                         linkedin=sources, tasks=tasks)
    return tasks, runs, store, searcher, worker


async def test_linkedin_apify_replaces_site_searches_and_runs_once(ledger) -> None:
    api = Api(li_rows)
    tasks, _runs, store, searcher, worker = await reach_world(api)
    for _ in range(4):
        await worker.tick()
        await tasks.wait()
    assert searcher.queries and not any("linkedin" in q.lower() for q, _ in searcher.queries)
    launches = api.launches()
    assert len(launches) == 2  # people once, companies once
    assert {"profileScraperMode", "scraperMode"} == {k for b in launches for k in b if k.endswith("raperMode")}
    linkedin = [s for s in store.contacts.values() if s.candidate.platform == "linkedin"]
    assert len(linkedin) == 3
    assert await store.enriched_count(LI_CAMPAIGN.id) == 0 and store.unmarked  # enriched_at never set
    assert LI_CAMPAIGN.id in store.done
    await tasks.http.aclose()


async def test_the_reach_is_not_done_while_its_linkedin_run_goes_on(ledger) -> None:
    gate = asyncio.Event()
    api = Api(li_rows, gate=gate)
    tasks, _runs, store, _searcher, worker = await reach_world(api)
    for _ in range(4):
        await worker.tick()
    assert LI_CAMPAIGN.id not in store.done  # its LinkedIn run still goes: the reach waits (reach_pending)
    gate.set()
    await tasks.wait()
    await worker.tick()
    assert LI_CAMPAIGN.id in store.done and len(api.launches()) == 2
    await tasks.http.aclose()


async def test_linkedin_without_the_flag_keeps_the_site_searches(ledger) -> None:
    api = Api(li_rows)
    tasks, _runs, _store, searcher, worker = await reach_world(api, linkedin_apify=False)
    await worker.tick()
    await tasks.wait()
    assert api.launches() == [] and any("linkedin" in q.lower() for q, _ in searcher.queries)
    await tasks.http.aclose()


async def test_linkedin_over_budget_or_a_source_error_never_stops_the_reach(ledger) -> None:
    api = Api(li_rows)
    tasks, _runs, _store, searcher, worker = await reach_world(api)
    await costs.record("api", cost_usd=5.0, campaign_id=LI_CAMPAIGN.id)
    await worker.tick()
    await tasks.wait()
    assert api.launches() == [] and searcher.queries

    class Failing:
        kind = "person"

        async def run(self, *args: Any, **kwargs: Any) -> Any:
            raise ValueError("refused input")

    costs.install(costs.MemoryLedger(), budget_usd=5)
    tasks2, _runs2, store2, _searcher2, worker2 = await reach_world(api)
    worker2.linkedin = LinkedInSources(Failing(), Failing())  # type: ignore[arg-type]
    for _ in range(4):
        await worker2.tick()
        await tasks2.wait()
    assert LI_CAMPAIGN.id in store2.done
    await tasks.http.aclose()
    await tasks2.http.aclose()


# --- the report knows the new platforms -------------------------------------------------------------------------


async def test_instagram_and_tiktok_sources_in_the_summary_and_the_final_report() -> None:
    sources = [SourceCount("instagram", "instagram", 1, 3, 1, 1, 0), SourceCount("tiktok", "tiktok", 1, 2, 0, 0, 1),
               SourceCount("linkedin", "linkedin", 1, 1, 0, 0, 0)]
    text = summary_text("Участок в Марбелье", sources, [])
    assert "Instagram — 3 поста" in text and "TikTok — 2 поста" in text
    report = await FinalReporter().build("Участок в Марбелье", Request(), [], [], sources, [])
    assert report


# --- review fixes: the whole launch is guarded, SIGTERM stops new launches at once --------------------------


class SlowSaveRuns(MemorySourceRunStore):
    """``save_run`` yields to the loop, as asyncpg does (pool, transaction, execute)."""

    async def save_run(self, run: Any, run_id: str, dataset_id: str | None) -> None:
        await asyncio.sleep(0.01)
        await super().save_run(run, run_id, dataset_id)


async def until(check: Any, steps: int = 200) -> None:
    for _ in range(steps):
        if check():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("never happened")


async def test_a_stop_between_the_201_and_save_run_still_saves_the_run_id(ledger) -> None:
    gate = asyncio.Event()
    api = Api(rows, gate=gate, run_status="RUNNING")
    runs = SlowSaveRuns()
    campaigns, store, tasks, _runs, worker = await world(api, platforms=("instagram",), max_targets=2, runs=runs)
    await new_campaign(campaigns, store)
    await worker.tick()
    await until(lambda: tasks.launching == 1 and api.calls)  # the POST is in flight
    closing = asyncio.create_task(tasks.aclose(timeout=5))
    await asyncio.sleep(0.005)
    gate.set()  # the 201 comes while the service stops; save_run then takes its time
    await closing
    [row] = runs.rows.values()
    assert row["status"] == "running" and row["run_id"] == "RUN1"  # resumable, never launch_unconfirmed
    assert len(api.launches()) == 1
    await tasks.http.aclose()


async def test_sigterm_stops_new_launches_at_once(ledger) -> None:
    gate = asyncio.Event()
    api = Api(rows, gate=gate)
    campaigns, store, tasks, runs, worker = await world(api, platforms=("instagram", "tiktok"), max_targets=3)
    await new_campaign(campaigns, store)
    await worker.tick()
    await until(lambda: tasks.launching == 1 and api.calls)
    stop = asyncio.Event()
    closer = asyncio.create_task(runner_module._close_on_stop(stop, tasks))
    runner_module._on_sigterm(stop, tasks)  # what the SIGTERM handler does
    assert stop.is_set() and tasks.stopping
    assert await worker.tick() == 0 and not tasks.spawn("x", asyncio.sleep(0))  # no new task
    gate.set()
    await closer
    assert len(api.launches()) == 1  # the one in flight; the other two targets never launched
    assert [r["status"] for r in runs.rows.values()] == ["succeeded"] or len(runs.rows) == 1
    # a source called after the stop gets «busy» from the guarded journal: no POST, no row changed
    source = worker.sources["tiktok"]
    outcome = await source.run("c-after", save=lambda posts, run_id: asyncio.sleep(0, 0), hashtags=["casa"])
    assert outcome.outcome.status == "busy" and len(api.launches()) == 1
    await tasks.http.aclose()


class FailingRecover(MemoryApifySocialStore):
    async def recover_apify(self) -> int:
        raise RuntimeError("db down")


async def test_a_failed_recovery_is_logged_and_no_platform_is_marked_done(ledger, caplog) -> None:
    store = FailingRecover()
    campaigns, store, tasks, _runs, worker = await world(Api(rows), platforms=("instagram",), store=store)
    await new_campaign(campaigns, store)
    stop = asyncio.Event()
    serving = asyncio.create_task(worker.serve(0.01, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await serving
    assert "social_apify.recover_failed" in caplog.text
    assert store.social == {} and store.queries == []  # nothing planned, nothing marked done
    await tasks.http.aclose()


class FlakySave(MemoryApifySocialStore):
    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    async def save_posts(self, campaign_id: str, vertical: str, posts: Any) -> int:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("db down")
        return await super().save_posts(campaign_id, vertical, posts)


async def test_a_database_error_while_importing_gives_the_paid_target_back(ledger) -> None:
    api = Api(rows)
    store = FlakySave()
    campaigns, store, tasks, _runs, worker = await world(api, platforms=("instagram",), max_targets=1, store=store)
    cid = await new_campaign(campaigns, store)
    await worker.tick()
    await tasks.wait()
    assert [q.state for q in store.queries] == ["planned"]  # not failed: the run is paid
    assert store.social[(cid, "instagram")]["state"] == "pending"
    await worker.tick()  # the next tick resumes the same run: its import, no second launch
    await tasks.wait()
    assert [q.state for q in store.queries] == ["done"] and len(api.launches()) == 1 and store.posts
    await tasks.http.aclose()


async def test_linkedin_without_a_place_is_not_run(ledger, caplog) -> None:
    api = Api(li_rows)
    nowhere = ReachCampaign("00000000-0000-0000-0000-000000000009", "  ", {}, ("en",))
    tasks, _runs, _store, _searcher, worker = await reach_world(api, campaign=nowhere)
    await worker.tick()
    await tasks.wait()
    assert api.launches() == [] and "campaign.reach.linkedin_no_location" in caplog.text
    await tasks.http.aclose()
