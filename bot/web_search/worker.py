"""The web stage of every open campaign, one bounded step per campaign per tick.

A step, under a per-campaign lease (so two workers never drive one campaign):

1. queued URLs  -> read up to ``pages_per_tick`` of them (site after site):
   robots.txt, the global "seen" claim, fetch, then either store the listing
   as a post or, for a portal's search/list page, queue its listing links.
   A listing on a site that cannot be read (it refuses bots, robots.txt, a
   blocked site) becomes a post from the search engine's title and snippet;
2. else pending queries -> search up to ``queries_per_tick`` in SearXNG and
   queue the new URLs (a URL any campaign already met is never queued);
3. else, below the per-campaign query cap -> generate the next round of
   queries (the model is shown every query already used);
4. else the stage is done.

Caps: queries and pages per campaign, pages per site per campaign, pages and
queries per rolling day for the whole system, and a wall-clock limit per
campaign. A finished or cancelled campaign ends its stage on the next tick.
It never logs in, submits forms or reads anything but public GET pages.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from bot.campaign import geo
from bot.campaign.architect import SearchPlanner, plan_with_model
from bot.campaign.models import TERMINAL_STATES, Campaign
from bot.campaign.search_plan import blocked_hosts_of, country_portals, source_hosts
from bot.campaign.spec import TaskSpec
from bot.campaign.store import CampaignStore
from bot.utils import costs
from bot.utils.listing_text import deal_of

from .extract import (
    MIN_INDEX_LINKS,
    Link,
    ParsedPage,
    listing_links,
    looks_like_index,
    parse_html,
    post_text,
)
from .fetcher import FetchError, PageFetcher
from .models import (
    INDEX_RESULT_NOTE,
    SEARCH_RESULT_NOTE,
    Candidate,
    FetchTicket,
    PageResult,
    QueuedUrl,
    WebProgress,
)
from .queries import (
    QueryGenerator,
    QueryTask,
    cover_portals,
    localise,
    missing_portals,
    place_level_of,
    plan_portal_urls,
    plan_sites,
    portal_quota,
)
from .render import ChallengeDetected, Renderer, RenderError
from .scrape_api import Scraper
from .searxng import Searcher, SearchError
from .store import (
    _REFUSALS,
    BUSY,
    HOST_BLOCKED,
    HOST_BREAKER,
    VERIFICATION_EXPIRED,
    WebStore,
    funnel_totals,
)
from .structured import Structured, facts_block, from_jsonld, structured
from .urls import (
    classify_page,
    classify_url,
    deal_conflict,
    fetchable,
    host_of,
    known_portal,
    listing_evidence,
    listing_figures,
    portal_listing,
    url_key,
)

log = logging.getLogger(__name__)
MIN_POST_CHARS = 120
MIN_SNIPPET_CHARS = 60  # title + snippet: less than this says nothing about the listing


DOMAIN_POLICIES = ("strict", "soft", "off")


@dataclass(frozen=True)
class WebSearchConfig:
    queries_per_round: int = 12
    max_queries_per_campaign: int = 40
    max_rounds: int = 8
    queries_per_tick: int = 3
    results_per_query: int = 30
    pages_per_query: int = 2          # SearXNG result pages walked per query (pageno 1..N)
    max_pages_per_campaign: int = 60
    max_pages_per_host: int = 12
    max_pages_per_unknown_host: int = 5   # a host that is no known portal, until it produced a listing post
    max_links_per_index: int = 10
    pages_per_tick: int = 4
    max_pages_per_day: int = 400
    max_queries_per_day: int = 300
    max_minutes_per_campaign: int = 240
    query_reuse_hours: int = 0        # 0: another campaign may repeat a query; N: it is skipped for N hours
    index_ttl_days: int = 7           # an index page is read again after this many days (0: never)
    lease_seconds: int = 300
    max_post_chars: int = 8000
    page_runtime_seconds: int = 60
    max_renders_per_campaign: int = 60
    render_on_refusal: bool = True    # a refused page (403/429/503, a captcha page) is tried once in the browser
    max_scrape_api_per_campaign: int = 40   # scrape-API reads per campaign (0: the layer is off)
    render_index_on_refusal: bool = True    # a refused depth-0 index page may use the browser (never the scrape API)
    blocked_hosts: frozenset[str] = frozenset()
    cover_portals: bool = True  # every known portal of the country gets its own site: query
    # A CAPTCHA / anti-bot page in the browser becomes a verification job for a person (bot/verification); the site
    # is skipped meanwhile and, once a person passed the check, read through the same browser profile (see
    # bot/web_search/verification.py). Off: such a page is a render refusal, as before.
    human_verification: bool = False
    verified_host_interval_seconds: float = 8    # gap between two reads of one verified site
    pages_per_verification: int = 40             # pages read through the browser after one passed check
    # Which sites a search result may come from (``domain_policy``): strict -- only the country's known portals and the
    # sites the plan or the person named; soft -- also other sites whose hit shows a property with a price or an area;
    # off -- also hits with a property word and a deal word (the old rule). A country without known portals is soft.
    domain_policy: str = "strict"
    # A site whose pages were refused (403/429/captcha, on every layer tried) this many times in a row is skipped for
    # the rest of the campaign: its search results are kept as cards, no layer (and no paid unlocker) is tried again.
    host_breaker_refusals: int = 3               # 0: off
    scrape_cost_usd: float = 0.0                 # what one scrape-API read costs (the ledger, CAMPAIGN_BUDGET_USD)
    query_cost_usd: float = 0.0                  # what one search query costs (paid backends; SearXNG is free)

    def __post_init__(self) -> None:
        if not (1 <= self.queries_per_round <= 30 and 1 <= self.max_queries_per_campaign <= 200
                and 1 <= self.queries_per_tick <= 10 and 1 <= self.results_per_query <= 30
                and 1 <= self.max_pages_per_campaign <= 500 and 1 <= self.max_pages_per_host <= 100
                and 1 <= self.max_pages_per_unknown_host <= 100
                and 0 <= self.max_links_per_index <= 100 and 1 <= self.pages_per_query <= 5
                and 0 <= self.query_reuse_hours <= 720 and 0 <= self.index_ttl_days <= 365 and 1 <= self.pages_per_tick <= 20
                and 1 <= self.max_pages_per_day <= 10_000 and 1 <= self.max_queries_per_day <= 5_000
                and 1 <= self.max_rounds <= 50 and 5 <= self.max_minutes_per_campaign <= 7 * 24 * 60
                and 60 <= self.lease_seconds <= 3600 and 1 <= self.page_runtime_seconds <= 600
                and 0 <= self.max_renders_per_campaign <= 200 and 0 <= self.max_scrape_api_per_campaign <= 500
                and 0 <= self.verified_host_interval_seconds <= 300 and 1 <= self.pages_per_verification <= 200
                and self.domain_policy in DOMAIN_POLICIES and 0 <= self.host_breaker_refusals <= 100
                and 0 <= self.scrape_cost_usd <= 10 and 0 <= self.query_cost_usd <= 10):
            raise ValueError("unsafe web search limits")


def campaign_deal(campaign: Campaign) -> str | None:
    """``sale`` or ``rent`` when the campaign wants one deal, else None (no deal filter)."""
    deal = campaign.plan.constraints.get("deal")
    return deal if deal in ("sale", "rent") else None


def known_hosts_of(campaign: Campaign) -> frozenset[str]:
    """Hosts the plan or the spec names (``spec.sources`` required/extra, the plan's sites): not capped as unknown."""
    hosts = {h for h, _ in plan_sites(campaign.plan.search_plan)}
    sources = (campaign.spec or {}).get("sources") if isinstance(campaign.spec, dict) else None
    if isinstance(sources, dict):
        names = [str(x) for k in ("required", "extra") for x in (sources.get(k) or [])]
        hosts.update(source_hosts(names, country_portals(campaign.plan.country)))
    return frozenset(hosts)


def query_task(campaign: Campaign) -> QueryTask:
    plan = campaign.plan
    return QueryTask(goal=plan.goal, task_text=campaign.source_text, location=plan.location,
                     location_aliases=dict(plan.location_aliases), vertical=plan.vertical,
                     constraints=dict(plan.constraints), languages=tuple(plan.languages),
                     country_code=plan.country, place_level=place_level_of(campaign.source_text, plan.location),
                     search_plan=plan.search_plan, blocked_hosts=blocked_hosts_of(campaign.spec, plan.country))


class WebSearchWorker:
    def __init__(
        self,
        campaigns: CampaignStore,
        store: WebStore,
        searcher: Searcher,
        fetcher: PageFetcher,
        generator: QueryGenerator,
        *,
        renderer: Renderer | None = None,
        scraper: Scraper | None = None,
        planner: SearchPlanner | None = None,
        config: WebSearchConfig | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        cancel_job: Callable[[str, str], Awaitable[bool]] | None = None,
    ) -> None:
        self.campaigns, self.store, self.searcher, self.fetcher, self.generator = (
            campaigns, store, searcher, fetcher, generator)
        self.config, self.now = config or WebSearchConfig(), now
        self.renderer, self.scraper = renderer, scraper
        self.cancel_job = cancel_job  # the verification store's ``cancel(job_id, actor)``
        self.planner = planner  # writes the campaign's search plan once, before its first round
        self.token = str(uuid.uuid4())
        self._progress: dict[str, WebProgress] = {}  # campaign id -> live numbers (also shown through the store)
        self._listing_hosts: dict[tuple[str, str], bool] = {}  # (campaign, host) -> has a listing, reset every step
        self._verified_at: dict[str, datetime] = {}  # site -> when it was last read through the verified browser
        # (campaign, site) -> pages refused in a row in this campaign; a site at ``host_breaker_refusals`` is tripped
        # (in memory: after a restart a tripped site gets that many tries again, its queued URLs stay skipped)
        self._refused: dict[tuple[str, str], int] = {}
        self._tripped: set[tuple[str, str]] = set()

    def progress(self, campaign_id: str) -> WebProgress:
        """The live progress of the campaign's web stage: current host and layer, pages read, listings found,
        sites done/known (in memory; empty until the worker has read a page for the campaign)."""
        return self._progress.get(campaign_id, WebProgress())

    def _set_layer(self, campaign_id: str, layer: str | None) -> None:
        self._publish(campaign_id, replace(self.progress(campaign_id), layer=layer))

    def _publish(self, campaign_id: str, progress: WebProgress) -> None:
        self._progress[campaign_id] = progress
        note = getattr(self.store, "note_progress", None)
        if note is not None:
            note(campaign_id, progress)

    async def _track(self, campaign_id: str, host: str | None, layer: str | None, *, finished: bool = False,
                     url: str | None = None) -> None:
        """Refresh the progress numbers from the store; a failure only leaves them as they were."""
        try:
            read, found, done, total = funnel_totals(await self.store.funnel(campaign_id))
            names = {"http": "http", "render": "browser"}
            refusals = tuple((names[k], n) for k, n in (await self.store.host_refusals(host) if host else {}).items()
                             if n and k in names)
            self._publish(campaign_id, WebProgress(host, layer, read, found, done, total, refusals, finished, url))
        except Exception:  # noqa: BLE001 - progress is cosmetic
            log.warning("web_search.progress_failed", extra={"campaign_id": campaign_id})

    async def tick(self) -> int:
        """One step for every campaign whose web stage is open; returns how many were seen.

        First, page reads a crashed worker left running (older than the lease) are failed,
        so their site is readable again and their URL is claimed again.
        """
        freed = await self.store.recover(self.config.lease_seconds)
        if freed:
            log.warning("web_search.recovered_runs", extra={"runs": freed})
        ids = await self.store.campaign_ids()
        for campaign_id in ids:
            try:
                await self.step(campaign_id)
            except Exception:  # one campaign must never stop the others
                log.exception("web_search.step_failed", extra={"campaign_id": campaign_id})
        return len(ids)

    async def serve(self, poll_seconds: float, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:  # a database outage delays the stage, it never ends the loop
                log.exception("web_search.tick_failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)

    async def step(self, campaign_id: str) -> None:
        self._listing_hosts = {}  # per-tick cache of ``_host_has_listing``
        campaign = await self.campaigns.get(campaign_id)
        if campaign is None:
            return
        run = await self.store.get_run(campaign_id)
        if campaign.state in TERMINAL_STATES:
            if run is not None and run.state == "searching":
                await self.store.finish(campaign_id, "stopped", f"campaign_{campaign.state}")
                await self._release_idle_jobs()
            return
        if run is None:
            run = await self.store.start_run(campaign_id)
        if run.state != "searching" or not await self.store.take_lease(campaign_id, self.token, self.config.lease_seconds):
            return
        try:
            with costs.scope(campaign_id):  # every paid call of this step is booked on the campaign
                await self._advance(campaign)
        finally:
            await self.store.drop_lease(campaign_id, self.token)

    async def _advance(self, campaign: Campaign) -> None:
        cfg, cid = self.config, campaign.id
        started = await self.store.started_at(cid)
        if started is not None and self.now() - started > timedelta(minutes=cfg.max_minutes_per_campaign):
            await self._done(campaign, "time_cap")
            return
        if await costs.over_budget(cid):  # CAMPAIGN_BUDGET_USD spent (all services together)
            await self._done(campaign, "budget_cap")
            return
        counts = await self.store.counts(cid)
        usage = await self.store.usage()
        # Sites that wait for a person's check keep their URLs queued; the stage goes on with everything else.
        waiting = await self.store.verification_waiting(cid) if self._human() else []
        queued = counts.queued_urls - (await self.store.queued_in(cid, waiting) if waiting else 0)
        if queued > 0:
            if counts.pages >= cfg.max_pages_per_campaign:
                await self._done(campaign, "page_cap")
                return
            if usage.pages >= cfg.max_pages_per_day:
                await self._done(campaign, "daily_page_cap")
                return
            await self._read_pages(campaign, counts.pages, usage.pages)
            return
        if counts.pending_queries:
            if usage.queries >= cfg.max_queries_per_day:
                await self._done(campaign, "daily_query_cap")
                return
            await self._search(campaign, counts.queries, counts.pages)
            return
        if counts.pages >= cfg.max_pages_per_campaign:
            await self._done(campaign, "page_cap")
            return
        run = await self.store.get_run(cid)
        if counts.queries >= cfg.max_queries_per_campaign or (run is not None and run.rounds >= cfg.max_rounds):
            if not waiting:  # a site's check is still pending: the stage waits for it (or for its expiry)
                await self._done(campaign, "queries_done")
            return
        if not await self._new_round(campaign, counts.queries) and not waiting:
            await self._done(campaign, "queries_exhausted")

    # -- queries --

    async def _new_round(self, campaign: Campaign, used_count: int) -> bool:
        cfg = self.config
        if used_count == 0:  # the first round: the search plan (once), then its direct portal pages
            campaign = await self._with_search_plan(campaign)
            await self._enqueue_portal_urls(campaign)
        want = min(cfg.queries_per_round, cfg.max_queries_per_campaign - used_count)
        used = await self.store.used_queries(campaign.id)
        await self.store.set_progress(campaign.id, None, self._line(used_count, None, "составляю запросы"))
        task = query_task(campaign)
        # The known portals (Idealista, Fotocasa first) are always searched: the ones not yet
        # searched take up to half of this round, written by the model or, if it skips one, from the task.
        if cfg.cover_portals:
            task = replace(task, required_portals=missing_portals(task, used)[:portal_quota(want)])
        # Whatever generated them, every query names the campaign's place (``localise``).
        queries = localise(await self.generator.generate(task, used=used, count=want), task)
        queries = cover_portals(queries, task, used, want)
        round_no = await self.store.next_round(campaign.id)
        added = await self.store.add_queries(campaign.id, round_no, queries[:want], reuse_hours=cfg.query_reuse_hours)
        log.info("web_search.round", extra={"campaign_id": campaign.id, "round": round_no, "generated": len(queries),
                                            "added": added})
        return bool(queries)

    async def _with_search_plan(self, campaign: Campaign) -> Campaign:
        """The campaign with its model-written search plan: asked once (idempotent), stored, never required."""
        if self.planner is None or campaign.plan.search_plan is not None or not campaign.spec:
            return campaign
        try:
            spec = TaskSpec.model_validate(campaign.spec)
        except ValueError:
            return campaign
        planned = await plan_with_model(campaign.plan, spec, self.planner, source_text=campaign.source_text)
        if planned.search_plan is None:
            return campaign
        await self.campaigns.set_search_plan(campaign.id, planned.search_plan)
        return replace(campaign, plan=planned)

    async def _enqueue_portal_urls(self, campaign: Campaign) -> None:
        """The plan's direct portal search pages go into the queue as depth-0 index pages (already queued: no-op)."""
        task = query_task(campaign)
        blocked = self.config.blocked_hosts | task.blocked_hosts
        # The kind is what the URL looks like (an index page is expected; a concrete listing URL is never queued here).
        deal = campaign_deal(campaign)
        candidates = [Candidate(u, url_key(u), host_of(u), 0, classify_url(u)) for u in plan_portal_urls(task.search_plan)
                      if fetchable(u, blocked) and classify_url(u) != "listing" and not deal_conflict(u, deal)]
        if candidates:
            await self.store.enqueue(campaign.id, candidates, index_ttl_days=self.config.index_ttl_days)

    async def _search(self, campaign: Campaign, query_count: int, pages: int) -> None:
        task = query_task(campaign)
        blocked = self.config.blocked_hosts | task.blocked_hosts
        deal, known, real_estate = campaign_deal(campaign), known_hosts_of(campaign), campaign.plan.vertical == "real_estate"
        allowed = self._allowed_hosts(campaign, known)
        for query in await self.store.pending_queries(campaign.id, self.config.queries_per_tick):
            await self.store.set_progress(campaign.id, None, self._line(query_count, pages, "поиск"))
            try:
                # A Spanish campaign searches Spain (es-ES) whatever the query's language.
                hits = await self.searcher.search(query.text, language=task.search_language or query.language)
            except SearchError as exc:
                log.warning("web_search.search_failed %s", exc.code, extra={"campaign_id": campaign.id})
                await self.store.query_done(query.id, ok=False, results=0, new_urls=0, error=exc.code)
                continue
            if self.config.query_cost_usd:
                await costs.record("search", provider="search_api", item="query", cost_usd=self.config.query_cost_usd)
            candidates: list[Candidate] = []
            dropped = 0
            for hit in hits[: self.config.results_per_query]:
                if not fetchable(hit.url, blocked):
                    continue
                host = host_of(hit.url)
                if deal and (deal_conflict(hit.url, deal) or deal_of(hit.title) not in (None, deal)):
                    dropped += 1  # a rent page for a sale campaign (and vice versa): its path or its title says so
                    continue
                if real_estate and not self._host_allowed(host, known, allowed, hit.title, hit.snippet):
                    dropped += 1  # not a client portal (strict), or a hit without a listing's figures: a dictionary ...
                    continue
                if geo.foreign_tld(host_of(hit.url), task.country):  # .ru/.ua/.pl ... for a Spanish campaign
                    continue
                if geo.foreign_markers_hit(task.country, f"{hit.title} {hit.snippet}"):  # Valencia in Venezuela/CA
                    continue
                candidates.append(Candidate(hit.url, url_key(hit.url), host_of(hit.url), 0, classify_url(hit.url),
                                            query.id, hit.title, hit.snippet))
            if dropped:
                log.info("web_search.serp_dropped", extra={"campaign_id": campaign.id, "query_id": query.id,
                                                           "dropped": dropped, "hits": len(hits)})
            new = await self.store.enqueue(campaign.id, candidates, index_ttl_days=self.config.index_ttl_days)
            await self.store.query_done(query.id, ok=True, results=len(hits), new_urls=new)

    def _allowed_hosts(self, campaign: Campaign, known: frozenset[str]) -> frozenset[str]:
        """The sites a strict campaign may read: the country's portals plus the plan's and the person's (empty: the
        country has no portal list, so a site the person named must not become the only one -- the campaign is soft)."""
        portals = country_portals(campaign.plan.country)
        return frozenset((*portals, *known)) if portals else frozenset()

    def _host_allowed(self, host: str, known: frozenset[str], allowed: frozenset[str], title: str,
                      snippet: str) -> bool:
        """Whether a search hit on ``host`` may be queued under ``domain_policy`` (see ``WebSearchConfig``)."""
        policy = self.config.domain_policy
        if policy == "strict" and allowed:
            return any(host == h or host.endswith("." + h) for h in allowed)
        if known_portal(host, known):
            return True
        if policy == "off":
            return listing_evidence(title, snippet)
        return listing_evidence(title, snippet) and listing_figures(title, snippet)

    def _note_read(self, campaign_id: str, host: str, result: PageResult) -> bool:
        """Count a refused page of ``host`` (a page read resets the count); True when the site trips the breaker now."""
        limit, key = self.config.host_breaker_refusals, (campaign_id, host)
        if not limit or key in self._tripped:
            return False
        if result.ok and result.via == "page":
            self._refused.pop(key, None)
            return False
        error = result.error or ""
        if error not in BREAKER_ERRORS and not error.startswith("scrape_http_4"):
            return False
        self._refused[key] = self._refused.get(key, 0) + 1
        if self._refused[key] < limit:
            return False
        self._tripped.add(key)
        return True

    # -- pages --

    async def _read_pages(self, campaign: Campaign, pages: int, pages_today: int) -> None:
        cfg = self.config
        budget = min(cfg.pages_per_tick, cfg.max_pages_per_campaign - pages, cfg.max_pages_per_day - pages_today)
        known = known_hosts_of(campaign)
        human = self._human()
        waiting = frozenset(await self.store.verification_waiting(campaign.id)) if human else frozenset()
        busy = human and await self.store.verification_busy()   # a person has the browser open on its profile
        for url in await self.store.next_urls(campaign.id, cfg.pages_per_tick * 3, waiting):
            if budget <= 0:
                break
            if url.host in waiting:  # a challenge met earlier in this very tick
                continue
            if (campaign.id, url.host) in self._tripped:  # the site kept refusing us: its search result is all we keep
                await self.store.mark_url(campaign.id, url.url_key, "skipped", HOST_BREAKER)
                await self._keep_search_result(campaign, url, None)
                continue
            attempts = await self.store.host_attempts(campaign.id, url.host)
            if attempts >= cfg.max_pages_per_host:
                await self.store.mark_url(campaign.id, url.url_key, "capped", "host_cap")
                continue
            if (attempts >= cfg.max_pages_per_unknown_host and not known_portal(url.host, known)
                    and not await self._host_has_listing(campaign.id, url.host)):
                log.info("web_search.unknown_host_capped", extra={"campaign_id": campaign.id, "host": url.host})
                await self.store.mark_url(campaign.id, url.url_key, "capped", "unknown_host_cap")
                continue
            if not await self.fetcher.allowed(url.url):
                if not await self._keep_search_result(campaign, url, None):
                    await self.store.mark_url(campaign.id, url.url_key, "robots", "robots_txt")
                continue
            # Which layers can read this URL right now, decided before a ticket is claimed: a URL no layer can
            # take is not claimed, so it never spends the page budget or the host cap on a ``layer="none"`` result.
            layers = await self.store.layer_state(url.host)
            gate = await self._gate(campaign.id, url, layers, busy) if human else None
            if gate == "wait":
                continue  # the site's check is pending (or a verified read is not due yet): the URL stays queued
            if gate == "unsolved":
                continue  # nobody passed the site's check: its URLs were dropped (counted in the report)
            if busy:
                layers = {**layers, "render": False}  # the browser profile is in a person's hands
            render_ok, scrape_ok = await self._fallbacks(campaign.id, url, layers)
            if gate == "verified":
                render_ok, scrape_ok = True, False
            if not (layers.get("http", True) or render_ok or scrape_ok):
                await self.store.mark_url(campaign.id, url.url_key, "skipped", HOST_BLOCKED)
                await self._keep_search_result(campaign, url, None)
                continue
            ticket = await self.store.begin_fetch(campaign.id, url, vertical=campaign.plan.vertical,
                                                  lease_seconds=cfg.lease_seconds,
                                                  max_runtime_seconds=cfg.page_runtime_seconds,
                                                  render_layer=render_ok, scrape_layer=scrape_ok)
            if isinstance(ticket, str):
                if ticket == HOST_BLOCKED:  # the site kept refusing us: its search result is all we keep
                    await self._keep_search_result(campaign, url, None)
                elif ticket != BUSY:
                    log.info("web_search.url_skipped %s", ticket, extra={"campaign_id": campaign.id})
                continue
            budget -= 1
            pages += 1
            await self.store.set_progress(campaign.id, url.host, self._line(None, pages, f"сайт {url.host}"))
            await self._track(campaign.id, url.host, "browser" if gate == "verified" else "http", url=url.url)
            try:
                if gate == "verified":
                    result, children = await self._read_verified(campaign.id, url)
                else:
                    result, children = await self._read(campaign, url, layers)
            except _Challenge as challenge:
                # The site asked for a person's check: one job per site, the URL goes back to the queue unread and
                # uses no page, host or render budget; the site is skipped until the job is solved or expires.
                await self._defer(campaign, ticket, url, challenge.found)
                waiting = waiting | {url.host}
                budget += 1
                pages -= 1
                await self._track(campaign.id, url.host, None, url=url.url)
                continue
            tripped = self._note_read(campaign.id, url.host, result)
            if not result.ok:  # refused (403, a captcha page ...): the listing as the search engine showed it
                card = search_result(url, result.error)
                result = replace(card, layer=result.layer) if card else result
            await self.store.finish_fetch(ticket, result)
            if tripped:  # its queued URLs are dropped one by one above, each keeping its search-result card
                log.warning("web_search.host_breaker", extra={"campaign_id": campaign.id, "host": url.host,
                                                              "refusals": self.config.host_breaker_refusals})
            deal = campaign_deal(campaign)
            children = [c for c in children if fetchable(c.url, cfg.blocked_hosts | query_task(campaign).blocked_hosts)
                        and not deal_conflict(c.url, deal)]
            if children:
                await self.store.enqueue(campaign.id, children, index_ttl_days=cfg.index_ttl_days)
            await self._track(campaign.id, url.host, None, url=url.url)

    async def _host_has_listing(self, campaign_id: str, host: str) -> bool:
        """The host has already produced at least one listing post in this campaign (asked once per host per tick)."""
        key = (campaign_id, host)
        if key not in self._listing_hosts:
            self._listing_hosts[key] = any(row[0] == host and row[3] > 0 for row in await self.store.funnel(campaign_id))
        return self._listing_hosts[key]

    async def _keep_search_result(self, campaign: Campaign, url: QueuedUrl, error: str | None) -> bool:
        """Store ``url``'s search result as its post without asking the site; False when there is none."""
        result = search_result(url, error)
        if result is None:
            return False
        ticket = await self.store.begin_fetch(campaign.id, url, vertical=campaign.plan.vertical,
                                              lease_seconds=self.config.lease_seconds,
                                              max_runtime_seconds=self.config.page_runtime_seconds,
                                              contact_site=False)
        if isinstance(ticket, str):  # already read (duplicate), a paused site, or busy: retried later
            return True
        await self.store.finish_fetch(ticket, result)
        log.info("web_search.search_result_kept", extra={"campaign_id": campaign.id, "host": url.host})
        return True

    def _render_layer(self) -> bool:
        """The browser is a fetch layer for refused pages (so a host's browser refusals can block it too)."""
        return self.renderer is not None and self.config.render_on_refusal and self.config.max_renders_per_campaign > 0

    def _may_fall_back(self, url: QueuedUrl) -> tuple[bool, bool]:
        """(browser, scrape API) allowed for ``url`` after a refusal: only listings and depth-1 children use them;
        a refused depth-0 index page gets at most the browser (``render_index_on_refusal``), never the scrape API."""
        if url.kind == "listing" or url.depth >= 1:
            return True, True
        return url.kind == "index" and self.config.render_index_on_refusal, False

    async def _fallbacks(self, campaign_id: str, url: QueuedUrl, layers: dict[str, bool]) -> tuple[bool, bool]:
        """Whether the browser / the scrape API can still read ``url``: allowed, configured, open, within budget."""
        cfg = self.config
        may_render, may_scrape = self._may_fall_back(url)
        render = (may_render and self._render_layer() and layers.get("render", True)
                  and await self.store.renders_used(campaign_id) < cfg.max_renders_per_campaign)
        scrape = (may_scrape and self.scraper is not None
                  and await self.store.scrapes_used(campaign_id) < cfg.max_scrape_api_per_campaign
                  and not await costs.over_budget(campaign_id))
        return render, scrape

    async def _read(self, campaign: Campaign, url: QueuedUrl,
                    layers: dict[str, bool] | None = None) -> tuple[PageResult, list[Candidate]]:
        """Layers: plain HTTP (+ Scrapling JSON-LD), then the browser, then an optional scrape API.

        ``layers``: which layers of the host are open (``store.layer_state``). A page the site refused (403/429/503,
        a captcha page) goes to the next layer instead of straight to the search-result card; a layer the host
        blocked is skipped. The empty-JavaScript-page render (HTTP 200, no text) is as before.
        """
        cfg = self.config
        layers = layers or {"http": True, "render": True}
        failed = PageResult(False, url.kind, url.url, error="http_blocked", layer="none")  # HTTP skipped: blocked
        if layers.get("http", True):
            try:
                fetch = self.fetcher.fetch(url.url, country=query_task(campaign).country)
                page = await asyncio.wait_for(fetch, timeout=cfg.page_runtime_seconds)
            except FetchError as exc:
                failed = PageResult(False, url.kind, url.url, error=exc.code)
            except TimeoutError:
                failed = PageResult(False, url.kind, url.url, error="timeout")
            else:
                parsed = parse_html(page.html, page.url)
                if looks_blocked(parsed.title, post_text(parsed, limit=cfg.max_post_chars)):
                    failed = PageResult(False, url.kind, page.url, parsed.title, error="captcha")
                else:
                    result, children = self._page(url, page.url, parsed, structured(page.html, page.url),
                                                  blocked=cfg.blocked_hosts | query_task(campaign).blocked_hosts,
                                                  deal=campaign_deal(campaign))
                    if not await self._wants_render(campaign.id, result, children):
                        return result, children
                    return await self._render_empty(campaign.id, url, page.url, parsed, result, children)
            if failed.error not in RENDER_ON:
                return failed, []
        return await self._next_layers(campaign, url, failed, render_open=layers.get("render", True))

    async def _render_empty(self, campaign_id: str, url: QueuedUrl, page_url: str, parsed: ParsedPage,
                            result: PageResult, children: list[Candidate]) -> tuple[PageResult, list[Candidate]]:
        """A page served with HTTP 200 but drawn by JavaScript: read it once more in the browser."""
        cfg = self.config
        self._set_layer(campaign_id, "browser")
        await self.store.mark_rendered(campaign_id, url.url_key)
        try:
            rendered = await asyncio.wait_for(self.renderer.render(page_url), timeout=cfg.page_runtime_seconds)
        except ChallengeDetected as exc:
            if self._human():
                raise _Challenge(exc) from exc
            return result, children  # human verification is off: the page stays as the plain fetch read it
        except (RenderError, TimeoutError) as exc:
            log.info("web_search.render_skipped %s", getattr(exc, "code", "timeout"), extra={"campaign_id": campaign_id})
            return result, children
        seen = ParsedPage(rendered.title or parsed.title, parsed.description, rendered.text,
                          tuple(Link(href, text) for href, text in rendered.links))
        again, more = self._page(url, rendered.url, seen, from_jsonld(rendered.jsonld, rendered.url))
        log.info("web_search.rendered", extra={"campaign_id": campaign_id, "ok": again.ok, "links": len(more)})
        return (again, more) if again.ok and (again.kind == "listing" or more) else (result, children)

    async def _next_layers(self, campaign: Campaign, url: QueuedUrl, failed: PageResult, *,
                           render_open: bool) -> tuple[PageResult, list[Candidate]]:
        """The HTTP layer was refused or blocked: the browser (once, within its budget), then the scrape API.

        A refusal of a layer left behind is counted for the host here; the last layer's result goes through
        ``finish_fetch``. robots.txt was checked before any layer (``_read_pages``): a disallowed URL never gets here.
        """
        cfg, cid, current = self.config, campaign.id, failed
        may_render, may_scrape = self._may_fall_back(url)
        if (may_render and self._render_layer() and render_open and self.renderer is not None
                and await self.store.renders_used(cid) < cfg.max_renders_per_campaign):
            http_result = current
            current, children = await self._render_refused(cid, url)   # may raise _Challenge: nothing is counted then
            await self._leave(url, http_result)
            if current.ok:
                return current, children
        if (may_scrape and self.scraper is not None and await self.store.scrapes_used(cid) < cfg.max_scrape_api_per_campaign
                and not await costs.over_budget(cid)):
            await self._leave(url, current)
            current, children = await self._scrape(cid, url)
            if current.ok:
                return current, children
        return current, []

    async def _leave(self, url: QueuedUrl, result: PageResult) -> None:
        """Count ``result``'s refusal against its layer of the host when another layer takes over."""
        if result.layer in ("http", "render") and (result.error or "") in REFUSALS:
            await self.store.layer_refused(url.host, result.layer)

    async def _render_refused(self, campaign_id: str, url: QueuedUrl, *,
                              count: bool = True) -> tuple[PageResult, list[Candidate]]:
        """Read ``url`` in the browser. ``count`` False: a verified site's page, which is not on the campaign's
        render budget (it has its own, ``pages_per_verification``)."""
        cfg = self.config
        self._set_layer(campaign_id, "browser")
        if count:
            await self.store.mark_rendered(campaign_id, url.url_key)
        try:
            rendered = await asyncio.wait_for(self.renderer.render(url.url), timeout=cfg.page_runtime_seconds)
        except ChallengeDetected as exc:
            if self._human():
                raise _Challenge(exc) from exc
            log.info("web_search.render_refused", extra={"campaign_id": campaign_id, "host": url.host})
            return PageResult(False, url.kind, url.url, error="render_blocked", layer="render"), []
        except (RenderError, TimeoutError) as exc:
            code = getattr(exc, "code", "timeout")
            log.info("web_search.render_failed %s", code, extra={"campaign_id": campaign_id, "host": url.host})
            return PageResult(False, url.kind, url.url, error=f"render:{code}"[:80], layer="render"), []
        if looks_blocked(rendered.title, rendered.text):
            log.info("web_search.render_refused", extra={"campaign_id": campaign_id, "host": url.host})
            return PageResult(False, url.kind, url.url, rendered.title, error="render_blocked", layer="render"), []
        seen = ParsedPage(rendered.title, "", rendered.text, tuple(Link(href, text) for href, text in rendered.links))
        result, children = self._page(url, rendered.url, seen, from_jsonld(rendered.jsonld, rendered.url))
        log.info("web_search.rendered_after_refusal", extra={"campaign_id": campaign_id, "ok": result.ok,
                                                             "links": len(children)})
        if result.ok and (result.kind == "listing" or children):
            return replace(result, layer="render"), children
        return PageResult(False, url.kind, rendered.url, rendered.title, error="no_readable_text", layer="render"), []

    async def _scrape(self, campaign_id: str, url: QueuedUrl) -> tuple[PageResult, list[Candidate]]:
        cfg = self.config
        self._set_layer(campaign_id, "api")
        await self.store.mark_scraped(campaign_id, url.url_key)
        # Booked before the call: a provider may bill a refused or timed-out request too (the safe side of a budget).
        await costs.record("scrape", provider="scrape_api", item=url.host, cost_usd=cfg.scrape_cost_usd)
        try:
            page = await asyncio.wait_for(self.scraper.fetch(url.url), timeout=cfg.page_runtime_seconds * 2)
        except FetchError as exc:
            return PageResult(False, url.kind, url.url, error=exc.code, layer="scrape"), []
        except TimeoutError:
            return PageResult(False, url.kind, url.url, error="scrape_timeout", layer="scrape"), []
        parsed = parse_html(page.html, page.url)
        if looks_blocked(parsed.title, post_text(parsed, limit=cfg.max_post_chars)):
            return PageResult(False, url.kind, url.url, parsed.title, error="captcha", layer="scrape"), []
        result, children = self._page(url, page.url, parsed, structured(page.html, page.url))
        log.info("web_search.scraped", extra={"campaign_id": campaign_id, "ok": result.ok, "links": len(children)})
        return replace(result, layer="scrape"), children

    async def _wants_render(self, campaign_id: str, result: PageResult, children: list[Candidate]) -> bool:
        """Only a page the site served (HTTP 200) but drew with JavaScript: no text, or a list without links."""
        if self.renderer is None or await self.store.renders_used(campaign_id) >= self.config.max_renders_per_campaign:
            return False
        return (not result.ok and result.error == "no_readable_text") or (result.kind == "index" and not children)

    def _page(self, url: QueuedUrl, final_url: str, parsed: ParsedPage,
              data: Structured, blocked: frozenset[str] | None = None,
              deal: str | None = None) -> tuple[PageResult, list[Candidate]]:
        cfg = self.config
        blocked = cfg.blocked_hosts if blocked is None else blocked  # config + the campaign's blocked sources
        kind = classify_url(final_url) if url.kind == "unknown" else url.kind
        if url.depth == 0:
            host = host_of(final_url)
            from_json = [u for u in data.item_urls if host_of(u) == host and url_key(u) != url_key(final_url)
                         and fetchable(u, blocked)
                         and not deal_conflict(u, deal)]
            if kind == "index" or (kind == "unknown" and (looks_like_index(parsed, final_url)
                                                          or len(from_json) >= MIN_INDEX_LINKS)):
                links: list[str] = []
                for link in [*from_json, *listing_links(parsed, final_url, limit=cfg.max_links_per_index,
                                                        extra_blocked=blocked, deal=deal)]:
                    if len(links) < cfg.max_links_per_index and url_key(link) not in {url_key(x) for x in links}:
                        links.append(link)
                cards = index_cards(data, final_url)
                children = [Candidate(link, url_key(link), host_of(link), 1, "listing", None,
                                      *cards.get(url_key(link), ("", ""))) for link in links]
                return PageResult(True, "index", final_url, parsed.title), children
        text = post_text(parsed, limit=cfg.max_post_chars)
        facts = data.listing_for(final_url)
        if facts:  # the site's own figures (price, area, address) as JSON, on top of the visible text
            text = f"{facts_block(facts)}\n{text}".strip()[:cfg.max_post_chars]
        if len(text) < MIN_POST_CHARS:
            return PageResult(False, "listing", final_url, parsed.title, error="no_readable_text"), []
        if url.kind == "unknown" and classify_page(final_url, has_listing_data=facts is not None, text=text) != "listing":
            return PageResult(False, "unknown", final_url, parsed.title, error="not_a_listing"), []
        return PageResult(True, "listing", final_url, parsed.title, text), []

    # -- human verification --

    def _human(self) -> bool:
        """WEB_SEARCH_HUMAN_VERIFICATION is on and there is a browser to read verified sites with."""
        return self.config.human_verification and self.renderer is not None

    async def _gate(self, campaign_id: str, url: QueuedUrl, layers: dict[str, bool], busy: bool) -> str | None:
        """What the site's verification state means for ``url``: ``wait`` (a check is pending, or a verified read is
        not due yet or the browser is in a person's hands: the URL stays queued), ``unsolved`` (nobody passed the
        check during this campaign: the site's queued URLs are dropped), ``verified`` (read it through the browser
        profile a person passed the check in), or None (read it as usual)."""
        cfg = self.config
        host = await self.store.host_verification(url.host, campaign_id)
        if host.state == "open":
            return "wait"
        if host.state == "unsolved":
            await self.store.skip_host(campaign_id, url.host, VERIFICATION_EXPIRED)
            log.info("web_search.host_unverified", extra={"campaign_id": campaign_id, "host": url.host})
            return "unsolved"
        if host.state != "verified" or host.solved_at is None or not layers.get("render", True):
            return None
        if await self.store.verified_pages(url.host, host.solved_at) >= cfg.pages_per_verification:
            return None  # this check's pages are used up: the site is read as usual (a new challenge is a new job)
        last = self._verified_at.get(url.host)
        if busy or (last is not None and (self.now() - last).total_seconds() < cfg.verified_host_interval_seconds):
            return "wait"
        return "verified"

    async def _read_verified(self, campaign_id: str, url: QueuedUrl) -> tuple[PageResult, list[Candidate]]:
        """A site a person passed the check of: its page through the browser profile (no plain HTTP, no scrape API)."""
        self._verified_at[url.host] = self.now()
        return await self._render_refused(campaign_id, url, count=False)

    async def _defer(self, campaign: Campaign, ticket: FetchTicket, url: QueuedUrl, found: ChallengeDetected) -> None:
        await self.store.defer_fetch(ticket)
        job_id = await self.store.open_verification(found.host or url.host, found.kind, found.url or url.url)
        self._verified_at.pop(url.host, None)
        log.info("web_search.challenge", extra={"campaign_id": campaign.id, "host": url.host, "kind": found.kind,
                                                "job_id": job_id})

    # -- bookkeeping --

    async def _done(self, campaign: Campaign, reason: str) -> None:
        if self._human():  # the stage ends while a site's check is pending: nobody passed it for this campaign
            for host in await self.store.verification_waiting(campaign.id):
                await self.store.skip_host(campaign.id, host, VERIFICATION_EXPIRED)
        counts = await self.store.counts(campaign.id)
        await self.store.set_progress(campaign.id, None, self._line(counts.queries, counts.pages, f"готово ({reason})"))
        await self.store.finish(campaign.id, "done", reason)
        await self._release_idle_jobs()
        await self._track(campaign.id, None, None, finished=True)
        log.info("web_search.done", extra={"campaign_id": campaign.id, "reason": reason, "queries": counts.queries,
                                           "pages": counts.pages})

    async def _release_idle_jobs(self) -> None:
        """Cancel the open verification jobs of sites that no running campaign has queued URLs for: nobody is
        waiting for the check any more."""
        if not self._human() or self.cancel_job is None:
            return
        try:
            for job_id in await self.store.idle_verification_jobs():
                if await self.cancel_job(job_id, "web_search"):
                    log.info("web_search.verification_cancelled", extra={"job_id": job_id})
        except Exception:  # noqa: BLE001 - housekeeping must not fail the campaign's finish
            log.warning("web_search.verification_cancel_failed")

    def _line(self, queries: int | None, pages: int | None, doing: str) -> str:
        """The owners' technical line, e.g. «сайты: запросов 12/40 · страниц 7/60 · сайт fotocasa.es»."""
        parts = ["сайты:"]
        if queries is not None:
            parts.append(f"запросов {queries}/{self.config.max_queries_per_campaign} ·")
        if pages is not None:
            parts.append(f"страниц {pages}/{self.config.max_pages_per_campaign} ·")
        parts.append(doing)
        return " ".join(parts)


class _Challenge(Exception):
    """A browser read met a CAPTCHA / anti-bot page and human verification is on (``found`` says which)."""

    def __init__(self, found: ChallengeDetected) -> None:
        super().__init__(found.kind)
        self.found = found


BLOCK_MARKERS = ("captcha", "datadome", "are you a robot", "access denied")
MAX_BLOCK_PAGE_CHARS = 400
REFUSALS = _REFUSALS
RENDER_ON = ("http_403", "http_429", "http_503", "captcha")   # HTTP refusals that send the page to the next layer
# A page's final error that counts toward the site's breaker: a refusal or an anti-bot page on any layer.
BREAKER_ERRORS = frozenset({*_REFUSALS, "render_blocked"})


def looks_blocked(title: str, text: str) -> bool:
    """A short page that is an anti-bot wall (captcha, DataDome, «are you a robot», access denied), not a listing."""
    body = " ".join((text or "").split())
    return len(body) < MAX_BLOCK_PAGE_CHARS and any(m in f"{title} {body}".lower() for m in BLOCK_MARKERS)


def search_result(url: QueuedUrl, error: str | None) -> PageResult | None:
    """A listing post from what the search engine showed (title, snippet, link) when the site cannot be read.

    Only a search result (not a link found on an index page) that looks like one concrete listing,
    and only when the title and snippet say something. ``error``: why the site's page was not read
    (None: the site was never asked).
    """
    title, snippet = " ".join(url.title.split()), " ".join(url.snippet.split())
    if url.depth > 0:  # a link from an index page: only when that page's JSON-LD described it (``index_cards``)
        if not snippet.startswith("JSON-LD: {") or not (title or url.url):
            return None
        text = f"{snippet}\n{title}\n\nСсылка: {url.url}\n{INDEX_RESULT_NOTE.format(host=url.host)}".strip()
        return PageResult(True, "listing", url.url, title, text, error=error, via="index")
    if not portal_listing(url.url) or not snippet or len(title) + len(snippet) < MIN_SNIPPET_CHARS:
        return None
    text = f"{title}\n{snippet}\n\nСсылка: {url.url}\n{SEARCH_RESULT_NOTE}"
    return PageResult(True, "listing", url.url, title, text, error=error, via="search")


MAX_CARD_SNIPPET = 500  # what the queue keeps of a search snippet (``PostgresWebStore.enqueue``)
_CARD_KEYS = ("title", "url", "price", "currency", "area_m2", "rooms", "address", "property_type", "deal")


def index_cards(data: Structured, page_url: str) -> dict[str, tuple[str, str]]:
    """url_key -> (title, «JSON-LD: {...}») of the ItemList listings an index page describes itself.

    Only a listing with a title or url and a price, area or rooms; the line is kept in the child's queue row
    (title and snippet), so ``search_result`` can make the post when the detail page cannot be read, and the
    detail page, when read, is the only post of that url_key.
    """
    cards: dict[str, tuple[str, str]] = {}
    in_list = {url_key(u) for u in data.item_urls}
    for listing in data.listings:
        link = str(listing.get("url") or "")
        key = url_key(link) if link else ""
        if not link or key not in in_list or key == url_key(page_url) or key in cards:
            continue
        if not (listing.get("title") or link) or not any(k in listing for k in ("price", "area_m2", "rooms")):
            continue
        facts = {k: listing[k] for k in _CARD_KEYS if k in listing}
        line = facts_block(facts)
        for drop in ("address", "property_type", "deal", "currency", "title"):  # keep the figures, cut the rest
            if len(line) <= MAX_CARD_SNIPPET:
                break
            facts.pop(drop, None)
            line = facts_block(facts)
        if len(line) <= MAX_CARD_SNIPPET:
            cards[key] = (str(listing.get("title") or "")[:300], line)
    return cards
