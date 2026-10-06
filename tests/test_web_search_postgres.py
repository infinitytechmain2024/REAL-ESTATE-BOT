"""The web stage on a real PostgreSQL with every migration applied (021 included).

Only SearXNG, the websites and OpenRouter are fakes. A campaign's web pages
become collected_posts linked to the campaign through an ordinary website
batch, the analysis worker turns them into findings, and the campaign runner
streams each one once, with the link to the concrete listing. A page read for
one campaign is never fetched again for another.

Skipped unless SYSTEM_TEST_DATABASE_URL names a disposable ``*_test`` database.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from bot.analysis_pipeline.main import run_once as analyse
from bot.analysis_pipeline.pipeline import AnalysisPipeline
from bot.campaign.architect import plan_campaign
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import PostgresRunStore
from bot.campaign.store import PostgresCampaignStore
from bot.orchestra.store import SafetyLimits
from bot.web_search.models import PageResult, QueuedUrl
from bot.web_search.store import BUSY, DUPLICATE, PostgresWebStore
from bot.web_search.urls import url_key
from bot.web_search.worker import WebSearchConfig, WebSearchWorker
from tests.test_campaign_runner import FakeMessenger
from tests.test_system_integration import FakeAnalyzer, Telegram, analysis_settings
from tests.test_verification_flow import OPERATOR
from tests.test_web_search import INDEX_HTML, INDEX_URL, FakeFetcher, FakeSearcher, ListGenerator

URL = os.environ.get("SYSTEM_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"),
                                reason="set SYSTEM_TEST_DATABASE_URL to a disposable *_test database")
MIGRATIONS = sorted((Path(__file__).resolve().parents[1] / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql"))
GOAL = "Найди квартиры в аренду в Мадриде"
LISTING = "https://www.idealista.com/inmueble/98765432/"
CHILDREN = ["https://www.fotocasa.es/es/comprar/terreno/boadilla-del-monte/sin-urbanizar/183456789/d?from=list",
            "https://www.fotocasa.es/es/comprar/terreno/pozuelo-de-alarcon/urbanizable/183456790/d"]


def rent_page(title: str) -> str:
    return f"""<html><head><title>{title}</title></head><body><main>
<h1>{title}</h1><p>Piso en alquiler en Madrid centro, dos habitaciones, 1200 EUR al mes, disponible ya.
Contacto por teléfono con la agencia, visitas de lunes a viernes.</p></main></body></html>"""


PAGES = {LISTING: rent_page("Piso en alquiler en Sol, Madrid"), INDEX_URL: INDEX_HTML,
         CHILDREN[0]: rent_page("Piso en alquiler en Boadilla, Madrid"),
         CHILDREN[1]: rent_page("Piso en alquiler en Pozuelo, Madrid")}


@pytest.fixture
async def pool():
    import asyncpg

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=6)
    try:
        yield pool
    finally:
        await pool.close()


async def new_campaign(pool) -> str:
    campaigns = PostgresCampaignStore(pool)
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=OPERATOR, requested_by=OPERATOR, source_text=GOAL,
                                 actor="test")
    await campaigns.set_state(cid, "running", "test")
    return cid


def web_worker(pool, fetcher: FakeFetcher, queries: list[str]) -> WebSearchWorker:
    searcher = FakeSearcher(default=[LISTING, LISTING + "?utm_source=bing", INDEX_URL])
    return WebSearchWorker(PostgresCampaignStore(pool), PostgresWebStore(pool), searcher, fetcher, ListGenerator(queries),
                           config=WebSearchConfig(max_links_per_index=2, query_reuse_hours=1, cover_portals=False))


async def until_done(worker: WebSearchWorker, campaign_id: str) -> None:
    for _ in range(40):
        await worker.tick()
        run = await worker.store.get_run(campaign_id)
        if run is not None and run.state != "searching":
            return
    raise AssertionError("web stage did not finish")


async def test_web_pages_become_campaign_findings_streamed_once_with_their_links(pool) -> None:
    cid = await new_campaign(pool)
    fetcher = FakeFetcher(PAGES)
    worker = web_worker(pool, fetcher, ["piso alquiler Madrid", "site:idealista.com apartamento Madrid"])
    assert (await worker.store.web_status(cid)).active  # not started yet: the campaign waits for it
    await until_done(worker, cid)

    # each page once: the tracking-parameter spelling of LISTING and the second query's results are duplicates
    assert fetcher.fetched == [LISTING, INDEX_URL, *CHILDREN]
    rows = await pool.fetch(
        """select p.canonical_url, s.platform, s.canonical_url as site, b.campaign_id::text as campaign, p.state
             from collected_posts p join monitoring_sources s on s.id = p.source_id
             join acquisition_runs r on r.id = p.acquisition_run_id
             join acquisition_batch_items i on i.id = r.batch_item_id
             join acquisition_batches b on b.id = i.batch_id order by p.collected_at, p.canonical_url""")
    assert sorted(r["canonical_url"] for r in rows) == sorted([LISTING, *CHILDREN])
    assert {r["platform"] for r in rows} == {"website"} and {r["campaign"] for r in rows} == {cid}
    assert {r["site"] for r in rows} == {"https://idealista.com/", "https://fotocasa.es/"}
    assert {r["state"] for r in rows} == {"normalised"}
    batch = await pool.fetchrow("select platform, acquisition_method, state, campaign_id::text as c from acquisition_batches")
    assert tuple(batch) == ("website", "scrapling", "succeeded", cid)
    assert [r["state"] for r in await pool.fetch("select state from acquisition_batch_items order by sequence_no")] == [
        "succeeded", "succeeded"]
    assert await pool.fetchval("select count(*) from acquisition_runs where state = 'succeeded'") == 4
    assert await pool.fetchval("select count(*) from web_seen_urls where state = 'fetched'") == 4
    assert await pool.fetchval("select kind from web_seen_urls where url_key = $1", url_key(INDEX_URL)) == "index"
    assert dict(await pool.fetch("select state, count(*) from web_campaign_urls group by state")) == {"fetched": 4}
    assert [r[0] for r in await pool.fetch("select state from web_search_queries order by created_at")] == [
        "searched", "searched"]
    assert tuple(await pool.fetchrow("select state, stop_reason from web_search_runs")) == ("done", "queries_exhausted")
    assert not (await worker.store.web_status(cid)).active

    # the existing analysis worker analyses them; campaign findings never go to the digest
    digest = Telegram()
    result = await analyse(None, AnalysisPipeline(FakeAnalyzer()), digest.send, analysis_settings())
    assert len(result["findings"]) == 3 and digest.messages == []

    messenger = FakeMessenger()
    runner = CampaignRunner(PostgresCampaignStore(pool), PostgresRunStore(pool, SafetyLimits()), messenger,
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=0), owner_ids={OPERATOR},
                            web=PostgresWebStore(pool))
    await runner.tick()
    await runner.tick()
    cards = messenger.findings()
    assert len(cards) == 3
    assert sorted(next(u for u in [LISTING, *CHILDREN] if u in card) for card in cards) == sorted([LISTING, *CHILDREN])
    assert await pool.fetchval("select state from campaigns") == "completed"
    sent = len(messenger.sent)
    await runner.tick()
    assert len(messenger.sent) == sent  # never twice

    # the summary, once: what each site gave, from both stores
    counts = {(c.platform, c.name): (c.posts, c.relevant, c.sent) for c in
              await PostgresRunStore(pool, SafetyLimits()).source_counts(cid)}
    assert counts == {("website", "idealista.com"): (1, 1, 1), ("website", "fotocasa.es"): (2, 2, 2)}
    reports = {r.host: r for r in await PostgresWebStore(pool).site_report(cid)}
    assert (reports["idealista.com"].queries, reports["idealista.com"].links, reports["idealista.com"].read) == (1, 1, 1)
    assert (reports["fotocasa.es"].links, reports["fotocasa.es"].read) == (3, 3)  # the index page and its two ads
    [summary] = messenger.summaries()
    assert "Idealista — 1 ссылка в поиске · прочитано 1 → 1 объявление" in summary.splitlines()
    assert "Fotocasa — 3 ссылки в поиске · прочитано 3 → 2 объявления" in summary.splitlines()
    assert await pool.fetchval("select summary_sent_at is not null from campaign_runs where campaign_id = $1::uuid", cid)

    # another campaign meets the same pages: nothing is fetched again
    other = await new_campaign(pool)
    await until_done(web_worker(pool, fetcher, ["pisos en alquiler Madrid centro"]), other)
    assert fetcher.fetched == [LISTING, INDEX_URL, *CHILDREN]
    assert dict(await pool.fetch(
        "select state, count(*) from web_campaign_urls where campaign_id = $1::uuid group by state", other)) == {
        "duplicate": 2}
    assert await pool.fetchval("select count(*) from collected_posts") == 3


async def test_a_crashed_fetch_is_recovered_and_one_site_is_read_one_page_at_a_time(pool) -> None:
    cid = await new_campaign(pool)
    store = PostgresWebStore(pool)
    await store.start_run(cid)
    first = QueuedUrl(LISTING, url_key(LISTING), "idealista.com", 0, "listing")
    second = QueuedUrl("https://www.idealista.com/inmueble/11112222/", url_key("https://www.idealista.com/inmueble/11112222/"),
                       "idealista.com", 0, "listing")
    for url in (first, second):
        await pool.execute(
            "insert into web_campaign_urls (campaign_id, url_key, url, host, kind) values ($1::uuid, $2, $3, $4, 'listing')",
            cid, url.url_key, url.url, url.host)
    ticket = await store.begin_fetch(cid, first, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60)
    assert not isinstance(ticket, str)
    # one running page per site: the second waits (and stays queued)
    assert await store.begin_fetch(cid, second, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60) == BUSY
    assert await pool.fetchval("select state from web_campaign_urls where url_key = $1", second.url_key) == "queued"
    # the same URL for another campaign while it is being read: a duplicate
    other = await new_campaign(pool)
    await pool.execute(
        "insert into web_campaign_urls (campaign_id, url_key, url, host) values ($1::uuid, $2, $3, $4)",
        other, first.url_key, first.url, first.host)
    assert await store.begin_fetch(other, first, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60) in (
        BUSY, DUPLICATE)
    await store.finish_fetch(ticket, PageResult(False, "listing", LISTING, error="http_404"))
    assert await store.begin_fetch(other, first, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60) == DUPLICATE
    assert await pool.fetchval("select state from web_campaign_urls where campaign_id = $1::uuid", other) == "duplicate"

    # the worker "crashed" holding `second`: its run is failed by recover(), its claim taken over after the lease
    ticket2 = await store.begin_fetch(cid, second, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60)
    assert not isinstance(ticket2, str)
    assert await store.recover(0) == 1
    assert await pool.fetchval("select error_code from acquisition_runs where id = $1::uuid", ticket2.run_id) == "worker_lost"
    retry = await store.begin_fetch(cid, second, vertical="real_estate", lease_seconds=0, max_runtime_seconds=60)
    assert not isinstance(retry, str) and retry.run_id != ticket2.run_id

    # an operator pausing the site's source stops the web stage from reading it
    await pool.execute("update monitoring_sources set state = 'paused' where canonical_url = 'https://idealista.com/'")
    third = QueuedUrl("https://www.idealista.com/inmueble/33334444/", url_key("https://www.idealista.com/inmueble/33334444/"),
                      "idealista.com", 0, "listing")
    await pool.execute(
        "insert into web_campaign_urls (campaign_id, url_key, url, host) values ($1::uuid, $2, $3, $4)",
        cid, third.url_key, third.url, third.host)
    assert await store.begin_fetch(cid, third, vertical="real_estate", lease_seconds=300,
                                   max_runtime_seconds=60) == "source_unavailable"
    assert await pool.fetchval("select state from web_campaign_urls where url_key = $1", third.url_key) == "skipped"


async def test_a_full_web_batch_is_closed_and_the_next_site_opens_a_new_one(pool) -> None:
    cid = await new_campaign(pool)
    store = PostgresWebStore(pool)
    await store.start_run(cid)

    for n in range(21):
        url = f"https://agencia{n}.es/inmueble/venta-{7000000 + n}"
        queued = QueuedUrl(url, url_key(url), f"agencia{n}.es", 0, "listing")
        await pool.execute("insert into web_campaign_urls (campaign_id, url_key, url, host) values ($1::uuid, $2, $3, $4)",
                           cid, queued.url_key, url, queued.host)
        ticket = await store.begin_fetch(cid, queued, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60)
        await store.finish_fetch(ticket, PageResult(True, "listing", url, "t", f"Piso en venta en Madrid número {n}" * 5))
    batches = await pool.fetch("select state, (select count(*) from acquisition_batch_items i where i.batch_id = b.id) as n "
                               "from acquisition_batches b order by created_at")
    assert [(b["state"], b["n"]) for b in batches] == [("succeeded", 20), ("running", 1)]
    await store.finish(cid, "stopped", "campaign_cancelled")
    assert [r[0] for r in await pool.fetch("select state from acquisition_batches order by created_at")] == [
        "succeeded", "cancelled"]
    assert await pool.fetchval("select count(*) from collected_posts") == 21


async def test_a_site_that_refuses_bots_gives_posts_from_its_search_results(pool) -> None:
    cid = await new_campaign(pool)
    refusing = [f"https://www.idealista.com/inmueble/{70000000 + n}/" for n in range(5)]
    title, snippet = ("Terreno en venta en Boadilla del Monte - idealista",
                      "Terreno urbanizable de 1.200 m² en Boadilla del Monte, Madrid. 480.000 €. Todos los servicios.")
    searcher = FakeSearcher(default=refusing, texts={u: (title, snippet) for u in refusing})
    fetcher = FakeFetcher(errors={u: "http_403" for u in refusing})
    worker = WebSearchWorker(PostgresCampaignStore(pool), PostgresWebStore(pool), searcher, fetcher,
                             ListGenerator(["terreno Boadilla Madrid"]),
                             config=WebSearchConfig(pages_per_tick=1, cover_portals=False))
    await until_done(worker, cid)
    assert fetcher.fetched == refusing[:3]  # three refusals block the site; the rest are never asked
    rows = await pool.fetch("select canonical_url, body_text, raw_payload->>'via' as via from collected_posts")
    assert sorted(r["canonical_url"] for r in rows) == sorted(refusing)
    assert all(r["via"] == "search" and "480.000 €" in r["body_text"] for r in rows)
    assert dict(await pool.fetch("select detail, count(*) from web_campaign_urls group by detail")) == {
        "search_snippet": 5}
    assert await pool.fetchval("select blocked_until > now() from web_hosts where host = 'idealista.com'")
    assert await pool.fetchval("select search_title from web_campaign_urls limit 1") == title


async def test_another_campaign_may_repeat_a_query_unless_reuse_hours_say_otherwise(pool) -> None:
    from bot.web_search.models import GeneratedQuery

    store = PostgresWebStore(pool)
    first, second, third = await new_campaign(pool), await new_campaign(pool), await new_campaign(pool)
    query = [GeneratedQuery("terreno Boadilla Madrid", "es")]
    assert await store.add_queries(first, 1, query, reuse_hours=0) == 1
    await pool.execute("update web_search_queries set state = 'searched', searched_at = now()")
    assert await store.add_queries(second, 1, query, reuse_hours=0) == 1      # no cross-campaign block
    assert await store.add_queries(second, 2, query, reuse_hours=0) == 0      # its own repeat: unique key
    assert await store.add_queries(third, 1, query, reuse_hours=72) == 0      # blocked when > 0
    assert await pool.fetchval("select state from web_search_queries where campaign_id = $1::uuid", third) == "skipped"
    assert await pool.fetchval("select state from web_search_queries where campaign_id = $1::uuid", second) == "pending"


async def test_an_index_page_is_queued_again_after_its_ttl_and_a_listing_never(pool) -> None:
    from bot.web_search.models import Candidate

    store = PostgresWebStore(pool)
    first, second = await new_campaign(pool), await new_campaign(pool)
    await store.start_run(first)
    index = QueuedUrl(INDEX_URL, url_key(INDEX_URL), "fotocasa.es", 0, "index")
    listing = QueuedUrl(LISTING, url_key(LISTING), "idealista.com", 0, "listing")
    for url in (index, listing):
        await store.enqueue(first, [Candidate(url.url, url.url_key, url.host, 0, url.kind)])
        ticket = await store.begin_fetch(first, url, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60)
        assert not isinstance(ticket, str)
        await store.finish_fetch(ticket, PageResult(True, url.kind, url.url, "t", "" if url.kind == "index" else "x" * 200))
    assert await pool.fetchval("select kind from web_seen_urls where url_key = $1", index.url_key) == "index"

    def candidates(cid: str) -> list[Candidate]:
        return [Candidate(u.url, u.url_key, u.host, 0, u.kind) for u in (index, listing)]

    # fresh: both are duplicates for another campaign
    assert await store.enqueue(second, candidates(second)) == 0
    await pool.execute("delete from web_campaign_urls where campaign_id = $1::uuid", second)
    # 8 days later: the index page is queued, the listing still a duplicate
    await pool.execute("update web_seen_urls set finished_at = now() - interval '8 days'")
    assert await store.enqueue(second, candidates(second)) == 1
    states = dict(await pool.fetch("select kind, state from web_campaign_urls where campaign_id = $1::uuid", second))
    assert states == {"index": "queued", "listing": "duplicate"}
    ticket = await store.begin_fetch(second, index, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60)
    assert not isinstance(ticket, str)
    assert await pool.fetchval("select state from web_seen_urls where url_key = $1", index.url_key) == "fetching"
    await store.finish_fetch(ticket, PageResult(True, "index", INDEX_URL, "t"))
    # the TTL disabled (0): never again
    await pool.execute("update web_seen_urls set finished_at = now() - interval '30 days'")
    third = await new_campaign(pool)
    assert await store.enqueue(third, candidates(third), index_ttl_days=0) == 0
    assert await store.begin_fetch(third, index, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60,
                                   index_ttl_days=0) == DUPLICATE


async def test_renders_are_counted_in_the_database(pool) -> None:
    from bot.web_search.models import Candidate

    store = PostgresWebStore(pool)
    cid, other = await new_campaign(pool), await new_campaign(pool)
    assert await store.renders_used(cid) == 0
    await store.enqueue(cid, [Candidate(LISTING, url_key(LISTING), "idealista.com", 0, "listing"),
                              Candidate(INDEX_URL, url_key(INDEX_URL), "fotocasa.es", 0, "index")])
    await store.mark_rendered(cid, url_key(LISTING))
    await store.mark_rendered(cid, url_key(LISTING))  # idempotent
    assert await store.renders_used(cid) == 1 and await store.renders_used(other) == 0
    await store.mark_rendered(cid, url_key(INDEX_URL))
    assert await PostgresWebStore(pool).renders_used(cid) == 2  # survives a new store (a restart)


async def test_scrape_api_reads_are_counted_in_the_database(pool) -> None:
    from bot.web_search.models import Candidate

    store = PostgresWebStore(pool)
    cid = await new_campaign(pool)
    await store.enqueue(cid, [Candidate(LISTING, url_key(LISTING), "idealista.com", 0, "listing")])
    assert await store.scrapes_used(cid) == 0
    await store.mark_scraped(cid, url_key(LISTING))
    await store.mark_scraped(cid, url_key(LISTING))
    assert await PostgresWebStore(pool).scrapes_used(cid) == 1


async def test_host_blocks_are_tracked_per_layer(pool) -> None:
    store = PostgresWebStore(pool)
    cid = await new_campaign(pool)
    urls = [QueuedUrl(f"https://www.idealista.com/inmueble/{80000000 + n}/", url_key(f"https://www.idealista.com/inmueble/{80000000 + n}/"),
                      "idealista.com", 0, "listing") for n in range(5)]
    assert await store.layer_state("idealista.com") == {"http": True, "render": True}  # unknown host
    for url in urls[:3]:  # three HTTP refusals; the page was then read by the browser
        ticket = await store.begin_fetch(cid, url, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60,
                                         render_layer=True)
        assert not isinstance(ticket, str)
        await store.layer_refused(url.host, "http")
        await store.finish_fetch(ticket, PageResult(True, "listing", url.url, "t", f"Piso {url.url} " * 20, layer="render"))
    row = await pool.fetchrow("select * from web_hosts where host = 'idealista.com'")
    assert row["http_refusals"] == 3 and row["http_blocked_until"] is not None
    assert row["render_refusals"] == 0 and row["render_blocked_until"] is None and row["blocked_until"] is None
    assert row["pages_fetched"] == 3
    assert await store.layer_state("idealista.com") == {"http": False, "render": True}
    # not blocked for the browser: a fetch is still given out
    ticket = await store.begin_fetch(cid, urls[3], vertical="real_estate", lease_seconds=300, max_runtime_seconds=60,
                                     render_layer=True)
    assert not isinstance(ticket, str)
    # three browser refusals block that layer too, and now the host is blocked altogether
    for _ in range(3):
        await store.finish_fetch(ticket, PageResult(False, "listing", urls[3].url, error="render_blocked", layer="render"))
    row = await pool.fetchrow("select * from web_hosts where host = 'idealista.com'")
    assert row["render_refusals"] == 3 and row["render_blocked_until"] is not None and row["blocked_until"] is not None
    assert await store.layer_state("idealista.com") == {"http": False, "render": False}
    assert await store.begin_fetch(cid, urls[4], vertical="real_estate", lease_seconds=300, max_runtime_seconds=60,
                                   render_layer=True) == "host_blocked"
    # without the browser layer the host was blocked after the HTTP refusals alone
    assert await store.begin_fetch(cid, urls[4], vertical="real_estate", lease_seconds=300, max_runtime_seconds=60) == "host_blocked"
    # the scrape API has no block: with it enabled the host is never host_blocked
    assert not isinstance(await store.begin_fetch(cid, urls[4], vertical="real_estate", lease_seconds=300,
                                                  max_runtime_seconds=60, render_layer=True, scrape_layer=True), str)


async def test_http_only_refusals_set_the_legacy_block_without_a_browser_layer(pool) -> None:
    store = PostgresWebStore(pool)
    cid = await new_campaign(pool)
    for n in range(3):
        url = f"https://www.fotocasa.es/es/comprar/vivienda/madrid/{90000000 + n}/d"
        queued = QueuedUrl(url, url_key(url), "fotocasa.es", 0, "listing")
        ticket = await store.begin_fetch(cid, queued, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60)
        assert not isinstance(ticket, str)
        await store.finish_fetch(ticket, PageResult(False, "listing", url, error="http_403"))
    row = await pool.fetchrow("select * from web_hosts where host = 'fotocasa.es'")
    assert row["http_blocked_until"] is not None and row["blocked_until"] is not None
    assert row["consecutive_refusals"] == 3
    assert await store.layer_state("fotocasa.es") == {"http": False, "render": True}


async def test_migration_034_carries_an_earlier_block_over_to_both_layers(pool) -> None:
    await pool.execute("insert into web_hosts (host, blocked_until) values ('old.example', now() + interval '5 hours')")
    sql = next(p for p in MIGRATIONS if p.name.startswith("034_")).read_text(encoding="utf-8")
    await pool.execute(sql)  # idempotent: applying it again after the data exists
    row = await pool.fetchrow("select http_blocked_until, render_blocked_until, blocked_until from web_hosts where host = 'old.example'")
    assert row["http_blocked_until"] == row["render_blocked_until"] == row["blocked_until"]
