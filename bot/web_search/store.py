"""Durable state of the web stage (migration 021) and its in-process twin for tests.

See the header of ``021_campaign_web_search.sql`` for the tables and the
linkage to campaigns. Every write that decides "this page is ours to fetch"
happens in one transaction (``begin_fetch``): the site's source, the campaign's
web batch and batch item, a ``running`` acquisition run and the global claim
of the URL; ``finish_fetch`` stores the post and closes all of them.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from .models import (
    Candidate,
    Counts,
    FetchTicket,
    GeneratedQuery,
    HostVerification,
    PageResult,
    PendingQuery,
    QueuedUrl,
    SiteReport,
    SourceRun,
    Usage,
    WebProgress,
    WebRun,
    WebStatus,
)
from .queries import query_key
from .sources.base import SourceListing

if TYPE_CHECKING:
    from collections.abc import Callable

    import asyncpg

ACTOR = "campaign:web"
BATCH_SIZE = 20          # acquisition_batches.max_items is at most 20
STALE_SECONDS = 3600     # a 'searching' stage untouched this long no longer holds its campaign open
REFUSALS_TO_BLOCK = 3    # consecutive 403/429 from one site block it for BLOCK_HOURS
BLOCK_HOURS = 12
INDEX_TTL_DAYS = 7       # an index (search/list) page is read again after this long; listings never
TERMINAL = ("completed", "cancelled", "failed")
# What counts as a refusal of one layer: an HTTP status, a captcha/anti-bot page (a 200 that is not the
# listing) on the HTTP layer, "render_blocked" (the browser got such a page) on the render layer.
_REFUSALS = ("http_401", "http_403", "http_429", "http_503", "captcha", "render_blocked")
LAYERS = ("http", "render")
# Human verification (bot/web_search/verification.py): the shared browser profile, the job type, and the note
# on a URL that was dropped because nobody passed the site's check.
RENDER_PROFILE = "web-search-render"
JOB_TYPE = "web_challenge"
VERIFICATION_EXPIRED = "verification_expired"
VERIFIED_HOURS = 24      # a passed check is relied on this long; after it the site is read as usual (a new challenge = a new job)
# What the verification flow's classifier turns into the job's challenge kind (``verification.service._announce``).
_JOB_NOTES = {"captcha": "captcha", "interstitial": "security check", "access_denied": "unusual activity"}

# begin_fetch refusals (the URL's campaign row gets this state; 'busy' leaves it queued)
DUPLICATE, SOURCE_UNAVAILABLE, HOST_BLOCKED, BUSY = "duplicate", "source_unavailable", "host_blocked", "busy"
HOST_BREAKER = "host_breaker"  # a URL dropped because its site kept refusing us in this campaign (worker breaker)


class WebStore(Protocol):
    async def claim_source(self, campaign_id: str, name: str) -> tuple[SourceRun, bool]: ...
    async def source_runs(self, campaign_id: str) -> list[SourceRun]: ...
    async def save_source_run(self, campaign_id: str, name: str, run_id: str, dataset_id: str | None) -> None: ...
    async def source_ready(self, campaign_id: str, name: str, listings: list[SourceListing]) -> None: ...
    async def advance_source_import(self, campaign_id: str, name: str, offset: int) -> None: ...
    async def finish_source(self, campaign_id: str, name: str, error_code: str | None = None) -> None: ...
    async def source_available(self, host: str) -> bool: ...

    async def recover(self, lease_seconds: int) -> int: ...
    async def campaign_ids(self) -> list[str]: ...
    async def take_lease(self, campaign_id: str, token: str, seconds: int) -> bool: ...
    async def drop_lease(self, campaign_id: str, token: str) -> None: ...
    async def get_run(self, campaign_id: str) -> WebRun | None: ...
    async def start_run(self, campaign_id: str) -> WebRun: ...
    async def started_at(self, campaign_id: str) -> datetime | None: ...
    async def set_progress(self, campaign_id: str, host: str | None, progress: str) -> None: ...
    async def next_round(self, campaign_id: str) -> int: ...
    async def finish(self, campaign_id: str, state: str, reason: str) -> None: ...
    async def counts(self, campaign_id: str) -> Counts: ...
    async def usage(self) -> Usage: ...
    async def used_queries(self, campaign_id: str) -> list[str]: ...
    async def add_queries(self, campaign_id: str, round_no: int, queries: list[GeneratedQuery], *,
                          reuse_hours: int) -> int: ...
    async def pending_queries(self, campaign_id: str, limit: int) -> list[PendingQuery]: ...
    async def query_done(self, query_id: str, *, ok: bool, results: int, new_urls: int,
                         error: str | None = None) -> None: ...
    async def enqueue(self, campaign_id: str, candidates: list[Candidate], *,
                      index_ttl_days: int = INDEX_TTL_DAYS, layer: str = "http") -> int: ...
    async def next_urls(self, campaign_id: str, limit: int, skip_hosts: frozenset[str] = frozenset()) -> list[QueuedUrl]: ...
    async def host_attempts(self, campaign_id: str, host: str) -> int: ...
    async def mark_rendered(self, campaign_id: str, url_key: str) -> None: ...
    async def renders_used(self, campaign_id: str) -> int: ...
    async def mark_scraped(self, campaign_id: str, url_key: str) -> None: ...
    async def scrapes_used(self, campaign_id: str) -> int: ...
    async def layer_state(self, host: str) -> dict[str, bool]: ...
    async def layer_refused(self, host: str, layer: str) -> None: ...
    async def mark_url(self, campaign_id: str, url_key: str, state: str, detail: str | None = None) -> None: ...
    async def begin_fetch(self, campaign_id: str, url: QueuedUrl, *, vertical: str, lease_seconds: int,
                          max_runtime_seconds: int, contact_site: bool = True,
                          index_ttl_days: int = INDEX_TTL_DAYS, render_layer: bool = False,
                          scrape_layer: bool = False, layer: str = "http") -> FetchTicket | str: ...
    async def finish_fetch(self, ticket: FetchTicket, result: PageResult) -> str | None: ...
    async def web_status(self, campaign_id: str) -> WebStatus | None: ...
    async def site_report(self, campaign_id: str) -> list[SiteReport]: ...
    async def funnel(self, campaign_id: str) -> list[tuple[str, int, int, int]]: ...
    async def host_refusals(self, host: str) -> dict[str, int]: ...
    def note_progress(self, campaign_id: str, progress: WebProgress) -> None: ...
    # human verification (WEB_SEARCH_HUMAN_VERIFICATION)
    async def render_profile(self) -> tuple[str, str]: ...
    async def host_verification(self, host: str, campaign_id: str) -> HostVerification: ...
    async def open_verification(self, host: str, kind: str, url: str) -> str: ...
    async def verification_waiting(self, campaign_id: str) -> list[str]: ...
    async def idle_verification_jobs(self) -> list[str]: ...
    async def verification_busy(self) -> bool: ...
    async def verified_pages(self, host: str, since: datetime) -> int: ...
    async def queued_in(self, campaign_id: str, hosts: list[str]) -> int: ...
    async def skip_host(self, campaign_id: str, host: str, detail: str) -> int: ...
    async def defer_fetch(self, ticket: FetchTicket) -> None: ...


def funnel_totals(rows: list[tuple[str, int, int, int]]) -> tuple[int, int, int, int]:
    """(read, found, sites done, sites known) from ``funnel`` rows ``(host, queued, read, found)``:
    a site is done when none of its links is left in the queue."""
    return (sum(r[2] for r in rows), sum(r[3] for r in rows), sum(1 for r in rows if r[1] == 0), len(rows))


_SITE = re.compile(r"site:(?:www\.)?([a-z0-9.-]+)", re.IGNORECASE)


def _site_queries(rows: list[tuple[str, int]]) -> dict[str, tuple[int, int]]:
    """``site:`` host -> (queries, results) over (query text, result count) rows."""
    out: dict[str, tuple[int, int]] = {}
    for text, results in rows:
        found = _SITE.search(text or "")
        if found:
            host = found.group(1).lower()
            queries, total = out.get(host, (0, 0))
            out[host] = (queries + 1, total + (results or 0))
    return out


def _reports(queries: dict[str, tuple[int, int]], urls: dict[str, list[int]],
             unverified: frozenset[str] = frozenset()) -> list[SiteReport]:
    hosts = sorted(set(queries) | set(urls))
    return [SiteReport(h, *queries.get(h, (0, 0)), *urls.get(h, [0, 0, 0, 0]), h in unverified) for h in hosts]


def _url_bucket(state: str, detail: str | None) -> int | None:
    """Index into [links, read, from_search, refused] beyond ``links``: 1 read, 2 from search, 3 refused."""
    if detail == "search_snippet":
        return 2
    if state == "fetched":
        return 1
    if state in ("failed", "robots") or (state == "skipped" and detail in (HOST_BLOCKED, HOST_BREAKER, VERIFICATION_EXPIRED)):
        return 3
    return None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _raw_payload(ticket: FetchTicket, result: PageResult) -> str:
    return json.dumps({"title": result.title[:500], "platform": "website", "source_type": "web_search",
                       "host": ticket.url.host, "found_by_query": (result.query or "")[:200], "via": result.via,
                       "campaign_id": ticket.campaign_id}, ensure_ascii=False)


class _Duplicate(Exception):
    pass


class _Busy(Exception):
    pass


class PostgresWebStore:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record], *, job_hours: int = 24,
                 human_verification: bool = True) -> None:
        self.pool = pool
        self.job_hours = job_hours  # VERIFICATION_JOB_HOURS: how long an open web_challenge job lives
        self.human_verification = human_verification  # WEB_SEARCH_HUMAN_VERIFICATION: no jobs to report when off
        self.live: dict[str, WebProgress] = {}  # the worker's in-memory progress, shown by ``web_status``

    @staticmethod
    def _source_run(row) -> SourceRun:
        listings = row["listings"]
        if isinstance(listings, str):
            listings = json.loads(listings)
        return SourceRun(str(row["campaign_id"]), row["name"], row["state"], row["run_id"], row["dataset_id"],
                         tuple(SourceListing(**item) for item in listings), row["import_offset"], row["error_code"])

    async def claim_source(self, campaign_id: str, name: str) -> tuple[SourceRun, bool]:
        row = await self.pool.fetchrow(
            """insert into web_listing_source_runs (campaign_id, name) values ($1::uuid, $2)
               on conflict do nothing returning *""", campaign_id, name)
        if row is not None:
            return self._source_run(row), True
        row = await self.pool.fetchrow(
            "select * from web_listing_source_runs where campaign_id=$1::uuid and name=$2", campaign_id, name)
        return self._source_run(row), False

    async def source_runs(self, campaign_id: str) -> list[SourceRun]:
        rows = await self.pool.fetch(
            "select * from web_listing_source_runs where campaign_id=$1::uuid order by name", campaign_id)
        return [self._source_run(row) for row in rows]

    async def save_source_run(self, campaign_id: str, name: str, run_id: str, dataset_id: str | None) -> None:
        await self.pool.execute(
            """update web_listing_source_runs set run_id=$3, dataset_id=coalesce($4,dataset_id),
               state=case when state='starting' then 'running' else state end, updated_at=now()
               where campaign_id=$1::uuid and name=$2 and (run_id is null or run_id=$3)""",
            campaign_id, name, run_id, dataset_id)

    async def source_ready(self, campaign_id: str, name: str, listings: list[SourceListing]) -> None:
        await self.pool.execute(
            """update web_listing_source_runs set state='ready', listings=$3::jsonb, updated_at=now()
               where campaign_id=$1::uuid and name=$2 and state in ('starting', 'running')""",
            campaign_id, name, json.dumps([asdict(item) for item in listings], allow_nan=False))

    async def advance_source_import(self, campaign_id: str, name: str, offset: int) -> None:
        if offset < 0:
            raise ValueError("negative source import offset")
        await self.pool.execute(
            """update web_listing_source_runs set import_offset=greatest(import_offset,$3), updated_at=now()
               where campaign_id=$1::uuid and name=$2 and state='ready'""", campaign_id, name, offset)

    async def finish_source(self, campaign_id: str, name: str, error_code: str | None = None) -> None:
        await self.pool.execute(
            """update web_listing_source_runs set state=$3, error_code=$4, updated_at=now()
               where campaign_id=$1::uuid and name=$2 and state not in ('completed','failed')""",
            campaign_id, name, "failed" if error_code else "completed", error_code)

    def note_progress(self, campaign_id: str, progress: WebProgress) -> None:
        self.live[campaign_id] = progress

    async def source_available(self, host: str) -> bool:
        return not await self.pool.fetchval(
            """select exists(select 1 from monitoring_sources where platform='website' and canonical_url=$1
                              and (state <> 'active' or deleted_at is not null))""", f"https://{host}/")

    async def funnel(self, campaign_id: str) -> list[tuple[str, int, int, int]]:
        """Per site: ``(host, links still queued, pages read from the site, listings found)``."""
        rows = await self.pool.fetch(
            """select host, count(*) filter (where state = 'queued') as queued,
                      count(*) filter (where state = 'fetched' and coalesce(detail, '') <> 'search_snippet') as read,
                      count(*) filter (where state = 'fetched' and kind = 'listing') as found
                 from web_campaign_urls where campaign_id = $1::uuid group by host order by host""", campaign_id)
        return [(r["host"], r["queued"], r["read"], r["found"]) for r in rows]

    async def host_refusals(self, host: str) -> dict[str, int]:
        """The host's consecutive refusals per layer (``http``, ``render``)."""
        row = await self.pool.fetchrow("select http_refusals, render_refusals from web_hosts where host = $1", host)
        return {"http": row["http_refusals"], "render": row["render_refusals"]} if row else {}

    async def recover(self, lease_seconds: int) -> int:
        """Fail web page runs a crashed worker left ``running`` (their URL is claimed again after the lease)."""
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn)
            rows = await conn.fetch(
                """update acquisition_runs r set state = 'failed', finished_at = now(), error_code = 'worker_lost'
                    where r.state = 'running' and r.acquisition_method = 'scrapling'
                      and r.updated_at < now() - make_interval(secs => $1)
                      and exists (select 1 from acquisition_batch_items i join acquisition_batches b on b.id = i.batch_id
                                   where i.id = r.batch_item_id and b.platform = 'website' and b.campaign_id is not null)
                returning r.id""",
                lease_seconds,
            )
        return len(rows)

    async def campaign_ids(self) -> list[str]:
        rows = await self.pool.fetch(
            """select c.id::text from campaigns c left join web_search_runs w on w.campaign_id = c.id
                where (c.state not in ('completed', 'cancelled', 'failed') and (w.state is null or w.state = 'searching'))
                   or (c.state in ('completed', 'cancelled', 'failed') and w.state = 'searching')
                order by c.created_at""")
        return [r["id"] for r in rows]

    async def take_lease(self, campaign_id: str, token: str, seconds: int) -> bool:
        return bool(await self.pool.fetchval(
            """update web_search_runs set lease_token = $2::uuid, lease_until = now() + make_interval(secs => $3),
                      updated_at = now()
                where campaign_id = $1::uuid and (lease_until is null or lease_until < now() or lease_token = $2::uuid)
                returning 1""",
            campaign_id, token, seconds))

    async def drop_lease(self, campaign_id: str, token: str) -> None:
        await self.pool.execute(
            "update web_search_runs set lease_token = null, lease_until = null where campaign_id = $1::uuid and lease_token = $2::uuid",
            campaign_id, token)

    async def get_run(self, campaign_id: str) -> WebRun | None:
        row = await self.pool.fetchrow(
            """select state, rounds, current_host, progress, stop_reason from web_search_runs
                where campaign_id = $1::uuid""", campaign_id)
        return WebRun(campaign_id, row["state"], row["rounds"], row["current_host"], row["progress"],
                      row["stop_reason"]) if row else None

    async def start_run(self, campaign_id: str) -> WebRun:
        await self.pool.execute(
            "insert into web_search_runs (campaign_id) values ($1::uuid) on conflict do nothing", campaign_id)
        run = await self.get_run(campaign_id)
        assert run is not None
        return run

    async def started_at(self, campaign_id: str) -> datetime | None:
        value: datetime | None = await self.pool.fetchval(
            "select started_at from web_search_runs where campaign_id = $1::uuid", campaign_id)
        return value

    async def set_progress(self, campaign_id: str, host: str | None, progress: str) -> None:
        await self.pool.execute(
            """update web_search_runs set current_host = $2, progress = $3, updated_at = now()
                where campaign_id = $1::uuid""", campaign_id, host, progress[:500])

    async def next_round(self, campaign_id: str) -> int:
        return int(await self.pool.fetchval(
            """update web_search_runs set rounds = rounds + 1, updated_at = now()
                where campaign_id = $1::uuid returning rounds""", campaign_id))

    async def finish(self, campaign_id: str, state: str, reason: str) -> None:
        """End the stage: leftover queued URLs are skipped, the campaign's web batches are closed."""
        cancelled = state == "stopped"
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn)
            await conn.execute(
                """update web_search_runs set state = $2, stop_reason = $3, finished_at = now(), current_host = null,
                          updated_at = now()
                    where campaign_id = $1::uuid and state = 'searching'""", campaign_id, state, reason[:200])
            await conn.execute(
                """update web_campaign_urls set state = 'skipped', detail = $2, finished_at = now()
                    where campaign_id = $1::uuid and state = 'queued'""", campaign_id, reason[:80])
            await conn.execute(
                """update web_search_queries set state = 'skipped', error_code = $2
                    where campaign_id = $1::uuid and state = 'pending'""", campaign_id, reason[:80])
            for batch_id in await conn.fetch(
                    """select id from acquisition_batches where campaign_id = $1::uuid and platform = 'website'
                          and state = 'running'""", campaign_id):
                await _close_batch(conn, batch_id["id"], cancelled=cancelled)

    async def counts(self, campaign_id: str) -> Counts:
        row = await self.pool.fetchrow(
            """select (select count(*) from web_search_queries where campaign_id = $1::uuid) as queries,
                      (select count(*) from web_search_queries where campaign_id = $1::uuid and state = 'pending') as pending,
                      (select count(*) from web_campaign_urls where campaign_id = $1::uuid and state in ('fetched', 'failed')) as pages,
                      (select count(*) from web_campaign_urls where campaign_id = $1::uuid and state = 'queued') as queued""",
            campaign_id)
        return Counts(row["queries"], row["pending"], row["pages"], row["queued"])

    async def usage(self) -> Usage:
        row = await self.pool.fetchrow(
            """select (select count(*) from web_seen_urls where finished_at > now() - interval '1 day') as pages,
                      (select count(*) from web_search_queries where searched_at > now() - interval '1 day') as queries""")
        return Usage(row["pages"], row["queries"])

    async def used_queries(self, campaign_id: str) -> list[str]:
        rows = await self.pool.fetch(
            "select query_text from web_search_queries where campaign_id = $1::uuid order by created_at", campaign_id)
        return [r["query_text"] for r in rows]

    async def add_queries(self, campaign_id: str, round_no: int, queries: list[GeneratedQuery], *,
                          reuse_hours: int) -> int:
        """Store a round; with ``reuse_hours`` > 0 a query another campaign searched within it is stored as skipped.

        0 (default): campaigns do not block each other; a campaign's own repeats hit the unique key.
        """
        added = 0
        async with self.pool.acquire() as conn, conn.transaction():
            for query in queries:
                key = query_key(query.text)
                state = await conn.fetchval(
                    """insert into web_search_queries (campaign_id, round_no, query_text, query_key, language, state)
                       values ($1::uuid, $2, $3, $4, $5,
                               case when $6::int > 0 and exists (select 1 from web_search_queries
                                                  where query_key = $4 and state = 'searched'
                                                    and campaign_id <> $1::uuid
                                                    and searched_at > now() - make_interval(hours => $6))
                                    then 'skipped' else 'pending' end)
                       on conflict (campaign_id, query_key) do nothing returning state""",
                    campaign_id, round_no, query.text[:200], key, query.language, reuse_hours)
                added += state == "pending"
        return added

    async def pending_queries(self, campaign_id: str, limit: int) -> list[PendingQuery]:
        rows = await self.pool.fetch(
            """select id::text, query_text, language from web_search_queries
                where campaign_id = $1::uuid and state = 'pending' order by created_at, id limit $2""",
            campaign_id, limit)
        return [PendingQuery(r["id"], r["query_text"], r["language"]) for r in rows]

    async def query_done(self, query_id: str, *, ok: bool, results: int, new_urls: int,
                         error: str | None = None) -> None:
        await self.pool.execute(
            """update web_search_queries set state = $2, result_count = $3, new_urls = $4, error_code = $5,
                      searched_at = now()
                where id = $1::uuid and state = 'pending'""",
            query_id, "searched" if ok else "failed", results, new_urls, (error or None) and error[:80])

    async def enqueue(self, campaign_id: str, candidates: list[Candidate], *,
                      index_ttl_days: int = INDEX_TTL_DAYS, layer: str = "http") -> int:
        """Queue new URLs; one already read by any campaign is recorded as ``duplicate`` and never queued.

        An expired index is queued again. Only API imports may requeue failed listings;
        fetched results (including snippets) and operator-skipped URLs remain protected.

        A URL another worker is reading right now is queued: ``begin_fetch`` decides (duplicate, or a
        takeover when that worker's claim is older than the lease).
        """
        queued = 0
        async with self.pool.acquire() as conn, conn.transaction():
            for c in candidates:
                state = await conn.fetchval(
                    """insert into web_campaign_urls (campaign_id, url_key, url, host, depth, kind, query_id, state,
                                                      detail, finished_at, created_at, search_title, search_snippet)
                       select $1::uuid, $2, $3, $4, $5, $6, $7::uuid, s.state,
                              case when s.state = 'duplicate' then 'seen_before' end,
                              case when s.state = 'duplicate' then now() end, clock_timestamp(),
                              nullif($8, ''), nullif($9, '')
                         from (select case when exists (select 1 from web_seen_urls
                                                         where url_key = $2 and state <> 'fetching'
                                                           and not ($11::boolean and state = 'failed')
                                                           and not ($10::int > 0 and kind = 'index'
                                                                    and finished_at < now() - make_interval(days => $10::int)))
                                           then 'duplicate' else 'queued' end as state) s
                       on conflict (campaign_id, url_key) do update set
                           state='queued', detail=null, finished_at=null, layer=null,
                           url=excluded.url, kind=excluded.kind, depth=excluded.depth,
                           search_title=excluded.search_title, search_snippet=excluded.search_snippet
                         where $11::boolean and (web_campaign_urls.state in ('failed', 'duplicate')
                             or (web_campaign_urls.state='skipped' and web_campaign_urls.detail=any($12::text[])))
                           and exists(select 1 from web_seen_urls where url_key=$2 and state='failed')
                       returning state""",
                    campaign_id, c.url_key, c.url[:2048], c.host, c.depth, c.kind, c.query_id, c.title[:300],
                    c.snippet[:500], index_ttl_days, layer == "api",
                    [HOST_BLOCKED, HOST_BREAKER, VERIFICATION_EXPIRED])
                queued += state == "queued"
        return queued

    async def next_urls(self, campaign_id: str, limit: int, skip_hosts: frozenset[str] = frozenset()) -> list[QueuedUrl]:
        """Queued URLs, one site after another (round-robin), search results before index links.

        ``skip_hosts``: sites that wait for a person's check; their URLs stay queued and are not returned."""
        rows = await self.pool.fetch(
            """select url, url_key, host, depth, kind, search_title, search_snippet from (
                   select *, row_number() over (partition by host order by depth, created_at, url_key) as turn
                     from web_campaign_urls where campaign_id = $1::uuid and state = 'queued'
                      and not (host = any($3::text[]))) q
                order by turn, depth, created_at, url_key limit $2""",
            campaign_id, limit, sorted(skip_hosts))
        return [QueuedUrl(r["url"], r["url_key"], r["host"], r["depth"], r["kind"], r["search_title"] or "",
                          r["search_snippet"] or "") for r in rows]

    async def host_attempts(self, campaign_id: str, host: str) -> int:
        return int(await self.pool.fetchval(
            """select count(*) from web_campaign_urls where campaign_id = $1::uuid and host = $2
                  and state in ('fetched', 'failed')""", campaign_id, host))

    async def mark_rendered(self, campaign_id: str, url_key: str) -> None:
        await self.pool.execute(
            "update web_campaign_urls set rendered = true where campaign_id = $1::uuid and url_key = $2",
            campaign_id, url_key)

    async def renders_used(self, campaign_id: str) -> int:
        return int(await self.pool.fetchval(
            "select count(*) from web_campaign_urls where campaign_id = $1::uuid and rendered", campaign_id))

    async def mark_scraped(self, campaign_id: str, url_key: str) -> None:
        await self.pool.execute(
            "update web_campaign_urls set scraped = true where campaign_id = $1::uuid and url_key = $2",
            campaign_id, url_key)

    async def scrapes_used(self, campaign_id: str) -> int:
        return int(await self.pool.fetchval(
            "select count(*) from web_campaign_urls where campaign_id = $1::uuid and scraped", campaign_id))

    async def layer_state(self, host: str) -> dict[str, bool]:
        """``{"http": open, "render": open}``: False while that layer of the host is blocked (refusals)."""
        row = await self.pool.fetchrow(
            """select coalesce(http_blocked_until > now(), false) as http_blocked,
                      coalesce(render_blocked_until > now(), false) as render_blocked
                 from web_hosts where host = $1""", host)
        return {"http": not (row and row["http_blocked"]), "render": not (row and row["render_blocked"])}

    async def layer_refused(self, host: str, layer: str) -> None:
        """Count a refusal of ``layer`` the worker left behind for the next layer (the last layer goes through ``finish_fetch``)."""
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn)
            await _count_refusal(conn, host, layer, True, render_layer=True)

    async def mark_url(self, campaign_id: str, url_key: str, state: str, detail: str | None = None) -> None:
        await self.pool.execute(
            """update web_campaign_urls set state = $3, detail = $4, finished_at = now()
                where campaign_id = $1::uuid and url_key = $2 and state = 'queued'""",
            campaign_id, url_key, state, detail and detail[:80])

    async def begin_fetch(self, campaign_id: str, url: QueuedUrl, *, vertical: str, lease_seconds: int,
                          max_runtime_seconds: int, contact_site: bool = True,
                          index_ttl_days: int = INDEX_TTL_DAYS, render_layer: bool = False,
                          scrape_layer: bool = False, layer: str = "http") -> FetchTicket | str:
        """Claim ``url`` for one read; ``contact_site`` False: only its search result is stored (a blocked site too).

        HOST_BLOCKED: every enabled layer (HTTP, the browser when ``render_layer``, the scrape API when
        ``scrape_layer``) is blocked for the site. The scrape API has no block: with it on, never HOST_BLOCKED.
        """
        import asyncpg

        try:
            async with self.pool.acquire() as conn, conn.transaction():
                await _set_actor(conn)
                if layer == "api":
                    queued = await conn.fetchrow(
                        "select state,detail from web_campaign_urls where campaign_id=$1::uuid and url_key=$2",
                        campaign_id, url.url_key)
                    if queued and (queued["state"] == "robots" or
                                   (queued["state"] == "skipped" and queued["detail"] not in
                                    (HOST_BLOCKED, HOST_BREAKER, VERIFICATION_EXPIRED))):
                        return queued["detail"] or queued["state"]
                source, refusal = await _site_source(conn, url.host, vertical, contact_site=contact_site and layer != "api",
                                                     render_layer=render_layer, scrape_layer=scrape_layer)
                if refusal is not None:
                    await _mark(conn, campaign_id, url.url_key, "skipped", refusal)
                    return refusal

                item_id = await _batch_item(conn, campaign_id, source, vertical)
                run_id = await conn.fetchval(
                    """insert into acquisition_runs (source_id, batch_item_id, acquisition_method, state, max_pages,
                                                     max_runtime_seconds, started_at)
                       values ($1::uuid, $2::uuid, 'scrapling', 'running', 1, $3, now()) returning id::text""",
                    source, item_id, max_runtime_seconds)
                claimed = await conn.fetchval(
                    """insert into web_seen_urls (url_key, url, host, kind, state, campaign_id)
                       values ($1, $2, $3, $4, 'fetching', $5::uuid)
                       on conflict (url_key) do update set state = 'fetching', claimed_at = now(),
                                                           campaign_id = excluded.campaign_id
                         where (web_seen_urls.state = 'fetching'
                                and web_seen_urls.claimed_at < now() - make_interval(secs => $6))
                            or ($7::int > 0 and web_seen_urls.kind = 'index'
                                and web_seen_urls.state in ('fetched', 'failed')
                                and web_seen_urls.finished_at < now() - make_interval(days => $7::int))
                            or ($8::boolean and web_seen_urls.state = 'failed')
                       returning 1""",
                    url.url_key, url.url[:2048], url.host, url.kind, campaign_id, lease_seconds, index_ttl_days,
                    layer == "api")
                if not claimed:
                    if layer == "api" and await conn.fetchval(
                        "select state = 'fetching' from web_seen_urls where url_key=$1", url.url_key
                    ):
                        raise _Busy
                    raise _Duplicate
                return FetchTicket(campaign_id, url, source, run_id, render_layer)
        except _Busy:
            return BUSY
        except _Duplicate:
            await self.mark_url(campaign_id, url.url_key, DUPLICATE, "seen_before")
            return DUPLICATE
        except asyncpg.UniqueViolationError:
            return BUSY  # another page of this site is being read right now; try later

    async def finish_fetch(self, ticket: FetchTicket, result: PageResult) -> str | None:
        """Store the listing as a post (if any) and close the run, the URL claim and the queue row."""
        post_id: str | None = None
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn)
            if result.ok and result.kind != "index" and result.text:
                post_id = await conn.fetchval(
                    """insert into collected_posts (source_id, acquisition_run_id, platform_post_id, canonical_url,
                                                    body_text, raw_payload, content_hash, state)
                       values ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, $7, 'normalised')
                       on conflict do nothing returning id::text""",
                    ticket.source_id, ticket.run_id, ticket.url.url_key, (result.final_url or ticket.url.url)[:2048],
                    result.text, _raw_payload(ticket, result), content_hash(result.text))
            fetched = result.ok and result.via in ("page", "api")
            contacted = result.layer != "none" and (result.via in ("page", "api") or result.error is not None)
            if result.ok:
                await conn.execute(
                    """update acquisition_runs set state = 'succeeded', finished_at = now()
                        where id = $1::uuid and state = 'running'""", ticket.run_id)
            else:
                await conn.execute(
                    """update acquisition_runs set state = 'failed', finished_at = now(), error_code = $2
                        where id = $1::uuid and state = 'running'""", ticket.run_id, (result.error or "failed")[:80])
            await conn.execute(
                """update web_seen_urls set state = $2, kind = $3, post_id = $4::uuid, error_code = $5,
                          finished_at = now()
                    where url_key = $1""",
                ticket.url.url_key, "fetched" if result.ok else "failed", result.kind, post_id,
                None if result.ok else (result.error or "failed")[:80])
            await conn.execute(
                """update web_campaign_urls set state = $3, kind = $4, detail = $5, layer = $6, finished_at = now()
                    where campaign_id = $1::uuid and url_key = $2""",
                ticket.campaign_id, ticket.url.url_key, "fetched" if result.ok else "failed", result.kind,
                _detail(result), result.layer)
            if not contacted:  # the site was never asked: its counters and block stay as they are
                return post_id
            refused = (result.error or "") in _REFUSALS
            await conn.execute(
                """update web_hosts set last_fetch_at = now(),
                          pages_fetched = pages_fetched + $2::int, pages_failed = pages_failed + (1 - $2::int)
                    where host = $1""", ticket.url.host, int(fetched))
            if result.via != "api":
                await _count_refusal(conn, ticket.url.host, result.layer, refused, render_layer=ticket.render_layer)
            await conn.execute(
                f"update monitoring_sources set {'last_success_at' if fetched else 'last_failure_at'} = now() where id = $1::uuid",
                ticket.source_id)
        return post_id

    async def site_report(self, campaign_id: str) -> list[SiteReport]:
        """Per site: its ``site:`` queries and results, links met, pages read, kept from search, refused."""
        queries = await self.pool.fetch(
            """select query_text, coalesce(result_count, 0) as results from web_search_queries
                where campaign_id = $1::uuid and query_text ilike '%site:%' and state = 'searched'""", campaign_id)
        urls = await self.pool.fetch(
            """select host, state, detail, count(*) as n from web_campaign_urls
                where campaign_id = $1::uuid group by host, state, detail""", campaign_id)
        counts: dict[str, list[int]] = {}
        unverified: set[str] = set()
        for r in urls:
            row = counts.setdefault(r["host"], [0, 0, 0, 0])
            row[0] += r["n"]
            bucket = _url_bucket(r["state"], r["detail"])
            if bucket is not None:
                row[bucket] += r["n"]
            if r["detail"] == VERIFICATION_EXPIRED:
                unverified.add(r["host"])
        return _reports(_site_queries([(r["query_text"], r["results"]) for r in queries]), counts, frozenset(unverified))

    async def web_status(self, campaign_id: str) -> WebStatus | None:
        row = await self.pool.fetchrow(
            """select w.state, w.current_host, w.progress, w.updated_at < now() - make_interval(secs => $2) as stale,
                      c.state as campaign_state
                 from campaigns c left join web_search_runs w on w.campaign_id = c.id
                where c.id = $1::uuid""", campaign_id, STALE_SECONDS)
        if row is None:
            return None
        if row["state"] is None:  # not picked up yet
            return WebStatus(row["campaign_state"] not in TERMINAL, None, "сайты: ждёт запуска")
        active = row["state"] == "searching" and not row["stale"]
        waiting = tuple(await self.verification_waiting(campaign_id)) if active and self.human_verification else ()
        return WebStatus(active, row["current_host"] if active else None, row["progress"] or "",
                         self.live.get(campaign_id), waiting)

    # -- human verification (bot/web_search/verification.py; jobs are handled by bot/verification) --

    async def render_profile(self) -> tuple[str, str]:
        """The browser profile of the render layer as a ``browser_profiles`` row (platform ``website``): the
        verification flow's live browser and watchdog open the profile that is named by this row's id."""
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn)
            await conn.execute(
                """insert into browser_profiles (profile_name, platform, storage_locator, state)
                   values ($1, 'website', 'volume:browser_profiles', 'ready') on conflict (profile_name) do nothing""",
                RENDER_PROFILE)
            row = await conn.fetchrow(
                "select id::text, profile_name from browser_profiles where profile_name = $1 and deleted_at is null",
                RENDER_PROFILE)
        if row is None:
            raise RuntimeError(f"browser profile {RENDER_PROFILE} is deleted")
        return row["id"], row["profile_name"]

    async def host_verification(self, host: str, campaign_id: str) -> HostVerification:
        """The site's latest verification job: open (skip it), verified (read it through the browser), unsolved (it
        ended without a pass after this campaign's web stage started), else none."""
        row = await self.pool.fetchrow(
            """select j.id::text as id, j.state, j.requested_at, j.recovered_at, w.started_at
                 from verification_jobs j join web_hosts h on h.source_id = j.source_id
                 left join web_search_runs w on w.campaign_id = $2::uuid
                where h.host = $1 and j.job_type = $3 order by j.requested_at desc limit 1""",
            host, campaign_id, JOB_TYPE)
        if row is None:
            return HostVerification()
        if row["state"] in ("requested", "active"):
            return HostVerification("open", row["id"])
        if row["state"] == "verified":
            solved = row["recovered_at"] or row["requested_at"]
            fresh = solved > datetime.now(UTC) - timedelta(hours=VERIFIED_HOURS)
            return HostVerification("verified" if fresh else "none", row["id"], solved)
        if row["started_at"] is not None and row["requested_at"] >= row["started_at"]:
            return HostVerification("unsolved", row["id"])
        return HostVerification()

    async def open_verification(self, host: str, kind: str, url: str) -> str:
        """One open verification job per site (a second challenge of the same site finds the first)."""
        profile_id, _ = await self.render_profile()
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn)
            source = await conn.fetchval("select source_id::text from web_hosts where host = $1", host)
            if source is None:
                raise RuntimeError(f"site {host} has no source yet")
            job = await conn.fetchval(
                """insert into verification_jobs (source_id, job_type, state, requested_by, resolution_note,
                                                  browser_profile_id, challenge_kind, target_url, expires_at)
                   values ($1::uuid, $2, 'requested', $3, $4, $5::uuid, null, $6, now() + make_interval(hours => $7::int))
                   on conflict (source_id, job_type) where state in ('requested', 'active') do nothing
                   returning id::text""",
                source, JOB_TYPE, ACTOR, f"{_JOB_NOTES.get(kind, 'security check')} on {host}"[:500], profile_id, url[:2048],
                self.job_hours)
            if job is None:
                job = await conn.fetchval(
                    """select id::text from verification_jobs
                        where source_id = $1::uuid and job_type = $2 and state in ('requested', 'active')""",
                    source, JOB_TYPE)
        return str(job)

    async def verification_waiting(self, campaign_id: str) -> list[str]:
        """Sites with an open verification job that still have URLs of this campaign in the queue."""
        rows = await self.pool.fetch(
            """select distinct h.host from verification_jobs j join web_hosts h on h.source_id = j.source_id
                where j.job_type = $2 and j.state in ('requested', 'active')
                  and exists (select 1 from web_campaign_urls u
                               where u.campaign_id = $1::uuid and u.host = h.host and u.state = 'queued')
                order by h.host""", campaign_id, JOB_TYPE)
        return [r["host"] for r in rows]

    async def idle_verification_jobs(self) -> list[str]:
        """Open web verification jobs of sites that no running web stage has queued URLs for any more."""
        rows = await self.pool.fetch(
            """select j.id::text as id from verification_jobs j join web_hosts h on h.source_id = j.source_id
                where j.job_type = $1 and j.state in ('requested', 'active')
                  and not exists (select 1 from web_campaign_urls u join web_search_runs w on w.campaign_id = u.campaign_id
                                   where u.host = h.host and u.state = 'queued' and w.state = 'searching')
                order by j.requested_at""", JOB_TYPE)
        return [r["id"] for r in rows]

    async def verification_busy(self) -> bool:
        """A person holds a web verification job (the live browser may have the render profile)."""
        return bool(await self.pool.fetchval(
            "select exists (select 1 from verification_jobs where job_type = $1 and state = 'active')", JOB_TYPE))

    async def verified_pages(self, host: str, since: datetime) -> int:
        """Pages of the site read through the browser since ``since`` (any campaign): the budget of one passed check."""
        return int(await self.pool.fetchval(
            """select count(*) from web_campaign_urls
                where host = $1 and layer = 'render' and state in ('fetched', 'failed') and finished_at >= $2""",
            host, since))

    async def queued_in(self, campaign_id: str, hosts: list[str]) -> int:
        return int(await self.pool.fetchval(
            "select count(*) from web_campaign_urls where campaign_id = $1::uuid and state = 'queued' and host = any($2::text[])",
            campaign_id, hosts))

    async def skip_host(self, campaign_id: str, host: str, detail: str) -> int:
        """Drop the site's queued URLs of this campaign (``skipped``, ``detail``): nobody passed its check."""
        status = await self.pool.execute(
            """update web_campaign_urls set state = 'skipped', detail = $3, finished_at = now()
                where campaign_id = $1::uuid and host = $2 and state = 'queued'""", campaign_id, host, detail[:80])
        return int(status.rsplit(" ", 1)[-1])

    async def defer_fetch(self, ticket: FetchTicket) -> None:
        """Give a claimed URL back unread: the run is cancelled, the global claim dropped, the URL stays queued and
        uses no page, host or render budget (the site asked for a person's check)."""
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn)
            await conn.execute(
                """update acquisition_runs set state = 'cancelled', finished_at = now(), stop_reason = 'challenge_deferred'
                    where id = $1::uuid and state = 'running'""", ticket.run_id)
            await conn.execute("delete from web_seen_urls where url_key = $1 and state = 'fetching'", ticket.url.url_key)
            await conn.execute(
                "update web_campaign_urls set rendered = false where campaign_id = $1::uuid and url_key = $2 and state = 'queued'",
                ticket.campaign_id, ticket.url.url_key)


def _detail(result: PageResult) -> str | None:
    """The queue row's note: why a read failed, or that the post came from the search result."""
    if result.via == "search":
        return "search_snippet"
    return None if result.ok else (result.error or "failed")[:80]


async def _count_refusal(conn: asyncpg.Connection[asyncpg.Record], host: str, layer: str, refused: bool, *,
                         render_layer: bool) -> None:
    """One more refusal (or a success: the count restarts) of ``layer``; REFUSALS_TO_BLOCK in a row block that layer.

    ``blocked_until`` is "every enabled layer blocked": set only when HTTP (and the browser, when enabled) are.
    """
    if layer == "http":
        await conn.execute(
            """update web_hosts set consecutive_refusals = case when $2 then consecutive_refusals + 1 else 0 end,
                      http_refusals = case when $2 then http_refusals + 1 else 0 end,
                      http_blocked_until = case when $2 and http_refusals + 1 >= $3
                                                then now() + make_interval(hours => $4) else http_blocked_until end
                where host = $1""", host, refused, REFUSALS_TO_BLOCK, BLOCK_HOURS)
    elif layer == "render":
        await conn.execute(
            """update web_hosts set render_refusals = case when $2 then render_refusals + 1 else 0 end,
                      render_blocked_until = case when $2 and render_refusals + 1 >= $3
                                                  then now() + make_interval(hours => $4) else render_blocked_until end
                where host = $1""", host, refused, REFUSALS_TO_BLOCK, BLOCK_HOURS)
    else:
        return
    await conn.execute(
        """update web_hosts set blocked_until = case when $2 then least(http_blocked_until, render_blocked_until)
                                                     else http_blocked_until end
            where host = $1 and http_blocked_until > now() and (not $2 or render_blocked_until > now())""",
        host, render_layer)


async def _set_actor(conn: asyncpg.Connection[asyncpg.Record]) -> None:
    await conn.execute("select set_config('app.actor', $1, true)", ACTOR)


async def _mark(conn: asyncpg.Connection[asyncpg.Record], campaign_id: str, url_key: str, state: str,
                detail: str) -> None:
    await conn.execute(
        """update web_campaign_urls set state = $3, detail = $4, finished_at = now()
            where campaign_id = $1::uuid and url_key = $2 and state = 'queued'""",
        campaign_id, url_key, state, detail[:80])


async def _site_source(conn: asyncpg.Connection[asyncpg.Record], host: str, vertical: str, *,
                       contact_site: bool = True, render_layer: bool = False,
                       scrape_layer: bool = False) -> tuple[str, str | None]:
    """The site's monitoring source id, created active on first sight; a refusal code when it may not be read.

    An operator can stop the web stage from reading a site by pausing or
    disabling its source (``/pause source:<id>``); a site that kept refusing
    us (403/429) is blocked for a while (unless ``contact_site`` is False: only its search result is kept).
    """
    blocked = contact_site and not scrape_layer and await conn.fetchval(
        """select coalesce(http_blocked_until > now(), false)
                  and (not $2 or coalesce(render_blocked_until > now(), false))
             from web_hosts where host = $1""", host, render_layer)
    if blocked:
        return "", HOST_BLOCKED
    url = f"https://{host}/"
    source_id = await conn.fetchval(
        """insert into monitoring_sources (platform, source_kind, vertical, canonical_url, display_name,
                                           acquisition_method, state, configuration)
           values ('website', 'website', $1, $2, $3, 'scrapling', 'active', '{"web_search": true}'::jsonb)
           on conflict (platform, canonical_url) do nothing returning id::text""",
        vertical, url, host)
    if source_id is None:
        row = await conn.fetchrow(
            """select id::text, state, deleted_at, vertical from monitoring_sources
                where platform = 'website' and canonical_url = $1 for update""", url)
        assert row is not None
        if row["deleted_at"] is not None or row["state"] != "active":
            return "", SOURCE_UNAVAILABLE
        source_id = row["id"]
        if row["vertical"] not in (vertical, "both"):  # a site read for both kinds of campaign
            await conn.execute("update monitoring_sources set vertical = 'both' where id = $1::uuid", source_id)
    await conn.execute(
        """insert into web_hosts (host, source_id) values ($1, $2::uuid)
           on conflict (host) do update set source_id = excluded.source_id
             where web_hosts.source_id is distinct from excluded.source_id""", host, source_id)
    return str(source_id), None


async def _batch_item(conn: asyncpg.Connection[asyncpg.Record], campaign_id: str, source_id: str, vertical: str) -> str:
    """The campaign's open web batch item for this site; a full batch (20 sites) is closed and a new one opened."""
    batch = await conn.fetchrow(
        """select id, (select count(*) from acquisition_batch_items i where i.batch_id = b.id) as items
             from acquisition_batches b
            where campaign_id = $1::uuid and platform = 'website' and state = 'running'
            order by created_at desc limit 1 for update""", campaign_id)
    if batch is not None:
        item = await conn.fetchval(
            "select id::text from acquisition_batch_items where batch_id = $1 and source_id = $2::uuid",
            batch["id"], source_id)
        if item is not None:
            return str(item)
        if batch["items"] >= BATCH_SIZE:
            await _close_batch(conn, batch["id"], cancelled=False)
            batch = None
    if batch is None:
        batch_id = await conn.fetchval(
            """insert into acquisition_batches (platform, acquisition_method, vertical, state, max_items, requested_by,
                                                campaign_id)
               values ('website', 'scrapling', $1, 'planned', $2, $3, $4::uuid) returning id""",
            vertical, BATCH_SIZE, ACTOR, campaign_id)
        await conn.execute("update acquisition_batches set state = 'queued' where id = $1", batch_id)
        await conn.execute("update acquisition_batches set state = 'running', started_at = now() where id = $1", batch_id)
        count = 0
    else:
        batch_id, count = batch["id"], batch["items"]
    return str(await conn.fetchval(
        """insert into acquisition_batch_items (batch_id, source_id, sequence_no) values ($1, $2::uuid, $3)
           returning id""", batch_id, source_id, count + 1))


async def _close_batch(conn: asyncpg.Connection[asyncpg.Record], batch_id: object, *, cancelled: bool) -> None:
    """Items that produced a page succeed, the rest are skipped (or cancelled); then the batch ends."""
    items = await conn.fetch(
        """select i.id, exists (select 1 from acquisition_runs r where r.batch_item_id = i.id and r.state = 'succeeded') as ok
             from acquisition_batch_items i where i.batch_id = $1 and i.state = 'queued' order by i.sequence_no""",
        batch_id)
    for item in items:
        if cancelled:
            await conn.execute("update acquisition_batch_items set state = 'cancelled' where id = $1", item["id"])
        elif item["ok"]:
            await conn.execute("update acquisition_batch_items set state = 'running', started_at = now() where id = $1",
                               item["id"])
            await conn.execute(
                "update acquisition_batch_items set state = 'succeeded', finished_at = now() where id = $1", item["id"])
        else:
            await conn.execute(
                "update acquisition_batch_items set state = 'skipped', last_error_code = 'no_page' where id = $1",
                item["id"])
    await conn.execute(
        "update acquisition_batches set state = $2, finished_at = now() where id = $1 and state = 'running'",
        batch_id, "cancelled" if cancelled else "succeeded")


# --- in-process twin ------------------------------------------------------------------------


@dataclass
class _MemUrl:
    url: str
    url_key: str
    host: str
    depth: int
    kind: str
    query_id: str | None
    state: str
    order: int
    detail: str | None = None
    title: str = ""
    snippet: str = ""
    rendered: bool = False
    scraped: bool = False
    layer: str | None = None  # the fetch layer that read it (campaign_metrics)
    finished_at: datetime | None = None


@dataclass
class _MemQuery:
    id: str
    text: str
    key: str
    language: str | None
    round_no: int
    state: str = "pending"
    searched_at: datetime | None = None
    results: int = 0
    new_urls: int = 0


@dataclass
class MemoryWebStore:
    """In-process twin of ``PostgresWebStore``: same de-duplication rules, no batches."""

    campaigns: object | None = None  # a MemoryCampaignStore, for campaign_ids()/web_status()
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sources: dict[tuple[str, str], SourceRun] = field(default_factory=dict)
    runs: dict[str, WebRun] = field(default_factory=dict)
    started: dict[str, datetime] = field(default_factory=dict)
    leases: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    queries: dict[str, list[_MemQuery]] = field(default_factory=dict)
    urls: dict[str, dict[str, _MemUrl]] = field(default_factory=dict)
    seen: dict[str, dict[str, object]] = field(default_factory=dict)      # url_key -> row
    hosts: dict[str, dict[str, object]] = field(default_factory=dict)
    posts: list[dict[str, object]] = field(default_factory=list)
    paused_hosts: set[str] = field(default_factory=set)
    busy_hosts: set[str] = field(default_factory=set)
    finished_fetches: list[str] = field(default_factory=list)  # url_keys, in order
    deferred_fetches: list[str] = field(default_factory=list)  # url_keys given back unread (a site's check was pending)
    live: dict[str, WebProgress] = field(default_factory=dict)
    verification_jobs: list[dict[str, object]] = field(default_factory=list)  # the web_challenge jobs, oldest first
    job_hours: int = 24
    human_verification: bool = True
    _order: int = 0

    async def claim_source(self, campaign_id: str, name: str) -> tuple[SourceRun, bool]:
        key = (campaign_id, name)
        fresh = key not in self.sources
        if fresh:
            self.sources[key] = SourceRun(campaign_id, name)
        return self.sources[key], fresh

    async def source_runs(self, campaign_id: str) -> list[SourceRun]:
        return sorted((run for (cid, _), run in self.sources.items() if cid == campaign_id), key=lambda r: r.name)

    async def save_source_run(self, campaign_id: str, name: str, run_id: str, dataset_id: str | None) -> None:
        key = (campaign_id, name)
        row = self.sources.get(key)
        if row is not None and row.run_id in (None, run_id):
            self.sources[key] = replace(row, run_id=run_id, dataset_id=dataset_id or row.dataset_id,
                                        state="running" if row.state == "starting" else row.state)

    async def source_ready(self, campaign_id: str, name: str, listings: list[SourceListing]) -> None:
        key = (campaign_id, name)
        row = self.sources.get(key)
        if row is not None and row.state in ("starting", "running"):
            # Match the persisted snapshot: caller mutations cannot alter stored facts.
            snapshot = json.loads(json.dumps([asdict(item) for item in listings], allow_nan=False))
            self.sources[key] = replace(row, state="ready", listings=tuple(SourceListing(**item) for item in snapshot))

    async def advance_source_import(self, campaign_id: str, name: str, offset: int) -> None:
        if offset < 0:
            raise ValueError("negative source import offset")
        key = (campaign_id, name)
        row = self.sources.get(key)
        if row is not None and row.state == "ready":
            if offset > len(row.listings):
                raise ValueError("source import offset exceeds snapshot")
            self.sources[key] = replace(row, import_offset=max(row.import_offset, offset))

    async def finish_source(self, campaign_id: str, name: str, error_code: str | None = None) -> None:
        key = (campaign_id, name)
        row = self.sources.get(key)
        if row is not None and row.state not in ("completed", "failed"):
            self.sources[key] = replace(row, state="failed" if error_code else "completed", error_code=error_code)

    def note_progress(self, campaign_id: str, progress: WebProgress) -> None:
        self.live[campaign_id] = progress

    async def source_available(self, host: str) -> bool:
        return host not in self.paused_hosts

    async def funnel(self, campaign_id: str) -> list[tuple[str, int, int, int]]:
        hosts: dict[str, list[int]] = {}
        for u in self.urls.get(campaign_id, {}).values():
            row = hosts.setdefault(u.host, [0, 0, 0])
            row[0] += u.state == "queued"
            row[1] += u.state == "fetched" and (u.detail or "") != "search_snippet"
            row[2] += u.state == "fetched" and u.kind == "listing"
        return [(h, *row) for h, row in sorted(hosts.items())]  # type: ignore[misc]

    async def host_refusals(self, host: str) -> dict[str, int]:
        row = self.hosts.get(host)
        return {"http": int(row["http_refusals"]), "render": int(row["render_refusals"])} if row else {}  # type: ignore[call-overload]

    def _campaign_state(self, campaign_id: str) -> str | None:
        campaign = getattr(self.campaigns, "campaigns", {}).get(campaign_id)
        return campaign.state if campaign is not None else None

    async def recover(self, lease_seconds: int) -> int:
        return 0

    async def campaign_ids(self) -> list[str]:
        items = getattr(self.campaigns, "campaigns", {})
        out = []
        for c in sorted(items.values(), key=lambda c: c.created_at):
            run = self.runs.get(c.id)
            if (c.state not in TERMINAL and (run is None or run.state == "searching")) or (
                    c.state in TERMINAL and run is not None and run.state == "searching"):
                out.append(c.id)
        return out

    async def take_lease(self, campaign_id: str, token: str, seconds: int) -> bool:
        held = self.leases.get(campaign_id)
        if held is not None and held[0] != token and held[1] > self.now():
            return False
        self.leases[campaign_id] = (token, self.now() + timedelta(seconds=seconds))
        return campaign_id in self.runs

    async def drop_lease(self, campaign_id: str, token: str) -> None:
        if self.leases.get(campaign_id, ("",))[0] == token:
            del self.leases[campaign_id]

    async def get_run(self, campaign_id: str) -> WebRun | None:
        return self.runs.get(campaign_id)

    async def start_run(self, campaign_id: str) -> WebRun:
        if campaign_id not in self.runs:
            self.runs[campaign_id] = WebRun(campaign_id, "searching")
            self.started[campaign_id] = self.now()
        return self.runs[campaign_id]

    async def started_at(self, campaign_id: str) -> datetime | None:
        return self.started.get(campaign_id)

    async def set_progress(self, campaign_id: str, host: str | None, progress: str) -> None:
        self.runs[campaign_id] = replace(self.runs[campaign_id], host=host, progress=progress[:500])

    async def next_round(self, campaign_id: str) -> int:
        run = self.runs[campaign_id]
        self.runs[campaign_id] = replace(run, rounds=run.rounds + 1)
        return run.rounds + 1

    async def finish(self, campaign_id: str, state: str, reason: str) -> None:
        run = self.runs.get(campaign_id)
        if run is None or run.state != "searching":
            return
        self.runs[campaign_id] = replace(run, state=state, stop_reason=reason, host=None)  # type: ignore[arg-type]
        for row in self.urls.get(campaign_id, {}).values():
            if row.state == "queued":
                row.state, row.detail = "skipped", reason
        for query in self.queries.get(campaign_id, []):
            if query.state == "pending":
                query.state = "skipped"

    async def counts(self, campaign_id: str) -> Counts:
        queries = self.queries.get(campaign_id, [])
        urls = self.urls.get(campaign_id, {}).values()
        return Counts(len(queries), sum(q.state == "pending" for q in queries),
                      sum(u.state in ("fetched", "failed") for u in urls), sum(u.state == "queued" for u in urls))

    async def usage(self) -> Usage:
        since = self.now() - timedelta(days=1)
        pages = sum(1 for row in self.seen.values() if isinstance(row.get("finished_at"), datetime)
                    and row["finished_at"] > since)  # type: ignore[operator]
        queries = sum(1 for qs in self.queries.values() for q in qs if q.searched_at and q.searched_at > since)
        return Usage(pages, queries)

    async def used_queries(self, campaign_id: str) -> list[str]:
        return [q.text for q in self.queries.get(campaign_id, [])]

    async def add_queries(self, campaign_id: str, round_no: int, queries: list[GeneratedQuery], *,
                          reuse_hours: int) -> int:
        rows = self.queries.setdefault(campaign_id, [])
        since = self.now() - timedelta(hours=reuse_hours)
        added = 0
        for query in queries:
            key = query_key(query.text)
            if any(q.key == key for q in rows):
                continue
            recent = reuse_hours > 0 and any(
                q.key == key and q.state == "searched" and q.searched_at and q.searched_at > since
                for other, qs in self.queries.items() if other != campaign_id for q in qs)
            rows.append(_MemQuery(str(uuid.uuid4()), query.text, key, query.language, round_no,
                                  "skipped" if recent else "pending"))
            added += not recent
        return added

    async def pending_queries(self, campaign_id: str, limit: int) -> list[PendingQuery]:
        return [PendingQuery(q.id, q.text, q.language) for q in self.queries.get(campaign_id, [])
                if q.state == "pending"][:limit]

    async def query_done(self, query_id: str, *, ok: bool, results: int, new_urls: int,
                         error: str | None = None) -> None:
        for qs in self.queries.values():
            for q in qs:
                if q.id == query_id and q.state == "pending":
                    q.state, q.results, q.new_urls, q.searched_at = ("searched" if ok else "failed"), results, new_urls, self.now()

    async def enqueue(self, campaign_id: str, candidates: list[Candidate], *,
                      index_ttl_days: int = INDEX_TTL_DAYS, layer: str = "http") -> int:
        rows = self.urls.setdefault(campaign_id, {})
        queued = 0
        for c in candidates:
            seen = self.seen.get(c.url_key)
            api_retry = layer == "api" and seen is not None and seen["state"] == "failed"
            if c.url_key in rows:
                row = rows[c.url_key]
                if api_retry and (row.state in ("failed", "duplicate") or
                                  (row.state == "skipped" and row.detail in
                                   (HOST_BLOCKED, HOST_BREAKER, VERIFICATION_EXPIRED))):
                    row.state, row.detail, row.finished_at, row.layer = "queued", None, None, None
                    row.url, row.kind, row.depth = c.url, c.kind, c.depth
                    row.title, row.snippet = c.title, c.snippet
                    queued += 1
                continue
            self._order += 1
            expired = seen is not None and self._index_expired(seen, index_ttl_days)
            state = "duplicate" if seen is not None and seen["state"] != "fetching" and not expired and not api_retry else "queued"
            rows[c.url_key] = _MemUrl(c.url, c.url_key, c.host, c.depth, c.kind, c.query_id, state, self._order,
                                      "seen_before" if state == "duplicate" else None, c.title, c.snippet)
            queued += state == "queued"
        return queued

    async def next_urls(self, campaign_id: str, limit: int, skip_hosts: frozenset[str] = frozenset()) -> list[QueuedUrl]:
        queued = sorted((u for u in self.urls.get(campaign_id, {}).values()
                         if u.state == "queued" and u.host not in skip_hosts),
                        key=lambda u: (u.depth, u.order))
        turns: dict[str, int] = {}
        ranked = []
        for u in queued:
            turns[u.host] = turns.get(u.host, 0) + 1
            ranked.append((turns[u.host], u.depth, u.order, u))
        ranked.sort(key=lambda t: t[:3])
        return [QueuedUrl(u.url, u.url_key, u.host, u.depth, u.kind, u.title, u.snippet)  # type: ignore[arg-type]
                for *_, u in ranked[:limit]]

    def _index_expired(self, seen: dict[str, object], index_ttl_days: int) -> bool:
        finished = seen.get("finished_at")
        return bool(index_ttl_days > 0 and seen.get("kind") == "index" and seen["state"] in ("fetched", "failed")
                    and isinstance(finished, datetime) and finished < self.now() - timedelta(days=index_ttl_days))

    async def mark_rendered(self, campaign_id: str, url_key: str) -> None:
        row = self.urls.get(campaign_id, {}).get(url_key)
        if row is not None:
            row.rendered = True

    async def renders_used(self, campaign_id: str) -> int:
        return sum(1 for u in self.urls.get(campaign_id, {}).values() if u.rendered)

    async def mark_scraped(self, campaign_id: str, url_key: str) -> None:
        row = self.urls.get(campaign_id, {}).get(url_key)
        if row is not None:
            row.scraped = True

    async def scrapes_used(self, campaign_id: str) -> int:
        return sum(1 for u in self.urls.get(campaign_id, {}).values() if u.scraped)

    def _host(self, host: str) -> dict[str, object]:
        return self.hosts.setdefault(host, {"fetched": 0, "failed": 0, "refusals": 0, "blocked_until": None,
                                            "http_refusals": 0, "http_blocked_until": None,
                                            "render_refusals": 0, "render_blocked_until": None})

    def _blocked(self, host: dict[str, object], layer: str) -> bool:
        until = host[f"{layer}_blocked_until"]
        return isinstance(until, datetime) and until > self.now()

    async def layer_state(self, host: str) -> dict[str, bool]:
        row = self.hosts.get(host)
        return {"http": not (row and self._blocked(row, "http")), "render": not (row and self._blocked(row, "render"))}

    async def layer_refused(self, host: str, layer: str) -> None:
        self._count_refusal(host, layer, True, render_layer=True)

    def _count_refusal(self, host_name: str, layer: str, refused: bool, *, render_layer: bool) -> None:
        host = self._host(host_name)
        if layer not in LAYERS:
            return
        host[f"{layer}_refusals"] = host[f"{layer}_refusals"] + 1 if refused else 0  # type: ignore[operator]
        if layer == "http":
            host["refusals"] = host["http_refusals"]
        if refused and host[f"{layer}_refusals"] >= REFUSALS_TO_BLOCK:  # type: ignore[operator]
            host[f"{layer}_blocked_until"] = self.now() + timedelta(hours=BLOCK_HOURS)
        if self._blocked(host, "http") and (not render_layer or self._blocked(host, "render")):
            host["blocked_until"] = (min(host["http_blocked_until"], host["render_blocked_until"])  # type: ignore[type-var]
                                     if render_layer else host["http_blocked_until"])

    async def host_attempts(self, campaign_id: str, host: str) -> int:
        return sum(1 for u in self.urls.get(campaign_id, {}).values()
                   if u.host == host and u.state in ("fetched", "failed"))

    async def mark_url(self, campaign_id: str, url_key: str, state: str, detail: str | None = None) -> None:
        row = self.urls.get(campaign_id, {}).get(url_key)
        if row is not None and row.state == "queued":
            row.state, row.detail = state, detail

    async def begin_fetch(self, campaign_id: str, url: QueuedUrl, *, vertical: str, lease_seconds: int,
                          max_runtime_seconds: int, contact_site: bool = True,
                          index_ttl_days: int = INDEX_TTL_DAYS, render_layer: bool = False,
                          scrape_layer: bool = False, layer: str = "http") -> FetchTicket | str:
        queued = self.urls.get(campaign_id, {}).get(url.url_key)
        if layer == "api" and queued is not None and (queued.state == "robots" or
            (queued.state == "skipped" and queued.detail not in (HOST_BLOCKED, HOST_BREAKER, VERIFICATION_EXPIRED))):
            return queued.detail or queued.state
        host = self._host(url.host)
        if (contact_site and layer != "api" and not scrape_layer and self._blocked(host, "http")
                and (not render_layer or self._blocked(host, "render"))):
            await self.mark_url(campaign_id, url.url_key, "skipped", HOST_BLOCKED)
            return HOST_BLOCKED
        if url.host in self.paused_hosts:
            await self.mark_url(campaign_id, url.url_key, "skipped", SOURCE_UNAVAILABLE)
            return SOURCE_UNAVAILABLE
        if url.host in self.busy_hosts:
            return BUSY
        row = self.seen.get(url.url_key)
        stale = row is not None and row["state"] == "fetching" and row["claimed_at"] < self.now() - timedelta(seconds=lease_seconds)  # type: ignore[operator]
        api_retry = layer == "api" and row is not None and row["state"] == "failed"
        if layer == "api" and row is not None and row["state"] == "fetching" and not stale:
            return BUSY
        if row is not None and not (stale or self._index_expired(row, index_ttl_days) or api_retry):
            await self.mark_url(campaign_id, url.url_key, DUPLICATE, "seen_before")
            return DUPLICATE
        self.seen[url.url_key] = {"url": url.url, "host": url.host, "state": "fetching", "campaign_id": campaign_id,
                                  "claimed_at": self.now(), "finished_at": None, "post_id": None}
        return FetchTicket(campaign_id, url, f"source:{url.host}", str(uuid.uuid4()), render_layer)

    async def finish_fetch(self, ticket: FetchTicket, result: PageResult) -> str | None:
        post_id: str | None = None
        duplicate_text = any(p["source_id"] == ticket.source_id and p["content_hash"] == content_hash(result.text)
                             for p in self.posts)
        if result.ok and result.kind != "index" and result.text and not duplicate_text:
            post_id = str(uuid.uuid4())
            self.posts.append({"id": post_id, "campaign_id": ticket.campaign_id, "source_id": ticket.source_id,
                               "url": result.final_url or ticket.url.url, "text": result.text,
                               "title": result.title, "content_hash": content_hash(result.text), "via": result.via})
        self.seen[ticket.url.url_key].update(state="fetched" if result.ok else "failed", finished_at=self.now(),
                                             post_id=post_id, kind=result.kind)
        row = self.urls[ticket.campaign_id][ticket.url.url_key]
        row.state, row.kind = ("fetched" if result.ok else "failed"), result.kind
        row.detail = _detail(result)
        row.layer = result.layer
        row.finished_at = self.now()
        self.finished_fetches.append(ticket.url.url_key)
        if result.layer == "none" or (result.via == "search" and result.error is None):  # the site was never asked
            return post_id
        host = self._host(ticket.url.host)
        host["fetched" if result.ok and result.via in ("page", "api") else "failed"] += 1  # type: ignore[operator]
        if result.via != "api":
            self._count_refusal(ticket.url.host, result.layer, (result.error or "") in _REFUSALS,
                                render_layer=ticket.render_layer)
        return post_id

    async def site_report(self, campaign_id: str) -> list[SiteReport]:
        queries = [(q.text, q.results) for q in self.queries.get(campaign_id, []) if q.state == "searched"]
        counts: dict[str, list[int]] = {}
        unverified: set[str] = set()
        for u in self.urls.get(campaign_id, {}).values():
            row = counts.setdefault(u.host, [0, 0, 0, 0])
            row[0] += 1
            bucket = _url_bucket(u.state, u.detail)
            if bucket is not None:
                row[bucket] += 1
            if u.detail == VERIFICATION_EXPIRED:
                unverified.add(u.host)
        return _reports(_site_queries(queries), counts, frozenset(unverified))

    async def web_status(self, campaign_id: str) -> WebStatus | None:
        state = self._campaign_state(campaign_id)
        run = self.runs.get(campaign_id)
        if run is None:
            return WebStatus(state not in TERMINAL, None, "сайты: ждёт запуска") if state else None
        active = run.state == "searching"
        waiting = tuple(await self.verification_waiting(campaign_id)) if active and self.human_verification else ()
        return WebStatus(active, run.host if active else None, run.progress or "", self.live.get(campaign_id), waiting)

    # -- human verification: the jobs the verification service would hold, as plain dicts (tests drive their state) --

    async def render_profile(self) -> tuple[str, str]:
        return "00000000-0000-0000-0000-00000000beef", RENDER_PROFILE

    def _jobs_of(self, host: str) -> list[dict[str, object]]:
        return [j for j in self.verification_jobs if j["host"] == host]

    async def host_verification(self, host: str, campaign_id: str) -> HostVerification:
        jobs = self._jobs_of(host)
        if not jobs:
            return HostVerification()
        job = jobs[-1]
        state = str(job["state"])
        if state in ("requested", "active"):
            return HostVerification("open", str(job["id"]))
        if state == "verified":
            solved = job["recovered_at"]
            assert isinstance(solved, datetime)
            fresh = solved > self.now() - timedelta(hours=VERIFIED_HOURS)
            return HostVerification("verified" if fresh else "none", str(job["id"]), solved)
        started = self.started.get(campaign_id)
        requested = job["requested_at"]
        assert isinstance(requested, datetime)
        return HostVerification("unsolved", str(job["id"])) if started is not None and requested >= started else HostVerification()

    async def open_verification(self, host: str, kind: str, url: str) -> str:
        for job in self._jobs_of(host):
            if job["state"] in ("requested", "active"):
                return str(job["id"])
        job = {"id": str(uuid.uuid4()), "host": host, "kind": kind, "url": url, "state": "requested",
               "requested_at": self.now(), "recovered_at": None,
               "expires_at": self.now() + timedelta(hours=self.job_hours)}
        self.verification_jobs.append(job)
        return str(job["id"])

    def set_job_state(self, host: str, state: str) -> None:
        """Test helper: what the verification service does to the site's latest job (active, verified, expired ...)."""
        job = self._jobs_of(host)[-1]
        job["state"] = state
        if state == "verified":
            job["recovered_at"] = self.now()

    async def verification_waiting(self, campaign_id: str) -> list[str]:
        queued = {u.host for u in self.urls.get(campaign_id, {}).values() if u.state == "queued"}
        return sorted({str(j["host"]) for j in self.verification_jobs if j["state"] in ("requested", "active")} & queued)

    async def idle_verification_jobs(self) -> list[str]:
        live = {c for c, run in self.runs.items() if run.state == "searching"}
        queued = {u.host for c in live for u in self.urls.get(c, {}).values() if u.state == "queued"}
        return [str(j["id"]) for j in self.verification_jobs
                if j["state"] in ("requested", "active") and j["host"] not in queued]

    async def verification_busy(self) -> bool:
        return any(j["state"] == "active" for j in self.verification_jobs)

    async def verified_pages(self, host: str, since: datetime) -> int:
        return sum(1 for rows in self.urls.values() for u in rows.values()
                   if u.host == host and u.layer == "render" and u.state in ("fetched", "failed")
                   and u.finished_at is not None and u.finished_at >= since)

    async def queued_in(self, campaign_id: str, hosts: list[str]) -> int:
        return sum(1 for u in self.urls.get(campaign_id, {}).values() if u.state == "queued" and u.host in hosts)

    async def skip_host(self, campaign_id: str, host: str, detail: str) -> int:
        rows = [u for u in self.urls.get(campaign_id, {}).values() if u.host == host and u.state == "queued"]
        for u in rows:
            u.state, u.detail = "skipped", detail
        return len(rows)

    async def defer_fetch(self, ticket: FetchTicket) -> None:
        self.seen.pop(ticket.url.url_key, None)
        row = self.urls.get(ticket.campaign_id, {}).get(ticket.url.url_key)
        if row is not None and row.state == "queued":
            row.rendered = False
        self.deferred_fetches.append(ticket.url.url_key)
