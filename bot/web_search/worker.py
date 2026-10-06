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
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from bot.campaign import geo
from bot.campaign.models import TERMINAL_STATES, Campaign
from bot.campaign.store import CampaignStore

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
from .models import INDEX_RESULT_NOTE, SEARCH_RESULT_NOTE, Candidate, PageResult, QueuedUrl
from .queries import (
    QueryGenerator,
    QueryTask,
    cover_portals,
    localise,
    missing_portals,
    place_level_of,
    portal_quota,
)
from .render import Renderer, RenderError
from .scrape_api import Scraper
from .searxng import Searcher, SearchError
from .store import _REFUSALS, BUSY, HOST_BLOCKED, WebStore
from .structured import Structured, facts_block, from_jsonld, structured
from .urls import classify_page, classify_url, fetchable, host_of, portal_listing, url_key

log = logging.getLogger(__name__)
MIN_POST_CHARS = 120
MIN_SNIPPET_CHARS = 60  # title + snippet: less than this says nothing about the listing


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

    def __post_init__(self) -> None:
        if not (1 <= self.queries_per_round <= 30 and 1 <= self.max_queries_per_campaign <= 200
                and 1 <= self.queries_per_tick <= 10 and 1 <= self.results_per_query <= 30
                and 1 <= self.max_pages_per_campaign <= 500 and 1 <= self.max_pages_per_host <= 100
                and 0 <= self.max_links_per_index <= 100 and 1 <= self.pages_per_query <= 5
                and 0 <= self.query_reuse_hours <= 720 and 0 <= self.index_ttl_days <= 365 and 1 <= self.pages_per_tick <= 20
                and 1 <= self.max_pages_per_day <= 10_000 and 1 <= self.max_queries_per_day <= 5_000
                and 1 <= self.max_rounds <= 50 and 5 <= self.max_minutes_per_campaign <= 7 * 24 * 60
                and 60 <= self.lease_seconds <= 3600 and 1 <= self.page_runtime_seconds <= 600
                and 0 <= self.max_renders_per_campaign <= 200 and 0 <= self.max_scrape_api_per_campaign <= 500):
            raise ValueError("unsafe web search limits")


def query_task(campaign: Campaign) -> QueryTask:
    plan = campaign.plan
    return QueryTask(goal=plan.goal, task_text=campaign.source_text, location=plan.location,
                     location_aliases=dict(plan.location_aliases), vertical=plan.vertical,
                     constraints=dict(plan.constraints), languages=tuple(plan.languages),
                     country_code=plan.country, place_level=place_level_of(campaign.source_text, plan.location))


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
        config: WebSearchConfig | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.campaigns, self.store, self.searcher, self.fetcher, self.generator = (
            campaigns, store, searcher, fetcher, generator)
        self.config, self.now = config or WebSearchConfig(), now
        self.renderer, self.scraper = renderer, scraper
        self.token = str(uuid.uuid4())

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
        campaign = await self.campaigns.get(campaign_id)
        if campaign is None:
            return
        run = await self.store.get_run(campaign_id)
        if campaign.state in TERMINAL_STATES:
            if run is not None and run.state == "searching":
                await self.store.finish(campaign_id, "stopped", f"campaign_{campaign.state}")
            return
        if run is None:
            run = await self.store.start_run(campaign_id)
        if run.state != "searching" or not await self.store.take_lease(campaign_id, self.token, self.config.lease_seconds):
            return
        try:
            await self._advance(campaign)
        finally:
            await self.store.drop_lease(campaign_id, self.token)

    async def _advance(self, campaign: Campaign) -> None:
        cfg, cid = self.config, campaign.id
        started = await self.store.started_at(cid)
        if started is not None and self.now() - started > timedelta(minutes=cfg.max_minutes_per_campaign):
            await self._done(campaign, "time_cap")
            return
        counts = await self.store.counts(cid)
        usage = await self.store.usage()
        if counts.queued_urls:
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
            await self._done(campaign, "queries_done")
            return
        if not await self._new_round(campaign, counts.queries):
            await self._done(campaign, "queries_exhausted")

    # -- queries --

    async def _new_round(self, campaign: Campaign, used_count: int) -> bool:
        cfg = self.config
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

    async def _search(self, campaign: Campaign, query_count: int, pages: int) -> None:
        task = query_task(campaign)
        for query in await self.store.pending_queries(campaign.id, self.config.queries_per_tick):
            await self.store.set_progress(campaign.id, None, self._line(query_count, pages, "поиск"))
            try:
                # A Spanish campaign searches Spain (es-ES) whatever the query's language.
                hits = await self.searcher.search(query.text, language=task.search_language or query.language)
            except SearchError as exc:
                log.warning("web_search.search_failed %s", exc.code, extra={"campaign_id": campaign.id})
                await self.store.query_done(query.id, ok=False, results=0, new_urls=0, error=exc.code)
                continue
            candidates: list[Candidate] = []
            for hit in hits[: self.config.results_per_query]:
                if not fetchable(hit.url, self.config.blocked_hosts):
                    continue
                if geo.foreign_tld(host_of(hit.url), task.country):  # .ru/.ua/.pl ... for a Spanish campaign
                    continue
                if geo.foreign_markers_hit(task.country, f"{hit.title} {hit.snippet}"):  # Valencia in Venezuela/CA
                    continue
                candidates.append(Candidate(hit.url, url_key(hit.url), host_of(hit.url), 0, classify_url(hit.url),
                                            query.id, hit.title, hit.snippet))
            new = await self.store.enqueue(campaign.id, candidates, index_ttl_days=self.config.index_ttl_days)
            await self.store.query_done(query.id, ok=True, results=len(hits), new_urls=new)

    # -- pages --

    async def _read_pages(self, campaign: Campaign, pages: int, pages_today: int) -> None:
        cfg = self.config
        budget = min(cfg.pages_per_tick, cfg.max_pages_per_campaign - pages, cfg.max_pages_per_day - pages_today)
        for url in await self.store.next_urls(campaign.id, cfg.pages_per_tick * 3):
            if budget <= 0:
                break
            if await self.store.host_attempts(campaign.id, url.host) >= cfg.max_pages_per_host:
                await self.store.mark_url(campaign.id, url.url_key, "capped", "host_cap")
                continue
            if not await self.fetcher.allowed(url.url):
                if not await self._keep_search_result(campaign, url, None):
                    await self.store.mark_url(campaign.id, url.url_key, "robots", "robots_txt")
                continue
            # Which layers can read this URL right now, decided before a ticket is claimed: a URL no layer can
            # take is not claimed, so it never spends the page budget or the host cap on a ``layer="none"`` result.
            layers = await self.store.layer_state(url.host)
            render_ok, scrape_ok = await self._fallbacks(campaign.id, url, layers)
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
            result, children = await self._read(campaign, url, layers)
            if not result.ok:  # refused (403, a captcha page ...): the listing as the search engine showed it
                card = search_result(url, result.error)
                result = replace(card, layer=result.layer) if card else result
            await self.store.finish_fetch(ticket, result)
            if children:
                await self.store.enqueue(campaign.id, children, index_ttl_days=cfg.index_ttl_days)

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
                  and await self.store.scrapes_used(campaign_id) < cfg.max_scrape_api_per_campaign)
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
                    result, children = self._page(url, page.url, parsed, structured(page.html, page.url))
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
        await self.store.mark_rendered(campaign_id, url.url_key)
        try:
            rendered = await asyncio.wait_for(self.renderer.render(page_url), timeout=cfg.page_runtime_seconds)
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
            await self._leave(url, current)
            current, children = await self._render_refused(cid, url)
            if current.ok:
                return current, children
        if may_scrape and self.scraper is not None and await self.store.scrapes_used(cid) < cfg.max_scrape_api_per_campaign:
            await self._leave(url, current)
            current, children = await self._scrape(cid, url)
            if current.ok:
                return current, children
        return current, []

    async def _leave(self, url: QueuedUrl, result: PageResult) -> None:
        """Count ``result``'s refusal against its layer of the host when another layer takes over."""
        if result.layer in ("http", "render") and (result.error or "") in REFUSALS:
            await self.store.layer_refused(url.host, result.layer)

    async def _render_refused(self, campaign_id: str, url: QueuedUrl) -> tuple[PageResult, list[Candidate]]:
        cfg = self.config
        await self.store.mark_rendered(campaign_id, url.url_key)
        try:
            rendered = await asyncio.wait_for(self.renderer.render(url.url), timeout=cfg.page_runtime_seconds)
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
        await self.store.mark_scraped(campaign_id, url.url_key)
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
              data: Structured) -> tuple[PageResult, list[Candidate]]:
        cfg = self.config
        kind = classify_url(final_url) if url.kind == "unknown" else url.kind
        if url.depth == 0:
            host = host_of(final_url)
            from_json = [u for u in data.item_urls if host_of(u) == host and url_key(u) != url_key(final_url)
                         and fetchable(u, cfg.blocked_hosts)]
            if kind == "index" or (kind == "unknown" and (looks_like_index(parsed, final_url)
                                                          or len(from_json) >= MIN_INDEX_LINKS)):
                links: list[str] = []
                for link in [*from_json, *listing_links(parsed, final_url, limit=cfg.max_links_per_index,
                                                        extra_blocked=cfg.blocked_hosts)]:
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

    # -- bookkeeping --

    async def _done(self, campaign: Campaign, reason: str) -> None:
        counts = await self.store.counts(campaign.id)
        await self.store.set_progress(campaign.id, None, self._line(counts.queries, counts.pages, f"готово ({reason})"))
        await self.store.finish(campaign.id, "done", reason)
        log.info("web_search.done", extra={"campaign_id": campaign.id, "reason": reason, "queries": counts.queries,
                                           "pages": counts.pages})

    def _line(self, queries: int | None, pages: int | None, doing: str) -> str:
        """The owners' technical line, e.g. «сайты: запросов 12/40 · страниц 7/60 · сайт fotocasa.es»."""
        parts = ["сайты:"]
        if queries is not None:
            parts.append(f"запросов {queries}/{self.config.max_queries_per_campaign} ·")
        if pages is not None:
            parts.append(f"страниц {pages}/{self.config.max_pages_per_campaign} ·")
        parts.append(doing)
        return " ".join(parts)


BLOCK_MARKERS = ("captcha", "datadome", "are you a robot", "access denied")
MAX_BLOCK_PAGE_CHARS = 400
REFUSALS = _REFUSALS
RENDER_ON = ("http_403", "http_429", "http_503", "captcha")   # HTTP refusals that send the page to the next layer


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
