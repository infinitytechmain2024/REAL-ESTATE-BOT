"""Campaign metrics (PLAN 4.4): the ``campaign_metrics`` row, the runner's refresh, ``/campaign report``, migration 039."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.metrics import CampaignMetrics, campaign_title, report_text
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.control_plane.models import CommandEnvelope
from bot.orchestra.parser import CommandValidationError, parse_campaign
from tests.test_campaign_dedup import GOAL, IDEALISTA, _seed, needs_db, payload
from tests.test_campaign_dedup import pool as pool
from tests.test_campaign_runner import FakeMessenger
from tests.test_user_intake import OTHER_USER, OWNER, USER, claimed, dispatch, plane, say

NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
METRICS = CampaignMetrics("c1", queries=12, results=340, pages_http=18, pages_render=5, pages_scrape=2, pages_failed=4,
                          findings=10, exact=4, similar=2, other=1, excluded={"budget": 2, "place": 1}, duplicates=1,
                          updated_at=NOW)


# --- the text ---------------------------------------------------------------------------------------------


def test_the_owners_report_is_technical_and_the_users_is_not() -> None:
    owner = report_text(METRICS, "квартира · Валенсия", technical=True)
    assert owner.splitlines()[0] == "📊 Отчёт по поиску: квартира · Валенсия"
    assert "Запросов в поиск: 12 · результатов: 340" in owner
    assert "Страниц прочитано: 25 (HTTP 18 · браузер 5 · API 2) · не открылось: 4" in owner
    assert "Найдено: 10 объявлений" in owner
    assert "Точных: 4 · похожих: 2 · других: 1 · повторов: 1 · отклонено: 3" in owner
    assert owner.index("не тот город или район: 1") < owner.index("дороже бюджета: 2")  # the reasons in the report's order
    assert "  · дороже бюджета: 2" in owner and "  · не тот город или район: 1" in owner
    assert owner.endswith("Данные на 12:00 UTC")
    user = report_text(METRICS, "квартира · Валенсия", technical=False)
    assert "Страниц прочитано: 25" in user and "HTTP" not in user and "Запросов" not in user and "UTC" not in user
    assert "Найдено: 10 объявлений" in user and "дороже бюджета: 2" in user


def test_an_empty_campaign_says_so() -> None:
    text = report_text(CampaignMetrics("c"), technical=True)
    assert text == "📊 Отчёт по поиску\nОбъявлений пока не найдено."
    assert "Найдено: 1 объявление" in report_text(CampaignMetrics("c", findings=1, exact=1))


def test_the_light_copies_match_the_final_report() -> None:
    from bot.campaign import metrics as light
    from bot.campaign.final_report import REASONS, task_title
    from bot.campaign.runner import campaign_request
    from bot.campaign.summary import plural

    assert light.REASONS == REASONS
    for n in (1, 2, 5, 11, 21, 104):
        assert light.plural(n, "объявление", "объявления", "объявлений") == plural(n, "объявление", "объявления", "объявлений")

    class Campaign:
        plan = plan_campaign(GOAL)
        source_text = GOAL

    assert campaign_title(Campaign) == task_title(campaign_request(Campaign), Campaign.plan.location_aliases.get("ru"),
                                                  Campaign.plan.goal)


def test_the_metrics_module_stays_light_for_the_orchestra_image() -> None:
    """The dispatcher's image carries no analysis package: importing the metrics must not pull it in."""
    code = ("import sys; import bot.campaign.metrics as m; m.report_text(m.CampaignMetrics('c'), 'x', technical=False); "
            "import bot.orchestra.dispatcher; "
            "assert not [k for k in sys.modules if k.startswith('bot.analysis_pipeline')], 'heavy import'")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr[-400:]


# --- the command --------------------------------------------------------------------------------------------


def test_report_arguments_parse() -> None:
    assert parse_campaign("report") == ("report", "") and parse_campaign(" Report abc ") == ("report", "abc")
    with pytest.raises(CommandValidationError):
        parse_campaign("report a b")


async def test_the_owner_reports_any_campaign_and_a_user_only_their_own() -> None:
    campaigns = MemoryCampaignStore()
    plan = plan_campaign(GOAL)
    mine = await campaigns.create(plan, chat_id=USER, requested_by=USER, source_text=GOAL, actor="t")
    theirs = await campaigns.create(plan, chat_id=OTHER_USER, requested_by=OTHER_USER, source_text=GOAL, actor="t")
    campaigns.metrics[mine] = CampaignMetrics(mine, queries=3, pages_http=2, findings=1, exact=1)
    campaigns.metrics[theirs] = CampaignMetrics(theirs, queries=9, findings=5, exact=5)
    store, _, notices = await dispatch(
        claimed("campaign", "report"),                        # the user's latest campaign of their chat
        claimed("campaign", f"report {mine}"),
        claimed("campaign", f"report {theirs}"),              # somebody else's: refused
        claimed("campaign", f"report {theirs}", user=OWNER),  # an owner may read any campaign
        campaigns=campaigns)
    first, second, refused, owner = [n for n in notices if not n.startswith("Orchestra:")]  # the owner also gets a «processing» line
    assert "Найдено: 1 объявление" in first and "Запросов" not in first and mine not in first
    assert second == first
    assert refused == "Кампания не найдена. Отчёт доступен только по своим кампаниям."
    assert "Запросов в поиск: 9" in owner and "Найдено: 5 объявлений" in owner
    assert campaigns.metric_refreshes == [mine, mine, theirs]  # the report is computed fresh
    assert all(state.name == "FINISHED" for _, state, _, _ in store.completed)
    _, _, empty = await dispatch(claimed("campaign", "report", user=OWNER), campaigns=MemoryCampaignStore())
    assert empty[-1] == "В этом чате ещё нет кампаний."


async def test_the_control_plane_queues_the_report_without_confirmation() -> None:
    control, sink, _ = plane()
    assert (await say(control, OWNER, "/campaign report")).text == "Собираю отчёт по поиску."
    assert (await say(control, OWNER, "/campaign report 0b7a1f5e-7c56-4c3b-9d0e-1a2b3c4d5e6f")).text == "Собираю отчёт по поиску."
    assert (await say(control, USER, "/campaign report")).text == "Собираю отчёт по поиску."
    assert [(e.command, e.arguments, e.user_id) for e in sink.envelopes] == [
        ("campaign", "report", OWNER), ("campaign", "report 0b7a1f5e-7c56-4c3b-9d0e-1a2b3c4d5e6f", OWNER),
        ("campaign", "report", USER)]
    assert all(isinstance(e, CommandEnvelope) and e.auto is False for e in sink.envelopes)


# --- the runner -------------------------------------------------------------------------------------------------


async def test_the_runner_refreshes_the_metrics_on_a_throttle_and_once_when_the_campaign_ends() -> None:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    clock = [NOW]
    runner = CampaignRunner(campaigns, store, FakeMessenger(), None, owner_ids={7}, now=lambda: clock[0],
                            config=RunnerConfig(relevance_fail_closed=False, metrics_seconds=30))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL, actor="t")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    await runner.step(cid)
    await runner.step(cid)
    assert campaigns.metric_refreshes == [cid]  # the second step is inside the throttle
    clock[0] += timedelta(seconds=31)
    await runner.step(cid)
    assert campaigns.metric_refreshes == [cid, cid]
    await campaigns.cancel(cid, "t")
    await runner.step(cid)
    await runner.step(cid)
    assert campaigns.metric_refreshes == [cid, cid, cid]  # once at the end, not on every later step


async def test_a_failing_refresh_never_stops_the_step() -> None:
    campaigns = MemoryCampaignStore()

    async def broken(_cid: str):
        raise RuntimeError("db down")

    campaigns.refresh_metrics = broken  # type: ignore[method-assign]
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7}, config=RunnerConfig(relevance_fail_closed=False))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL, actor="t")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    await runner.step(cid)
    assert messenger.sent  # the status message went out


# --- PostgreSQL (migration 039) ---------------------------------------------------------------------------------------


@needs_db
async def test_postgres_recomputes_the_row_from_queries_pages_and_findings(pool) -> None:
    from bot.campaign.runs import PostgresRunStore
    from bot.campaign.store import PostgresCampaignStore
    from bot.orchestra.store import SafetyLimits

    campaigns = PostgresCampaignStore(pool)
    store = PostgresRunStore(pool, SafetyLimits())
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL, actor="telegram:42")
    assert await campaigns.campaign_metrics(cid) is None
    assert await campaigns.refresh_metrics(cid) == CampaignMetrics(cid, updated_at=(await campaigns.campaign_metrics(cid)).updated_at)

    for n, (state, results) in enumerate([("searched", 10), ("searched", 5), ("failed", None), ("pending", None)]):
        await pool.execute(
            """insert into web_search_queries (campaign_id, round_no, query_text, query_key, state, result_count)
               values ($1::uuid, 1, $2, $2, $3, $4)""", cid, f"q{n}", state, results)
    pages = [("fetched", "http", None)] * 3 + [("fetched", "render", None), ("fetched", "scrape", None),
                                                 ("fetched", "http", "search_snippet"), ("fetched", "none", "search_snippet"),
                                                 ("failed", "http", "http_403"), ("failed", "render", "render_blocked"),
                                                 ("queued", None, None), ("skipped", None, "robots")]
    for n, (state, layer, detail) in enumerate(pages):
        await pool.execute(
            """insert into web_campaign_urls (campaign_id, url_key, url, host, state, layer, detail)
               values ($1::uuid, $2, $3, 'idealista.com', $4, $5, $6)""",
            cid, f"{n:064x}", f"https://idealista.com/{n}", state, layer, detail)
    sent, similar, rejected, dup = await _seed(pool, cid, [
        (IDEALISTA, payload(199_000, 85)), ("https://www.pisos.com/s", payload(150_000, 60, rooms=2)),
        ("https://www.pisos.com/r", payload(900_000, 60, rooms=2)),
        ("https://www.fotocasa.es/d", payload(200_000, 86))])
    assert await store.claim_finding(cid, sent) == 1
    await store.finding_sent(sent, 5)
    assert await store.hold_finding(cid, similar, "similar", 0.2, why="budget")
    assert await store.hold_finding(cid, rejected, "excluded", None, why="budget")
    assert await store.attach_to_cluster(dup, sent, {"url": "https://www.fotocasa.es/d", "site": "fotocasa.es"})

    got = await campaigns.refresh_metrics(cid)
    assert (got.queries, got.results) == (2, 15)
    assert (got.pages_http, got.pages_render, got.pages_scrape, got.pages_failed) == (3, 1, 1, 2)
    assert (got.findings, got.exact, got.similar, got.other, got.duplicates) == (4, 1, 1, 0, 1)
    assert got.excluded == {"budget": 1} and got.pages_read == 5 and got.rejected == 1
    stored = await campaigns.campaign_metrics(cid)
    assert stored == got
    assert await campaigns.refresh_metrics(cid) == (await campaigns.campaign_metrics(cid))  # idempotent, one row
    assert await pool.fetchval("select count(*) from campaign_metrics") == 1
    text = report_text(got, "x")
    assert "Страниц прочитано: 5 (HTTP 3 · браузер 1 · API 1) · не открылось: 2" in text and "дороже бюджета: 1" in text
    with pytest.raises(ValueError):
        await campaigns.refresh_metrics("not-a-uuid")
    json.dumps(got.excluded)


@needs_db
async def test_postgres_finish_fetch_records_the_layer(pool) -> None:
    from bot.campaign.store import PostgresCampaignStore
    from bot.web_search.models import PageResult, QueuedUrl
    from bot.web_search.store import PostgresWebStore
    from bot.web_search.urls import url_key

    campaigns = PostgresCampaignStore(pool)
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL, actor="t")
    store = PostgresWebStore(pool)
    await store.start_run(cid)
    text = "Piso en venta en Valencia, Ruzafa, 3 habitaciones, 85 m2, 199.000 euros. " * 3
    for n, (host, layer, ok) in enumerate([("idealista.com", "http", True), ("fotocasa.es", "render", True),
                                           ("pisos.com", "scrape", True), ("habitaclia.com", "http", False)]):
        url = f"https://www.{host}/inmueble/{n}000000/"
        queued = QueuedUrl(url, url_key(url), host, 0, "listing")
        await pool.execute(
            "insert into web_campaign_urls (campaign_id, url_key, url, host, kind) values ($1::uuid, $2, $3, $4, 'listing')",
            cid, queued.url_key, url, host)
        ticket = await store.begin_fetch(cid, queued, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60,
                                         render_layer=True, scrape_layer=True)
        assert not isinstance(ticket, str)
        await store.finish_fetch(ticket, PageResult(ok, "listing", url, "t", text if ok else "", layer=layer,
                                                    error=None if ok else "http_404"))
    layers = {r["host"]: (r["state"], r["layer"]) for r in await pool.fetch(
        "select host, state, layer from web_campaign_urls where campaign_id = $1::uuid", cid)}
    assert layers == {"idealista.com": ("fetched", "http"), "fotocasa.es": ("fetched", "render"),
                      "pisos.com": ("fetched", "scrape"), "habitaclia.com": ("failed", "http")}
    got = await campaigns.refresh_metrics(cid)
    assert (got.pages_http, got.pages_render, got.pages_scrape, got.pages_failed) == (1, 1, 1, 1)
