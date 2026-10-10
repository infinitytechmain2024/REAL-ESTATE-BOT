"""Provider imports survive worker restarts without repeating a paid launch."""

from __future__ import annotations

import json

import pytest

from bot.campaign import MemoryCampaignStore
from bot.utils import costs
from bot.web_search.models import GeneratedQuery
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources import SourceListing
from bot.web_search.sources.apify import TemporaryApifyError, _execution
from bot.web_search.store import MemoryWebStore
from bot.web_search.worker import WebSearchConfig, WebSearchWorker
from tests.test_web_search import FakeFetcher, FakeSearcher, ListGenerator, campaign, run_until_done


@pytest.fixture
def source_ledger():
    previous, budget = costs.ledger(), costs.budget()
    sink = costs.MemoryLedger()
    costs.install(sink, 1)
    try:
        yield sink
    finally:
        costs.install(previous, budget)


class FakeSource:
    name = "apify_idealista"
    hosts = frozenset({"idealista.com"})
    max_items = 20
    max_charge_usd = .10
    actor_id = "fixture"

    def __init__(self, *, rows=2, fail_once=False, supported=True):
        self.rows, self.fail_once, self.supported = rows, fail_once, supported
        self.launches, self.calls, self.caps = 0, 0, []

    def supports(self, task):
        return self.supported

    async def search(self, task, *, limit):
        context = _execution.get()
        assert context is not None
        self.calls += 1
        self.caps.append(context.max_charge_usd)
        if context.launch_allowed:
            self.launches += 1
            await context.save_run("knownrun", "dataset")
        if self.fail_once:
            self.fail_once = False
            raise TemporaryApifyError("apify_http_503")
        await context.reconcile_usage("knownrun", .01)
        return [SourceListing(f"https://www.idealista.com/inmueble/{12345670 + n}/", "Terreno Madrid",
                              plot_m2=2200, area_m2=120, deal="sale", property_type="land",
                              description="x" * 9000) for n in range(min(self.rows, limit))]

    async def aclose(self):
        pass


def provider_worker(campaigns, store, source, **config):
    return WebSearchWorker(campaigns, store, FakeSearcher(), FakeFetcher(),
                           ListGenerator(["terreno venta Boadilla Madrid"]), sources=(source,),
                           config=WebSearchConfig(cover_portals=False, **config))


async def test_first_round_imports_source_before_html_and_restart_never_launches_twice(source_ledger):
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource()
    first = provider_worker(campaigns, store, source, pages_per_tick=1)
    await first.step(cid)
    assert source.launches == 1 and not store.posts
    restarted = provider_worker(campaigns, store, source, pages_per_tick=1)
    await restarted.step(cid)
    assert len(store.posts) == 1 and not restarted.fetcher.fetched
    await run_until_done(restarted, cid)
    assert source.launches == 1 and len(store.posts) == 2
    assert (await store.source_runs(cid))[0].state == "completed"
    for post in store.posts:
        assert post["via"] == "api" and len(post["text"]) <= restarted.config.max_post_chars
        data = json.loads(post["text"].splitlines()[0].removeprefix("JSON-LD: "))
        assert data["plot_m2"] == 2200 and data["area_m2"] == 120 and data["property_type"] == "landparcel"
    assert len([e for e in source_ledger.entries if e.kind == "cost"]) == 1


async def test_running_source_resumes_with_queries_already_generated(source_ledger):
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource()
    await store.start_run(cid)
    await store.claim_source(cid, source.name)
    await store.save_source_run(cid, source.name, "knownrun", "dataset")
    await store.add_queries(cid, 1, [GeneratedQuery("terreno venta Madrid")], reuse_hours=0)
    assert (await store.counts(cid)).queries > 0
    await run_until_done(provider_worker(campaigns, store, source), cid)
    assert source.calls == 1 and source.launches == 0 and len(store.posts) == 2


async def test_provider_transport_failure_keeps_run_metadata_and_allows_search(source_ledger):
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource(fail_once=True)
    await run_until_done(provider_worker(campaigns, store, source), cid)
    run, = await store.source_runs(cid)
    assert run.state == "failed" and run.run_id == "knownrun"
    assert source.calls == 1 and (await store.counts(cid)).pending_queries == 0
    assert any(e.code == "apify_http_503" for e in source_ledger.entries)


async def test_crashed_paid_run_is_booked_and_settled_when_campaign_expires(source_ledger):
    import asyncio
    from datetime import UTC, datetime, timedelta

    class CrashedSource(FakeSource):
        settlements = 0

        async def search(self, task, *, limit):
            await _execution.get().save_run("knownrun", "dataset")
            raise asyncio.CancelledError

        async def settle(self, context):
            assert context.run_id == "knownrun" and not context.launch_allowed
            self.settlements += 1
            await context.reconcile_usage("knownrun", .02)

    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), CrashedSource()
    worker = provider_worker(campaigns, store, source)
    with pytest.raises(asyncio.CancelledError):
        await worker.step(cid)
    assert await source_ledger.spent(cid) == .10
    assert source_ledger.entries[0].code == "estimated_pending_run"
    store.started[cid] = datetime.now(UTC) - timedelta(days=1)
    await worker.step(cid)
    assert source.settlements == 1 and not store.posts
    assert await source_ledger.spent(cid) == .02
    assert (await store.source_runs(cid))[0].error_code == "apify_time_cap"


async def test_definitive_post_429_does_not_book_a_charge(source_ledger):
    class RefusedSource(FakeSource):
        async def search(self, task, *, limit):
            raise TemporaryApifyError("apify_http_429")

    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), RefusedSource()
    await run_until_done(provider_worker(campaigns, store, source), cid)
    assert not [e for e in source_ledger.entries if e.kind == "cost"]
    assert any(e.code == "apify_http_429" for e in source_ledger.entries)


async def test_provider_outer_timeout_preserves_lease_margin_and_fallback(source_ledger, monkeypatch):
    import asyncio

    timeouts = []
    async def timed_out(coroutine, *, timeout):
        timeouts.append(timeout)
        coroutine.close()
        raise TimeoutError
    monkeypatch.setattr(asyncio, "wait_for", timed_out)
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource()
    await run_until_done(provider_worker(campaigns, store, source, lease_seconds=60), cid)
    assert timeouts == [30]
    assert (await store.source_runs(cid))[0].error_code == "apify_worker_timeout"
    assert (await store.counts(cid)).queries > 0


async def test_oversize_provider_snapshot_is_bounded_and_skips_recorded(source_ledger):
    class OversizeSource(FakeSource):
        async def search(self, task, *, limit):
            return await super().search(task, limit=100)

    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), OversizeSource(rows=100)
    await provider_worker(campaigns, store, source).step(cid)
    run, = await store.source_runs(cid)
    assert len(run.listings) == 12  # remaining per-host page allowance
    assert any(e.code == "apify_result_limit" and e.units == 88 for e in source_ledger.entries)


async def test_uncertain_starting_claim_falls_back_without_launch(source_ledger):
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource()
    await store.claim_source(cid, source.name)
    await run_until_done(provider_worker(campaigns, store, source), cid)
    assert source.launches == 0
    assert (await store.source_runs(cid))[0].error_code == "apify_launch_uncertain"
    assert any(e.code == "apify_launch_uncertain" for e in source_ledger.entries)


async def test_busy_api_import_keeps_offset_and_blocks_html_until_released(source_ledger):
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource(rows=1)
    w = provider_worker(campaigns, store, source)
    await w.step(cid)
    store.busy_hosts.add("idealista.com")
    await w.step(cid)
    assert (await store.source_runs(cid))[0].import_offset == 0
    assert not store.posts and not w.fetcher.fetched
    store.busy_hosts.clear()
    await run_until_done(w, cid)
    assert len(store.posts) == 1 and source.launches == 1


async def test_disabled_restart_marks_cached_source_failed_without_network_or_import(source_ledger):
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource()
    await provider_worker(campaigns, store, source).step(cid)
    w = WebSearchWorker(campaigns, store, FakeSearcher(), FakeFetcher(), ListGenerator(["terreno venta Madrid"]),
                        config=WebSearchConfig(cover_portals=False))
    await run_until_done(w, cid)
    run, = await store.source_runs(cid)
    assert run.state == "failed" and run.error_code == "apify_disabled_during_run"
    assert run.listings and not store.posts and source.calls == 1
    assert any(e.code == "apify_disabled_during_run" for e in source_ledger.entries)


async def test_changed_task_stops_known_run_without_resuming_provider(source_ledger):
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource()
    await store.start_run(cid)
    await store.claim_source(cid, source.name)
    await store.save_source_run(cid, source.name, "knownrun", "dataset")
    source.supported = False
    await run_until_done(provider_worker(campaigns, store, source), cid)
    assert source.calls == 0
    assert (await store.source_runs(cid))[0].error_code == "apify_task_changed"


async def test_source_rechecks_limits_and_reduces_provider_cap_to_remaining_budget(source_ledger):
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource(rows=10)
    await source_ledger.add(costs.Entry(cid, "llm", cost_usd=.95))
    await run_until_done(provider_worker(campaigns, store, source, max_pages_per_campaign=1), cid)
    assert source.caps == [pytest.approx(.05)]
    assert len(store.posts) == 1


async def test_unreadable_budget_disables_provider_and_preserves_search(source_ledger):
    async def unreadable(cid):
        raise RuntimeError("unavailable")
    source_ledger.spent = unreadable
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource()
    await run_until_done(provider_worker(campaigns, store, source), cid)
    assert source.launches == 0 and (await store.counts(cid)).queries > 0
    assert (await store.source_runs(cid))[0].error_code == "apify_launch_limits"


async def test_worker_imports_failed_url_via_api_and_reports_real_read(source_ledger):
    from bot.web_search.models import Candidate, PageResult

    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource(rows=1)
    candidate = Candidate("https://www.idealista.com/inmueble/12345670/", "previous-failure", "idealista.com", kind="listing")
    # Use the same canonical key the worker will generate.
    from bot.web_search.urls import url_key
    candidate = Candidate(candidate.url, url_key(candidate.url), candidate.host, kind="listing")
    await store.enqueue(cid, [candidate])
    queued, = await store.next_urls(cid, 1)
    ticket = await store.begin_fetch(cid, queued, vertical="real_estate", lease_seconds=300, max_runtime_seconds=60)
    await store.finish_fetch(ticket, PageResult(False, error="http_403"))
    worker = provider_worker(campaigns, store, source)
    await run_until_done(worker, cid)
    assert len(store.posts) == 1 and store.posts[0]["via"] == "api"
    assert not worker.fetcher.fetched and source.launches == 1
    assert store.hosts["idealista.com"]["fetched"] == 1
    assert store.hosts["idealista.com"]["http_refusals"] == 1
    assert worker.progress(cid).read == 1


async def test_paused_portal_does_not_start_paid_source(source_ledger):
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource()
    store.paused_hosts.add("idealista.com")
    await run_until_done(provider_worker(campaigns, store, source), cid)
    assert source.launches == 0 and not store.posts


def test_provider_settings_default_off_secret_hidden_and_limits_validated(monkeypatch):
    monkeypatch.delenv("APIFY_IDEALISTA_ENABLED", raising=False)
    settings = WebSearchSettings(_env_file=None, APIFY_TOKEN="secret-token")
    assert settings.sources() == () and "secret-token" not in repr(settings)
    with pytest.raises(ValueError):
        WebSearchSettings(_env_file=None, APIFY_IDEALISTA_MAX_RESULTS=51)
    with pytest.raises(ValueError):
        WebSearchSettings(_env_file=None, APIFY_IDEALISTA_TIMEOUT_SECONDS=121)


async def test_enabled_settings_construct_source_with_all_configured_limits():
    settings = WebSearchSettings(_env_file=None, APIFY_IDEALISTA_ENABLED=True, APIFY_TOKEN="secret-token",
                                APIFY_IDEALISTA_LOCATION_ID="verified-id", APIFY_IDEALISTA_MAX_RESULTS=7,
                                APIFY_IDEALISTA_MAX_CHARGE_USD=.03, APIFY_IDEALISTA_TIMEOUT_SECONDS=90)
    source, = settings.sources()
    try:
        assert source.max_items == 7 and source.max_charge_usd == .03 and source.timeout_seconds == 90
        assert source.location_id == "verified-id" and "secret-token" not in repr(source)
    finally:
        await source.aclose()


async def test_unsupported_source_and_later_round_never_launch(source_ledger):
    campaigns = MemoryCampaignStore()
    cid = await campaign(campaigns)
    store, source = MemoryWebStore(campaigns), FakeSource(supported=False)
    await run_until_done(provider_worker(campaigns, store, source), cid)
    assert not await store.source_runs(cid) and source.launches == 0
    other = await campaign(campaigns)
    await store.start_run(other)
    await store.add_queries(other, 1, [GeneratedQuery("terreno venta Madrid")], reuse_hours=0)
    source.supported = True
    await run_until_done(provider_worker(campaigns, store, source), other)
    assert source.launches == 0
