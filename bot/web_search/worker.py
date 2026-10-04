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
from .models import Candidate, PageResult, QueuedUrl
from .queries import (
    QueryGenerator,
    QueryTask,
    cover_portals,
    localise,
    missing_portals,
    portal_quota,
)
from .render import Renderer, RenderError
from .searxng import Searcher, SearchError
from .store import BUSY, HOST_BLOCKED, WebStore
from .structured import Structured, facts_block, from_jsonld, structured
from .urls import classify_url, fetchable, host_of, url_key

log = logging.getLogger(__name__)
MIN_POST_CHARS = 120
MIN_SNIPPET_CHARS = 60  # title + snippet: less than this says nothing about the listing


@dataclass(frozen=True)
class WebSearchConfig:
    queries_per_round: int = 12
    max_queries_per_campaign: int = 40
    max_rounds: int = 8
    queries_per_tick: int = 3
    results_per_query: int = 10
    max_pages_per_campaign: int = 60
    max_pages_per_host: int = 12
    max_links_per_index: int = 10
    pages_per_tick: int = 4
    max_pages_per_day: int = 400
    max_queries_per_day: int = 300
    max_minutes_per_campaign: int = 240
    query_reuse_hours: int = 72
    lease_seconds: int = 300
    max_post_chars: int = 8000
    page_runtime_seconds: int = 60
    max_renders_per_campaign: int = 15
    blocked_hosts: frozenset[str] = frozenset()
    cover_portals: bool = True  # every known portal of the country gets its own site: query

    def __post_init__(self) -> None:
        if not (1 <= self.queries_per_round <= 30 and 1 <= self.max_queries_per_campaign <= 200
                and 1 <= self.queries_per_tick <= 10 and 1 <= self.results_per_query <= 30
                and 1 <= self.max_pages_per_campaign <= 500 and 1 <= self.max_pages_per_host <= 100
                and 0 <= self.max_links_per_index <= 30 and 1 <= self.pages_per_tick <= 20
                and 1 <= self.max_pages_per_day <= 10_000 and 1 <= self.max_queries_per_day <= 5_000
                and 1 <= self.max_rounds <= 50 and 5 <= self.max_minutes_per_campaign <= 7 * 24 * 60
                and 60 <= self.lease_seconds <= 3600 and 1 <= self.page_runtime_seconds <= 600
                and 0 <= self.max_renders_per_campaign <= 200):
            raise ValueError("unsafe web search limits")


def query_task(campaign: Campaign) -> QueryTask:
    plan = campaign.plan
    return QueryTask(goal=plan.goal, task_text=campaign.source_text, location=plan.location,
                     location_aliases=dict(plan.location_aliases), vertical=plan.vertical,
                     constraints=dict(plan.constraints), languages=tuple(plan.languages),
                     country_code=plan.country)


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
        config: WebSearchConfig | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.campaigns, self.store, self.searcher, self.fetcher, self.generator = (
            campaigns, store, searcher, fetcher, generator)
        self.config, self.now = config or WebSearchConfig(), now
        self.renderer = renderer
        self.renders: dict[str, int] = {}   # browser reads per campaign (this process)
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
                candidates.append(Candidate(hit.url, url_key(hit.url), host_of(hit.url), 0, classify_url(hit.url),
                                            query.id, hit.title, hit.snippet))
            new = await self.store.enqueue(campaign.id, candidates)
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
            ticket = await self.store.begin_fetch(campaign.id, url, vertical=campaign.plan.vertical,
                                                  lease_seconds=cfg.lease_seconds,
                                                  max_runtime_seconds=cfg.page_runtime_seconds)
            if isinstance(ticket, str):
                if ticket == HOST_BLOCKED:  # the site kept refusing us: its search result is all we keep
                    await self._keep_search_result(campaign, url, None)
                elif ticket != BUSY:
                    log.info("web_search.url_skipped %s", ticket, extra={"campaign_id": campaign.id})
                continue
            budget -= 1
            pages += 1
            await self.store.set_progress(campaign.id, url.host, self._line(None, pages, f"сайт {url.host}"))
            result, children = await self._read(campaign.id, url)
            if not result.ok:  # refused (403, a captcha page ...): the listing as the search engine showed it
                result = search_result(url, result.error) or result
            await self.store.finish_fetch(ticket, result)
            if children:
                await self.store.enqueue(campaign.id, children)

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

    async def _read(self, campaign_id: str, url: QueuedUrl) -> tuple[PageResult, list[Candidate]]:
        """Plain HTTP first; Scrapling reads the site's JSON-LD; the browser only when HTTP showed nothing."""
        cfg = self.config
        try:
            page = await asyncio.wait_for(self.fetcher.fetch(url.url), timeout=cfg.page_runtime_seconds)
        except FetchError as exc:
            return PageResult(False, url.kind, url.url, error=exc.code), []
        except TimeoutError:
            return PageResult(False, url.kind, url.url, error="timeout"), []
        parsed = parse_html(page.html, page.url)
        result, children = self._page(url, page.url, parsed, structured(page.html, page.url))
        if not self._wants_render(campaign_id, result, children):
            return result, children
        self.renders[campaign_id] = self.renders.get(campaign_id, 0) + 1
        try:
            rendered = await asyncio.wait_for(self.renderer.render(page.url), timeout=cfg.page_runtime_seconds)
        except (RenderError, TimeoutError) as exc:
            log.info("web_search.render_skipped %s", getattr(exc, "code", "timeout"), extra={"campaign_id": campaign_id})
            return result, children
        seen = ParsedPage(rendered.title or parsed.title, parsed.description, rendered.text,
                          tuple(Link(href, text) for href, text in rendered.links))
        again, more = self._page(url, rendered.url, seen, from_jsonld(rendered.jsonld, rendered.url))
        log.info("web_search.rendered", extra={"campaign_id": campaign_id, "ok": again.ok, "links": len(more)})
        return (again, more) if again.ok and (again.kind == "listing" or more) else (result, children)

    def _wants_render(self, campaign_id: str, result: PageResult, children: list[Candidate]) -> bool:
        """Only a page the site served (HTTP 200) but drew with JavaScript: no text, or a list without links."""
        if self.renderer is None or self.renders.get(campaign_id, 0) >= self.config.max_renders_per_campaign:
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
                children = [Candidate(link, url_key(link), host_of(link), 1, "listing") for link in links]
                return PageResult(True, "index", final_url, parsed.title), children
        text = post_text(parsed, limit=cfg.max_post_chars)
        facts = data.listing_for(final_url)
        if facts:  # the site's own figures (price, area, address) as JSON, on top of the visible text
            text = f"{facts_block(facts)}\n{text}".strip()[:cfg.max_post_chars]
        if len(text) < MIN_POST_CHARS:
            return PageResult(False, "listing", final_url, parsed.title, error="no_readable_text"), []
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


def search_result(url: QueuedUrl, error: str | None) -> PageResult | None:
    """A listing post from what the search engine showed (title, snippet, link) when the site cannot be read.

    Only a search result (not a link found on an index page) that looks like one concrete listing,
    and only when the title and snippet say something. ``error``: why the site's page was not read
    (None: the site was never asked).
    """
    title, snippet = " ".join(url.title.split()), " ".join(url.snippet.split())
    if url.depth != 0 or classify_url(url.url) != "listing" or not snippet or len(title) + len(snippet) < MIN_SNIPPET_CHARS:
        return None
    text = (f"{title}\n{snippet}\n\nСсылка: {url.url}\n"
            "(Страница сайта не прочитана: это заголовок и описание объявления из результатов поиска.)")
    return PageResult(True, "listing", url.url, title, text, error=error, via="search")
