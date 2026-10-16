"""v2 B1: a Facebook post link -> its comments through Apify (mocked) -> investor leads -> cards.

Only ``httpx.MockTransport`` on ``api.apify.com`` and in-memory stores: nothing here reaches Apify, a model or Telegram.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from bot.acquisition.apify_client import ApifyHttp
from bot.acquisition.apify_facebook_comments import (
    POST_TEXT_MAX_USD,
    ApifyCommentsSource,
    CommentsConfig,
    comment_items,
)
from bot.campaign import MemoryCampaignStore
from bot.campaign.fblinks import post_link_only
from bot.campaign.leads import MemoryLeadStore, Person, Sighting, person_card
from bot.campaign.post_leads import MemoryPostReadStore, PostLeadWorker, url_key
from bot.campaign.runner import CampaignRunner, RunnerConfig, post_read_line
from bot.campaign.runs import MemoryRunStore, PostReadSummary
from bot.campaign.settings import CampaignRunnerSettings
from bot.utils import costs
from tests.test_campaign_runner import CHAT, Clock, FakeMessenger

TOKEN = "apify_api_SECRETtoken123"
POST = "https://www.facebook.com/groups/123/posts/456/"
CID = "11111111-1111-1111-1111-111111111111"
CONFIG = CommentsConfig(build="1.2.3", max_comments=200, max_usd_per_post=1.0, page_size=10)
WITH_POSTS = CommentsConfig(build="1.2.3", page_size=10, posts_actor_id="apify/facebook-posts-scraper")
INTERESTED = ["Me interesa, ¿precio?", "Interesado, escríbeme por privado", "Invierto en terrenos, info por favor",
              "Cuánto cuesta? me interesa", "Quiero comprar, ¿sigue disponible?"]


def profile(n: int) -> str:
    return f"https://www.facebook.com/profile.php?id={100000000000 + n}"


def comment(n: int, text: str, **extra: Any) -> dict[str, Any]:
    return {"profileUrl": profile(n), "profileName": f"Persona {n}", "text": text,
            "commentUrl": f"{POST}?comment_id={n}", **extra}


def thirty() -> list[dict[str, Any]]:
    """30 comments: 5 show interest, the rest are noise (tags, thanks, jokes)."""
    return [comment(n, INTERESTED[n] if n < 5 else f"jaja qué bueno {n}") for n in range(30)]


class Api:
    """A scripted api.apify.com: the comments actor and the posts actor, every request recorded."""

    def __init__(self, *, items: list[Any] | None = None, status: str = "SUCCEEDED", usage: float | None = 0.0825,
                 post_text: str | None = "Vendo parcela de 2.000 m² en Boadilla", launch: Any = None) -> None:
        self.items, self.status, self.usage, self.post_text, self.launch = items or [], status, usage, post_text, launch
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            if isinstance(self.launch, Exception):
                raise self.launch
            if isinstance(self.launch, int):
                return httpx.Response(self.launch, json={"error": "x"})
            posts = "posts-scraper" in path
            return httpx.Response(201, json={"data": {"id": "RUNP" if posts else "RUNC", "status": "READY",
                                                      "defaultDatasetId": "DSP" if posts else "DSC"}})
        if "/actor-runs/RUNP" in path:
            return httpx.Response(200, json={"data": {"id": "RUNP", "status": "SUCCEEDED", "defaultDatasetId": "DSP",
                                                      "usageTotalUsd": 0.01}})
        if "/actor-runs/RUNC" in path:
            return httpx.Response(200, json={"data": {"id": "RUNC", "status": self.status, "defaultDatasetId": "DSC",
                                                      "usageTotalUsd": self.usage}})
        if "/datasets/DSP/items" in path:
            return httpx.Response(200, json=[{"text": self.post_text}] if self.post_text else [])
        if "/datasets/DSC/items" in path:
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            return httpx.Response(200, json=self.items[offset:offset + limit])
        return httpx.Response(404)

    def launches(self, actor: str = "") -> list[httpx.Request]:
        return [r for r in self.calls if r.method == "POST" and r.url.path.endswith("/runs") and actor in r.url.path]


def source(api: Api, config: CommentsConfig = CONFIG) -> ApifyCommentsSource:
    async def no_sleep(_: float) -> None:
        return None

    http = ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(api)), sleep=no_sleep, jitter=lambda: 0)
    return ApifyCommentsSource(http, config)


def worker(api: Api, store: MemoryPostReadStore | None = None, leads: MemoryLeadStore | None = None,
           config: CommentsConfig = CONFIG):
    store = store or MemoryPostReadStore([(CID, POST)])
    leads = leads or MemoryLeadStore()
    return PostLeadWorker(store, source(api, config), leads), store, leads


@pytest.fixture
def ledger():
    sink = costs.MemoryLedger()
    costs.install(sink, budget_usd=5)
    try:
        yield sink
    finally:
        costs.install(None)


# --- the source and its mapper ------------------------------------------------------------------------------------


def test_the_mapper_reads_the_documented_fields_and_guesses_nothing() -> None:
    items = [comment(1, "Me interesa  ¿precio?", postTitle="Vendo parcela"),
             comment(2, "ok", profileUrl="https://www.facebook.com/groups/1/"),      # not a person
             {"profileName": "Sin enlace", "text": "hola, me interesa"},               # no profile link at all
             {"profileUrl": profile(3), "text": ""},                                    # no text
             "not a dict"]
    result = comment_items(items, limit=10)
    assert [(c.author, c.profile_url, c.text, c.comment_url) for c in result.comments] == [
        ("Persona 1", profile(1), "Me interesa ¿precio?", f"{POST}?comment_id=1")]
    assert result.post_text == "Vendo parcela" and result.error is None
    assert comment_items([{"error": "not_available", "errorDescription": "closed"}], limit=10).error == "post_unavailable"
    assert comment_items([], limit=10) == comment_items([], limit=10) and comment_items([], limit=10).error is None


def test_the_config_needs_a_pinned_build_and_sane_limits() -> None:
    for build in ("", "latest", "  "):
        with pytest.raises(ValueError, match="pinned"):
            CommentsConfig(build=build)
    with pytest.raises(ValueError, match="unsafe"):
        CommentsConfig(build="1.2.3", max_usd_per_post=0)


def test_the_flag_is_off_by_default_and_on_needs_a_token_and_a_pinned_build(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [k for k in os.environ if k.startswith("APIFY_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    off = CampaignRunnerSettings(_env_file=None)
    assert off.apify_comments_enabled is False and off.post_reads_config() is None
    monkeypatch.setenv("APIFY_FACEBOOK_COMMENTS_ENABLED", "true")
    with pytest.raises(ValueError, match="APIFY_TOKEN"):
        CampaignRunnerSettings(_env_file=None)
    monkeypatch.setenv("APIFY_TOKEN", TOKEN)
    with pytest.raises(ValueError, match="ACTOR_BUILD"):
        CampaignRunnerSettings(_env_file=None)
    monkeypatch.setenv("APIFY_FACEBOOK_COMMENTS_ACTOR_BUILD", "1.2.3")
    config = CampaignRunnerSettings(_env_file=None).post_reads_config()
    assert config is not None and (config.actor_id, config.max_comments, config.max_usd_per_post) == (
        "apify/facebook-comments-scraper", 200, 1.0)
    assert TOKEN not in repr(CampaignRunnerSettings(_env_file=None))


# --- the worker -----------------------------------------------------------------------------------------------------


async def test_30_comments_become_5_leads_one_cost_row_and_a_capped_launch(ledger) -> None:
    api = Api(items=thirty())
    w, store, leads = worker(api, config=WITH_POSTS)
    assert await w.step() is True
    row = store.rows[(CID, POST)]
    assert (row["state"], row["comments"], row["leads"], row["run_id"], row["error"]) == ("done", 30, 5, "RUNC", None)
    assert {(k[0]) for k in leads.leads} == {profile(n).split("id=")[1].join(["fb:", ""]) for n in range(5)}
    lead = next(iter(leads.leads.values()))
    assert lead.read.finding_id is None and lead.read.campaign_id == CID and lead.read.post_url == POST
    assert lead.comment.profile_url.startswith("https://www.facebook.com/profile.php?id=") and lead.judged_by == "rules"
    # the comments run: pinned build, the charge cap and the limits in the request
    launch = api.launches("comments-scraper")[0]
    assert launch.url.params["build"] == "1.2.3" and launch.url.params["maxTotalChargeUsd"] == "1.0"
    body = json.loads(launch.content)
    assert body["startUrls"] == [{"url": POST}] and body["resultsLimit"] == 200 and body["includeNestedComments"] is True
    # one booked cost per run, never more: the comments run and the post-text run
    costs_rows = [e for e in ledger.entries if e.kind == "cost"]
    assert sorted(ledger.keys) == [f"post-read:{CID}:{url_key(POST)}", f"post-text:{CID}:{url_key(POST)}"]
    assert [(e.campaign_id, e.stage, e.provider, e.cost_usd) for e in costs_rows] == [
        (CID, "api", "apify", 0.0825), (CID, "api", "apify", 0.01)]
    assert row["cost"] == pytest.approx(0.0925)
    assert [e.units for e in ledger.entries if e.kind == "skip"] == [25]  # the noise is counted, never stored
    # the post's text reached the judge's context through the posts actor
    assert json.loads(api.launches("posts-scraper")[0].content)["resultsLimit"] == 1


async def test_the_post_text_comes_with_the_comments_and_the_posts_actor_is_off_by_default(ledger) -> None:
    api = Api(items=[comment(n, INTERESTED[n], postTitle="Vendo parcela de 2.000 m²") for n in range(5)])
    w, store, _ = worker(api)  # the default config: no posts actor
    await w.step()
    assert store.rows[(CID, POST)]["leads"] == 5 and api.launches("posts-scraper") == []
    assert [e.cost_usd for e in ledger.entries if e.kind == "cost"] == [0.0825]


async def test_the_judge_is_asked_in_chunks_and_the_rules_cover_a_failed_call(ledger) -> None:
    from bot.campaign.leads import Verdict

    calls: list[tuple[str, int]] = []

    class Judge:
        model = "test/judge"

        async def judge(self, context, comments):
            calls.append((context.post_text, len(comments)))
            if len(calls) == 2:
                raise RuntimeError("model down")
            return [Verdict("investor", 0.9, "quiere invertir") for _ in comments]

    api = Api(items=[comment(n, f"texto {n}") for n in range(50)])
    store, leads = MemoryPostReadStore([(CID, POST)]), MemoryLeadStore()
    w = PostLeadWorker(store, source(api, WITH_POSTS), leads, Judge())
    await w.step()
    assert calls == [("Vendo parcela de 2.000 m² en Boadilla", 40), ("Vendo parcela de 2.000 m² en Boadilla", 10)]
    assert store.rows[(CID, POST)]["leads"] == 40  # the failed chunk fell back to the rules: plain texts are not leads
    assert {lead.judged_by for lead in leads.leads.values()} == {"test/judge"}


async def test_no_comments_is_done_without_leads_and_the_report_says_so(ledger) -> None:
    api = Api(items=[], post_text=None)
    w, store, leads = worker(api)
    await w.step()
    row = store.rows[(CID, POST)]
    assert (row["state"], row["comments"], row["leads"], row["error"]) == ("done", 0, 0, None)
    assert not leads.leads and not api.launches("posts-scraper")  # nothing to judge: no second paid run
    assert "Лидов нет" in post_read_line(PostReadSummary(POST, "done", 0, 0, row["cost"]))


@pytest.mark.parametrize(("api", "error"), [
    (Api(items=[{"error": "not_available", "errorDescription": "closed group"}]), "post_unavailable"),
    (Api(status="FAILED"), "apify_run_failed"),
    (Api(status="TIMED-OUT"), "apify_run_timed_out"),
    (Api(launch=403), "apify_http_403"),
])
async def test_a_closed_or_failed_post_ends_failed_with_a_code_and_never_retries(api: Api, error: str, ledger) -> None:
    w, store, leads = worker(api)
    await w.step()
    row = store.rows[(CID, POST)]
    assert (row["state"], row["error"], row["leads"]) == ("failed", error, 0)
    assert await w.step() is False and len(api.launches()) <= 1  # failed is final
    assert "Пост недоступен" in post_read_line(PostReadSummary(POST, "failed", error=error))
    assert not leads.leads


async def test_a_second_tick_never_launches_the_paid_run_again(ledger) -> None:
    api = Api(items=thirty())
    w, store, _ = worker(api)
    store.rows[(CID, POST)] = {"state": "queued", "attempts": 1, "run_id": "RUNC", "comments": None, "leads": None,
                               "cost": None, "error": None}  # a crash after the run id was stored
    store.links = [(CID, POST)]
    assert await w.step() is True
    assert api.launches() == []  # resumed: the stored run id was awaited, nothing was started
    assert store.rows[(CID, POST)]["state"] == "done" and store.rows[(CID, POST)]["leads"] == 5
    assert await w.step() is False and api.launches() == []


async def test_a_transient_failure_requeues_the_same_run_and_the_retry_pays_nothing_more(ledger) -> None:
    api = Api(items=thirty(), post_text=None)
    w, store, _leads = worker(api, config=WITH_POSTS)

    class Flaky(MemoryLeadStore):
        fail = True

        async def save_leads(self, leads):
            if self.fail:
                self.fail = False
                raise RuntimeError("database down")
            return await super().save_leads(leads)

    w.leads = flaky = Flaky()
    await w.step()
    assert store.rows[(CID, POST)]["state"] == "queued" and store.rows[(CID, POST)]["run_id"] == "RUNC"
    await w.step()
    assert store.rows[(CID, POST)]["state"] == "done" and len(flaky.leads) == 5
    assert len(api.launches("comments-scraper")) == 1, "one paid comments run in all"
    assert len(api.launches("posts-scraper")) == 1, "the post text is asked once, never by a resumed read"
    assert [e.cost_usd for e in ledger.entries if e.kind == "cost"] == [0.0825, 0.01]


async def test_a_lost_launch_answer_is_booked_at_its_cap_and_never_launched_again(ledger) -> None:
    api = Api(launch=httpx.ConnectError("lost"))
    w, store, _ = worker(api)
    await w.step()
    row = store.rows[(CID, POST)]
    assert (row["state"], row["error"], row["cost"]) == ("failed", "apify_launch_unknown", 1.0)
    assert [e.cost_usd for e in ledger.entries if e.kind == "cost"] == [1.0]
    assert await w.step() is False and len(api.launches()) == 1


async def test_a_campaign_over_budget_starts_no_run(ledger) -> None:
    await ledger.add(costs.Entry(CID, "llm", "cost", cost_usd=9.0))
    api = Api(items=thirty())
    w, store, _ = worker(api)
    await w.step()
    assert store.rows[(CID, POST)]["error"] == "budget_cap" and api.launches() == []


async def test_only_facebook_posts_are_queued() -> None:
    api = Api()
    store = MemoryPostReadStore([(CID, "https://www.facebook.com/groups/123/"), (CID, "https://example.com/x")])
    w = PostLeadWorker(store, source(api), MemoryLeadStore())
    assert await w.step() is False and store.rows == {} and api.calls == []


async def test_the_token_never_appears_in_logs_errors_or_reprs(ledger, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    api = Api(launch=403)
    w, store, _ = worker(api)
    await w.step()
    api2 = Api(items=thirty())
    w2, _, _ = worker(api2)
    await w2.step()
    texts = [r.getMessage() + str(getattr(r, "__dict__", "")) for r in caplog.records]
    texts += [repr(w.source), repr(w.source.http), str(store.rows), str(ledger.entries)]
    assert TOKEN not in " ".join(texts)
    assert all(TOKEN not in r.url.query.decode() for r in api2.calls)  # sent as a header, not in the URL
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in api2.calls)


# --- the runner: people by campaign, the wait, the short report ---------------------------------------------------


def _person(n: int) -> Person:
    return Person(f"fb:{100000000000 + n}", profile(n), f"Persona {n}",
                  (Sighting(POST, INTERESTED[n], "buyer", datetime(2026, 9, 25, 11, tzinfo=UTC)),))


async def _runner(link: bool = True, discovery: Any = None, **config: Any):
    from bot.campaign import plan_campaign
    from bot.campaign.spec import Place, Sources, TaskSpec

    campaigns = MemoryCampaignStore()
    store, messenger, clock = MemoryRunStore(campaigns), FakeMessenger(), Clock()
    runner = CampaignRunner(campaigns, store, messenger, discovery, now=clock, owner_ids={7},
                            config=RunnerConfig(relevance_fail_closed=False, post_reads=True, **config))
    spec = TaskSpec(mode="investors", place=Place(name="Spain", names={"ru": "Испания"}, level="region"),
                    sources=Sources(required=[POST]), post_link=link)
    plan = plan_campaign("лиды из комментариев под постом", vertical="investors", place={"en": "Spain", "ru": "Испания"},
                         spec=spec)
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=8, source_text="лиды", actor="telegram:8",
                                 spec=spec.model_dump(mode="json"))
    await campaigns.set_state(cid, "running", "campaign:test")
    return campaigns, store, messenger, clock, runner, cid


async def test_the_runner_sends_the_commenters_of_the_post_with_profile_and_quote() -> None:
    _campaigns, store, messenger, _, runner, cid = await _runner(max_people=3)
    store.post_read_rows[cid] = [PostReadSummary(POST, "done", 30, 5, 0.09)]
    store.post_people[cid] = [_person(n) for n in range(5)]
    store.people["Spain"] = [_person(4)]  # a city's people are not this campaign's
    await runner.step(cid)
    cards = [t for _, _, t in messenger.sent if t.startswith(("🏠", "💼"))]
    assert len(cards) == 3 and set(store.deliveries) == {(cid, f"fb:{100000000000 + n}") for n in range(3)}
    assert f"Профиль: {profile(0)}" in cards[0] and "«Me interesa, ¿precio?»" in cards[0] and POST in cards[0]
    assert cards[0] == person_card(_person(0), location=None, now=runner.now())


async def test_the_campaign_waits_for_the_post_read_then_ends_with_a_short_count() -> None:
    campaigns, store, messenger, clock, runner, cid = await _runner()
    store.post_read_rows[cid] = [PostReadSummary(POST, "reading")]
    for _ in range(4):
        await runner.step(cid)
    assert (await campaigns.get(cid)).state != "completed", "the read is not over: the search is still open"
    store.post_read_rows[cid] = [PostReadSummary(POST, "done", 30, 5, 0.0925)]
    store.post_people[cid] = [_person(0)]
    for _ in range(4):
        clock.advance(30)
        await runner.step(cid)
    assert (await campaigns.get(cid)).state == "completed"
    texts = [t for _, _, t in messenger.sent]
    assert sum("Профиль:" in t for t in texts) == 1
    assert texts.count("Комментариев: 30, лидов: 5, $0.09") == 1, "the short count, once"


async def test_a_post_the_worker_has_not_queued_yet_is_waited_for_too() -> None:
    campaigns, _store, _, _, runner, cid = await _runner()
    for _ in range(4):
        await runner.step(cid)
    assert (await campaigns.get(cid)).state != "completed"


async def test_with_the_flag_off_a_post_link_campaign_still_never_turns_into_a_discovery() -> None:
    campaigns, _store, _messenger, _, runner, cid = await _runner(discovery=NoCall())
    runner.config = RunnerConfig(relevance_fail_closed=False)  # post_reads off: no reader, nothing to wait for
    for _ in range(4):
        await runner.step(cid)
    assert (await campaigns.get(cid)).state == "completed"


async def test_a_post_link_campaign_ends_with_the_web_stage_on() -> None:
    from bot.web_search.models import WebStatus

    class Web:
        async def web_status(self, _cid: str):
            return WebStatus(True, None, "сайты: ждёт запуска")  # no web run is ever made for it

        async def site_report(self, _cid: str) -> list:
            return []

    campaigns, store, messenger, clock, runner, cid = await _runner(discovery=NoCall())
    runner.web = Web()
    store.post_read_rows[cid] = [PostReadSummary(POST, "done", 30, 5, 0.09)]
    for _ in range(4):
        clock.advance(30)
        await runner.step(cid)
    assert (await campaigns.get(cid)).state == "completed"
    assert "Комментариев: 30, лидов: 5, $0.09" in [t for _, _, t in messenger.sent]


async def test_an_ordinary_campaign_with_a_post_link_among_its_sources_is_untouched() -> None:
    campaigns, store, _messenger, _, runner, cid = await _runner(link=False)  # a link in sources.required, no marker
    store.people["Spain"] = [_person(1)]
    store.post_people[cid] = [_person(0)]
    store.post_read_rows[cid] = [PostReadSummary(POST, "reading")]  # would block completion if it were consulted
    await runner.step(cid)
    assert set(store.deliveries) == {(cid, "fb:100000000001")}, "the people of the city, as before"
    assert not await runner._post_read_pending(await campaigns.get(cid))


async def test_a_lost_run_id_fails_the_read_instead_of_a_second_paid_run(ledger) -> None:
    api = Api(items=thirty())
    w, store, _ = worker(api)

    async def broken(*_a: Any) -> None:
        raise RuntimeError("database down")

    store.save_run = broken  # type: ignore[method-assign]
    await w.step()
    assert store.rows[(CID, POST)]["state"] == "failed" and store.rows[(CID, POST)]["error"] == "run_id_not_saved"
    assert await w.step() is False and len(api.launches()) == 1


class NoCall:
    """Raises on any use: proves a stage was not touched."""

    def __getattr__(self, name: str):
        raise AssertionError(f"must not be used: {name}")


async def test_a_post_link_campaign_runs_only_the_comment_read() -> None:
    campaigns, store, messenger, clock, runner, cid = await _runner(discovery=NoCall())  # discovery would raise
    store.post_read_rows[cid] = [PostReadSummary(POST, "reading")]
    await runner.step(cid)
    assert (await campaigns.get(cid)).state == "running" and not store.batches, "no discovery, no windows"
    store.post_read_rows[cid] = [PostReadSummary(POST, "done", 30, 5, 0.0925)]
    store.post_people[cid] = [_person(0)]
    for _ in range(4):
        clock.advance(30)
        await runner.step(cid)
    campaign = await campaigns.get(cid)
    assert campaign.state == "completed" and campaign.stop_reason == "post_reads_done"
    assert "Комментариев: 30, лидов: 5, $0.09" in [t for _, _, t in messenger.sent]


async def test_an_ordinary_investor_campaign_still_goes_through_discovery() -> None:
    campaigns, _, _, _, runner, cid = await _runner(link=False)  # no discovery configured: the old pause
    await runner.step(cid)
    assert (await campaigns.get(cid)).stop_reason != "post_reads_done"
    assert not post_link_only(await campaigns.get(cid))


async def test_the_web_stage_skips_a_post_link_campaign_and_not_an_ordinary_one() -> None:
    from types import SimpleNamespace

    from bot.web_search.worker import WebSearchWorker

    campaigns, _, _, _, _, cid = await _runner()
    ordinary_campaigns, _, _, _, _, other = await _runner(link=False)
    reached: list[str] = []

    class Store:
        async def get_run(self, _cid: str):
            reached.append("run")
            return None

        async def start_run(self, _cid: str):
            raise AssertionError("no web run for a post link")

    for store, campaign_id, touched in ((campaigns, cid, False), (ordinary_campaigns, other, True)):
        reached.clear()
        me = SimpleNamespace(_listing_hosts={}, campaigns=store, store=Store())
        try:
            await WebSearchWorker.step(me, campaign_id)  # type: ignore[arg-type]
        except AssertionError:
            assert touched
        assert bool(reached) is touched


def test_post_link_only_needs_the_marker_investors_and_only_post_links() -> None:
    from types import SimpleNamespace

    def camp(vertical: str = "investors", **spec: Any):
        return SimpleNamespace(plan=SimpleNamespace(vertical=vertical), spec=spec)

    ok = {"post_link": True, "sources": {"required": [POST]}}
    assert post_link_only(camp(**ok))
    assert not post_link_only(camp("real_estate", **ok)) and not post_link_only(camp("both", **ok))
    assert not post_link_only(camp(**{**ok, "post_link": False}))
    assert not post_link_only(camp(**{**ok, "sources": {"required": [POST, "https://example.com/"]}}))
    assert not post_link_only(camp(**{**ok, "sources": {"required": []}}))
    assert not post_link_only(SimpleNamespace(plan=SimpleNamespace(vertical="investors"), spec=None))


# --- Postgres: migration 048 and the stores (skipped without a disposable *_test database) --------------------------

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
postgres = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"),
                              reason="set SYSTEM_TEST_DATABASE_URL to a *_test database")
ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))
ROLLBACK = ROOT / "scripts/rollback/048_campaign_post_reads_down.sql"


@pytest.fixture
async def pool():
    import asyncpg

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        costs.install(None)
        await pool.close()


@postgres
async def test_migration_048_the_worker_and_the_runner_store_on_a_real_database(pool) -> None:
    from bot.campaign import PostgresCampaignStore, plan_campaign
    from bot.campaign.leads import PostgresLeadStore
    from bot.campaign.post_leads import PostgresPostReadStore
    from bot.campaign.runs import PostgresRunStore
    from bot.campaign.spec import Place, Sources, TaskSpec
    from bot.orchestra.store import SafetyLimits

    costs.install(costs.PostgresLedger(pool), budget_usd=5)
    spec = TaskSpec(mode="investors", place=Place(name="Spain"), sources=Sources(required=[POST, "https://example.com/x"]))
    from bot.campaign.reach import PostgresReachStore
    plan = plan_campaign("лиды из комментариев под постом", vertical="investors", place={"en": "Spain"}, spec=spec)
    cid = await PostgresCampaignStore(pool).create(plan, chat_id=1, requested_by=1, source_text="лиды", actor="test",
                                                   spec=spec.model_dump(mode="json"))
    store = PostgresPostReadStore(pool)
    assert [c.id for c in await PostgresReachStore(pool).open_campaigns()] == [cid]  # an ordinary investor search
    await pool.execute("update campaigns set spec = jsonb_set(spec, '{post_link}', 'true') where id = $1::uuid", cid)
    assert await PostgresReachStore(pool).open_campaigns() == []  # a post link campaign sets no reach query
    await PostgresCampaignStore(pool).create(  # an ordinary campaign with the same link: never a candidate
        plan, chat_id=1, requested_by=1, source_text="x", actor="test",
        spec=spec.model_copy(update={"post_link": False}).model_dump(mode="json"))
    assert set(await store.candidates()) == {(cid, POST), (cid, "https://example.com/x")}
    api = Api(items=thirty())
    w = PostLeadWorker(store, source(api, WITH_POSTS), PostgresLeadStore(pool, SafetyLimits()))
    assert await w.step() is True
    row = await pool.fetchrow("select * from campaign_post_reads where campaign_id = $1::uuid and post_url = $2", cid, POST)
    assert (row["state"], row["run_id"], row["comments_found"], row["leads_found"], row["provider"]) == (
        "done", "RUNC", 30, 5, "apify")
    assert float(row["cost_usd"]) == pytest.approx(0.0925) and row["error_code"] is None and row["read_at"] is not None
    assert await pool.fetchval("select count(*) from investor_leads where campaign_id = $1::uuid and finding_id is null",
                               cid) == 5
    assert await pool.fetchval("select count(*) from campaign_costs where campaign_id = $1::uuid and kind = 'cost'", cid) == 2
    assert await w.step() is False and len(api.launches()) == 2  # comments + post text, nothing again
    runs = PostgresRunStore(pool, SafetyLimits())
    [summary] = await runs.post_reads(cid)
    assert (summary.state, summary.comments, summary.leads) == ("done", 30, 5)
    people = await runs.stored_people_of_campaign(cid, 3)
    assert len(people) == 3 and all(p.sightings[0].post_url == POST for p in people)
    assert await runs.claim_person(cid, people[0].profile_key)
    assert len(await runs.stored_people_of_campaign(cid, 10)) == 4  # one is delivered already
    # a crashed reader leaves the row reading: recover() queues it again with its run id
    await pool.execute("update campaign_post_reads set state = 'reading' where campaign_id = $1::uuid", cid)
    assert await store.recover() == 1
    assert await pool.fetchval("select run_id from campaign_post_reads where campaign_id = $1::uuid", cid) == "RUNC"
    # the rollback refuses while rows exist, then drops the table once it is empty
    import asyncpg

    with pytest.raises(asyncpg.PostgresError, match="still has rows"):
        await pool.execute(ROLLBACK.read_text(encoding="utf-8"))
    await pool.execute("delete from campaign_post_reads")
    await pool.execute(ROLLBACK.read_text(encoding="utf-8"))
    assert await pool.fetchval("select to_regclass('public.campaign_post_reads')") is None


def test_migration_048_is_additive_and_has_a_guarded_rollback() -> None:
    sql = (ROOT / "bot/services/db/migrations/048_campaign_post_reads.sql").read_text(encoding="utf-8").lower()
    assert "create table if not exists public.campaign_post_reads" in sql
    assert "primary key (campaign_id, post_url)" in sql and "state in ('queued', 'reading', 'done', 'failed')" in sql
    assert not any(word in sql for word in ("drop table", "alter table public.campaigns", "delete from", "update "))
    down = ROLLBACK.read_text(encoding="utf-8").lower()
    assert "raise exception" in down and "drop table if exists public.campaign_post_reads" in down


async def test_a_wait_that_never_succeeds_books_the_run_at_its_cap(ledger) -> None:
    class RunDown(Api):
        def __call__(self, request):
            if "/actor-runs/" in request.url.path:
                self.calls.append(request)
                return httpx.Response(503, json={"error": "x"})
            return super().__call__(request)

    api = RunDown(items=thirty())
    w, store, _ = worker(api)
    for _ in range(4):
        await w.step()
    row = store.rows[(CID, POST)]
    assert row["state"] == "failed" and len(api.launches()) == 1
    costs_rows = [e for e in ledger.entries if e.kind == "cost"]
    assert [(e.cost_usd, e.code) for e in costs_rows] == [(1.0, "estimated")]


async def test_a_posts_actor_wait_that_fails_is_booked_at_its_cap_with_one_launch(ledger) -> None:
    class PostsRunDown(Api):
        def __call__(self, request):
            if "/actor-runs/RUNP" in request.url.path:
                self.calls.append(request)
                return httpx.Response(503, json={"error": "x"})
            return super().__call__(request)

    api = PostsRunDown(items=thirty())
    w, store, _ = worker(api, config=WITH_POSTS)
    await w.step()
    assert len(api.launches("posts-scraper")) == 1
    estimated = [e for e in ledger.entries if e.kind == "cost" and e.code == "estimated"]
    assert len(estimated) == 1 and estimated[0].cost_usd > 0
    assert store.rows[(CID, POST)]["state"] == "done"


async def test_a_lost_posts_launch_is_booked_at_its_cap_with_one_launch(ledger) -> None:
    class PostsLaunchLost(Api):
        def __call__(self, request):
            if request.method == "POST" and "posts-scraper" in request.url.path:
                self.calls.append(request)
                raise httpx.ConnectError("lost")
            return super().__call__(request)

    api = PostsLaunchLost(items=thirty())
    w, store, _ = worker(api, config=WITH_POSTS)
    await w.step()
    assert len(api.launches("posts-scraper")) == 1
    estimated = [e for e in ledger.entries if e.kind == "cost" and e.code == "estimated"]
    assert [e.cost_usd for e in estimated] == [POST_TEXT_MAX_USD]
    assert store.rows[(CID, POST)]["state"] == "done"


async def test_over_budget_before_the_posts_actor_skips_it_and_the_read_is_done(ledger, monkeypatch) -> None:
    answers = iter([False, True])  # the comments run starts; the posts run does not
    async def over_budget(_: str) -> bool:
        return next(answers, True)

    monkeypatch.setattr(costs, "over_budget", over_budget)
    api = Api(items=thirty())
    w, store, _ = worker(api, config=WITH_POSTS)
    await w.step()
    assert api.launches("posts-scraper") == [] and len(api.launches("comments-scraper")) == 1
    assert [e.code for e in ledger.entries if e.kind == "skip" and e.code == "budget_cap"] == ["budget_cap"]
    assert store.rows[(CID, POST)]["state"] == "done"


async def test_a_successful_posts_run_leaves_one_real_post_text_row(ledger) -> None:
    api = Api(items=thirty())
    w, _, _ = worker(api, config=WITH_POSTS)
    await w.step()
    rows = [e for e in ledger.entries if e.kind == "cost"]
    assert [(e.cost_usd, e.code) for e in rows] == [(0.0825, ""), (0.01, "")]  # the estimate was replaced
    assert sorted(ledger.keys) == [f"post-read:{CID}:{url_key(POST)}", f"post-text:{CID}:{url_key(POST)}"]
