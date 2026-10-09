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
from bot.web_search.models import Candidate, FetchTicket, PageResult, QueuedUrl
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
    plan = plan_campaign(GOAL)
    plan.constraints["deal"] = None  # the shared fixture index lists /comprar/ links: no deal filter here
    cid = await campaigns.create(plan, chat_id=OPERATOR, requested_by=OPERATOR, source_text=GOAL,
                                 actor="test")
    await campaigns.set_state(cid, "running", "test")
    return cid


async def test_structured_api_listing_uses_the_existing_post_and_campaign_path(pool) -> None:
    cid = await new_campaign(pool)
    store = PostgresWebStore(pool)
    await store.start_run(cid)
    queued = QueuedUrl(LISTING, url_key(LISTING), "idealista.com", 0, "listing")
    assert await store.enqueue(cid, [Candidate(queued.url, queued.url_key, queued.host, kind="listing")]) == 1
    # An existing HTML block is left intact by importing a ready structured result.
    await pool.execute(
        """insert into web_hosts (host, http_refusals, render_refusals, consecutive_refusals,
                                 http_blocked_until, render_blocked_until, blocked_until)
           values ('idealista.com', 3, 4, 3, now() + interval '1 hour',
                   now() + interval '2 hours', now() + interval '1 hour')""")
    before = await pool.fetchrow(
        """select http_refusals, render_refusals, consecutive_refusals,
                  http_blocked_until, render_blocked_until, blocked_until
             from web_hosts where host = 'idealista.com'""")
    ticket = await store.begin_fetch(cid, queued, vertical="real_estate", lease_seconds=300,
                                     max_runtime_seconds=60, contact_site=False)
    assert isinstance(ticket, FetchTicket)
    text = 'JSON-LD: {"price": 180000, "plot_m2": 2500, "deal": "sale"}\nTerreno en Madrid'
    post_id = await store.finish_fetch(
        ticket, PageResult(True, final_url=LISTING, title="Terreno en Madrid", text=text, via="api", layer="api"))
    assert post_id is not None
    row = await pool.fetchrow(
        """select p.id::text, p.canonical_url, p.body_text, p.state, p.raw_payload->>'via' as via,
                  p.raw_payload->>'campaign_id' as campaign, b.campaign_id::text as linked_campaign,
                  s.platform, r.state as run_state
             from collected_posts p join monitoring_sources s on s.id = p.source_id
             join acquisition_runs r on r.id = p.acquisition_run_id
             join acquisition_batch_items i on i.id = r.batch_item_id
             join acquisition_batches b on b.id = i.batch_id where p.id = $1::uuid""", post_id)
    assert tuple(row) == (post_id, LISTING, text, "normalised", "api", cid, cid, "website", "succeeded")
    assert tuple(await pool.fetchrow(
        "select state, layer from web_campaign_urls where campaign_id = $1::uuid and url_key = $2",
        cid, queued.url_key)) == ("fetched", "api")
    assert tuple(await pool.fetchrow(
        "select state, post_id::text from web_seen_urls where url_key = $1", queued.url_key)) == ("fetched", post_id)
    assert await pool.fetchrow(
        """select http_refusals, render_refusals, consecutive_refusals,
                  http_blocked_until, render_blocked_until, blocked_until
             from web_hosts where host = 'idealista.com'""") == before


async def test_listing_source_migration_upgrades_populated_039_and_is_idempotent(pool) -> None:
    import asyncpg

    # Start from the actual old schema, with existing rows; do not simulate its CHECK.
    await pool.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        if path.name[:3] <= "039":
            await pool.execute(path.read_text(encoding="utf-8"))
    cid = await new_campaign(pool)
    old_layers = [None, "http", "render", "scrape", "none"]
    for n, layer in enumerate(old_layers):
        url = f"https://www.idealista.com/inmueble/{90000000 + n}/"
        await pool.execute(
            """insert into web_campaign_urls (campaign_id, url_key, url, host, layer)
               values ($1::uuid, $2, $3, 'idealista.com', $4)""", cid, url_key(url), url, layer)
    with pytest.raises(asyncpg.CheckViolationError):
        await pool.execute("update web_campaign_urls set layer = 'api' where layer = 'http'")
    for path in MIGRATIONS:
        if path.name[:3] > "039":
            await pool.execute(path.read_text(encoding="utf-8"))
    assert [r[0] for r in await pool.fetch("select layer from web_campaign_urls order by url")] == old_layers
    await pool.execute("update web_campaign_urls set layer = 'api' where layer = 'http'")
    sql = next(p for p in MIGRATIONS if p.name == "043_listing_sources.sql").read_text(encoding="utf-8")
    for _ in range(2):
        await pool.execute(sql)
        assert [r[0] for r in await pool.fetch("select layer from web_campaign_urls order by url")] == [
            None, "api", "render", "scrape", "none"]
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("update web_campaign_urls set layer = 'unknown'")
        # A different CHECK on the same table remains enforced.
        with pytest.raises(asyncpg.CheckViolationError):
            await pool.execute("update web_campaign_urls set state = 'unknown'")


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


# --- human verification of a website (migration 041) --------------------------------------------------------------


class ChallengeRenderer:
    """The browser: every page of idealista.com is a challenge page (raised as the real renderer does)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.passed = False  # a person passed the check: the page is a listing now

    async def render(self, url: str):
        from bot.web_search.render import ChallengeDetected, RenderedPage

        self.calls.append(url)
        if self.passed:
            return RenderedPage(url, "Piso en alquiler en Sol, Madrid", rent_page("Piso en alquiler en Sol, Madrid"))
        raise ChallengeDetected("captcha", url, "idealista.com")


async def test_a_challenge_opens_one_verification_job_the_flow_hands_to_a_person_and_the_site_is_read_after(pool) -> None:
    from bot.verification.models import Recovery
    from bot.verification.service import FlowConfig, VerificationService
    from bot.verification.store import PostgresVerificationStore
    from tests.test_live_view import TOKEN, init_data
    from tests.test_verification_flow import OWNER, FakeLive, FakeNotifier, FakeWatchdog, token_of

    cid = await new_campaign(pool)
    store = PostgresWebStore(pool)
    renderer = ChallengeRenderer()
    fetcher = FakeFetcher(errors={LISTING: "http_403"})
    worker = WebSearchWorker(PostgresCampaignStore(pool), store, FakeSearcher(default=[LISTING]), fetcher,
                             ListGenerator(["piso alquiler Madrid"]), renderer=renderer,
                             config=WebSearchConfig(cover_portals=False, human_verification=True))
    for _ in range(6):
        await worker.tick()

    # exactly one job: the site's source, the render profile (a browser_profiles row), the challenged page
    [job] = await pool.fetch(
        """select j.id::text, j.state, j.job_type, j.target_url, j.requested_by, s.canonical_url, s.platform,
                  p.profile_name, p.platform as profile_platform
             from verification_jobs j join monitoring_sources s on s.id = j.source_id
             join browser_profiles p on p.id = j.browser_profile_id""")
    assert (job["state"], job["job_type"], job["target_url"], job["canonical_url"], job["platform"]) == (
        "requested", "web_challenge", LISTING, "https://idealista.com/", "website")
    assert (job["profile_name"], job["profile_platform"]) == ("web-search-render", "website")
    assert len(renderer.calls) == 1  # the site was asked once
    assert await store.open_verification("idealista.com", "captcha", LISTING) == job["id"]  # deduped
    assert await pool.fetchval("select count(*) from verification_jobs") == 1
    # the URL went back to the queue unread: no run left running, no claim, no refusal, no budget
    assert await pool.fetchval("select state from web_campaign_urls where url = $1", LISTING) == "queued"
    assert await pool.fetchval("select count(*) from acquisition_runs where state = 'running'") == 0
    assert await pool.fetchval("select count(*) from web_seen_urls") == 0
    assert dict(await pool.fetchrow("select http_refusals, render_refusals from web_hosts where host = 'idealista.com'")) == {
        "http_refusals": 0, "render_refusals": 0}
    assert await store.renders_used(cid) == 0
    assert await store.verification_waiting(cid) == ["idealista.com"]
    assert (await store.web_status(cid)).verification == ("idealista.com",)
    assert (await store.host_verification("idealista.com", cid)).state == "open"
    assert (await worker.store.get_run(cid)).state == "searching"  # the stage waits

    # the existing verification service announces it, a person claims it, the watchdog confirms a clean page
    verification = PostgresVerificationStore(URL)
    await verification.connect()
    try:
        notifier, live = FakeNotifier(), FakeLive()
        service = VerificationService(verification, live, FakeWatchdog(Recovery(True)), notifier,
                                      FlowConfig(public_url="https://1-2-3-4.sslip.io",
                                                 operator_ids=frozenset({OWNER, OPERATOR}), owner_id=OWNER,
                                                 bot_token=TOKEN))
        await service.tick()
        [_, text, button] = next(m for m in notifier.sent if m[0] == OPERATOR)
        assert "Сайт idealista.com просит пройти проверку" in text and "«Готово»" in text
        announced = await verification.get_job(job["id"])
        assert announced.challenge_kind == "captcha" and not announced.sensitive and announced.platform == "website"
        session = (await service.open(token_of(button[1]), init_data(OPERATOR))).session
        await service.claim(session)
        await service.view(session)
        assert live.started == [f"{announced.profile_id}@{LISTING}"]
        assert await service.solve(session) is True
        assert (await store.host_verification("idealista.com", cid)).state == "verified"
        assert (await verification.get_job(job["id"])).resumed_at is not None
    finally:
        await verification.close()

    # the site is read through the browser profile now (no plain HTTP), and the stage finishes
    renderer.passed = True
    fetched = list(fetcher.fetched)
    for _ in range(8):
        await worker.tick()
        if (await worker.store.get_run(cid)).state != "searching":
            break
    assert (await worker.store.get_run(cid)).state == "done"
    assert fetcher.fetched == fetched and renderer.calls[-1] == LISTING
    assert await pool.fetchval("select layer from web_campaign_urls where url = $1", LISTING) == "render"
    assert await pool.fetchval("select count(*) from collected_posts where canonical_url = $1", LISTING) == 1


async def test_an_unsolved_web_job_expires_and_the_site_is_reported(pool) -> None:
    cid = await new_campaign(pool)
    store = PostgresWebStore(pool)
    worker = WebSearchWorker(PostgresCampaignStore(pool), store, FakeSearcher(default=[LISTING]),
                             FakeFetcher(errors={LISTING: "http_403"}), ListGenerator(["piso alquiler Madrid"]),
                             renderer=ChallengeRenderer(), config=WebSearchConfig(cover_portals=False, human_verification=True))
    for _ in range(6):
        await worker.tick()
    await pool.execute("update verification_jobs set state = 'expired', resolved_at = now() where job_type = 'web_challenge'")
    assert (await store.host_verification("idealista.com", cid)).state == "unsolved"
    for _ in range(6):
        await worker.tick()
    assert (await worker.store.get_run(cid)).state == "done"
    assert await pool.fetchval("select detail from web_campaign_urls where url = $1", LISTING) == "verification_expired"
    [report] = [r for r in await store.site_report(cid) if r.host == "idealista.com"]
    assert report.unverified and report.refused == 1


async def test_an_open_web_job_expires_by_itself_and_goes_when_no_campaign_waits_for_the_site(pool) -> None:
    from bot.verification.store import PostgresVerificationStore

    cid = await new_campaign(pool)
    store = PostgresWebStore(pool, job_hours=5, human_verification=True)
    verification = PostgresVerificationStore(URL)
    await verification.connect()
    try:
        worker = WebSearchWorker(PostgresCampaignStore(pool), store, FakeSearcher(default=[LISTING]),
                                 FakeFetcher(errors={LISTING: "http_403"}), ListGenerator(["piso alquiler Madrid"]),
                                 renderer=ChallengeRenderer(), cancel_job=verification.cancel,
                                 config=WebSearchConfig(cover_portals=False, human_verification=True))
        for _ in range(6):
            await worker.tick()
        # not announced yet, still it carries an expiry: the job's lifetime from VERIFICATION_JOB_HOURS
        hours = await pool.fetchval(
            "select extract(epoch from expires_at - requested_at) / 3600 from verification_jobs where job_type = 'web_challenge'")
        assert round(float(hours)) == 5
        assert await store.idle_verification_jobs() == []  # the campaign still has the site's URL queued
        campaign = await PostgresCampaignStore(pool).get(cid)
        await worker._done(campaign, "time_cap")  # the stage ends: nobody waits for the check any more
        assert await pool.fetchval("select state from verification_jobs where job_type = 'web_challenge'") == "cancelled"
    finally:
        await verification.close()


async def test_the_web_status_skips_the_verification_query_when_it_is_off(pool) -> None:
    cid = await new_campaign(pool)
    on, off = PostgresWebStore(pool), PostgresWebStore(pool, human_verification=False)
    worker = WebSearchWorker(PostgresCampaignStore(pool), on, FakeSearcher(default=[LISTING]),
                             FakeFetcher(errors={LISTING: "http_403"}), ListGenerator(["piso alquiler Madrid"]),
                             renderer=ChallengeRenderer(), config=WebSearchConfig(cover_portals=False, human_verification=True))
    for _ in range(6):
        await worker.tick()
    assert (await on.web_status(cid)).verification == ("idealista.com",)
    assert (await off.web_status(cid)).verification == ()


async def test_the_render_profile_is_never_a_collector_profile(pool) -> None:
    from bot.orchestra.store import ready_profile

    store = PostgresWebStore(pool)
    await store.render_profile()
    async with pool.acquire() as conn:
        with pytest.raises(ValueError):
            await ready_profile(conn, "website")
        await conn.execute(
            """insert into browser_profiles (profile_name, platform, storage_locator, state)
               values ('website-main', 'website', 'volume:browser_profiles', 'ready')""")
        own = await conn.fetchval("select id::text from browser_profiles where profile_name = 'website-main'")
        assert await ready_profile(conn, "website") == own
