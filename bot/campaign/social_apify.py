"""Instagram and TikTok posts through Apify for each campaign (v3 P1.8, decisions D1 and D2).

On only with ``ACQUISITION_MODE=apify`` and ``APIFY_INSTAGRAM_ENABLED`` / ``APIFY_TIKTOK_ENABLED`` (both off by
default). In that mode the browser social search (``bot.social_search``) is off; this worker takes its place for
Instagram and TikTok and keeps to the social search's own tables (migration 022), so the runner's drain, its status
line and the summary read it as they read the browser search:

* **targets**: one round of queries per campaign and platform from :class:`~bot.social_search.queries.QueryPlanner`
  (the model with ``OPENROUTER_API_KEY``, else the deterministic fallback from the plan's seeds), kept in
  ``social_search_queries``. ``APIFY_SOCIAL_MAX_TARGETS`` per campaign **in all**, dealt to the platforms in turn,
  and fewer when the run caps (``APIFY_*_MAX_CHARGE_USD``) would not fit in ``CAMPAIGN_BUDGET_USD``;
* **one target per paid run** (one hashtag or one set of words), so ``resultsLimit`` / ``resultsPerPage`` is that
  target's own and no target eats another's share; every run is checked against ``costs.over_budget`` before it;
* **posts**: ``collected_posts`` under the platform's ``search`` source -- the same ``monitoring_sources`` row the
  browser search files its posts under (``collected_posts.source_id`` is a NOT NULL FK, ``003_orchestration.sql``),
  linked to the campaign by ``campaign_social_posts``, state ``normalised``: the analysis worker reads them as any
  other post. A URL any source collected already is not collected again;
* **progress**: ``campaign_social_state`` pending -> running -> done. The runner's drain waits for it, at most
  ``CAMPAIGN_SOCIAL_GRACE_SECONDS`` (``runner._drain``).

**Out of the tick.** The paid runs are background tasks (:class:`ApifyTasks`), never awaited by a worker's tick and
never under ``wait_for``: a tick only plans and hands a campaign's targets to its task. The launch of a run is a
critical section of the run journal (:meth:`ApifyTasks.runs`): from ``mark_launching`` (the ``pending:``
placeholder) through the POST to ``save_run`` (the real run id), ``unmark_launching`` (refused, nothing started) or
``finish`` (a lost answer, booked at its cap). On SIGTERM (:meth:`ApifyTasks.begin_stop`) no new target and no new
launch starts and a pause after a 429 ends at once; :meth:`ApifyTasks.aclose` waits for every open section to close
before it cancels anything, so a row is never left ``launching`` by a stop (which would end ``launch_unconfirmed``
with the cap booked while the run goes on in Apify). A task cancelled after that is awaiting a known run or
importing it: the next start plans the target again (``recover_apify``) and ``ApifySocialRun`` resumes the same run
by its input, never launches a second one.

A refused input (``ValueError``, «all links rejected» included) or an Apify failure fails that target only, with a
log line. Any other error (the database while the dataset is imported) gives the target back (``planned``): the run
is paid, so the next tick resumes it. The campaign goes on either way.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Mapping, Sequence
from contextlib import suppress
from typing import Any, Protocol

from bot.campaign.fblinks import post_link_only
from bot.campaign.models import TERMINAL_STATES
from bot.social_search.adapters import PLATFORM_NAMES
from bot.social_search.queries import QueryContext, QueryPlanner
from bot.social_search.store import (
    CampaignSocial,
    MemorySocialStore,
    PlannedQuery,
    PostgresSocialStore,
    _actor,
    _source,
)
from bot.utils import costs

log = logging.getLogger(__name__)

PLATFORMS = ("instagram", "tiktok")
MAX_TARGETS = 10
# How long shutdown waits for a launch in flight (its POST has ApifyHttp's 30 s timeout, plus the journal writes).
SHUTDOWN_SECONDS = 45.0


# --- background runs -------------------------------------------------------------------------------------------


class _GuardedRuns:
    """A ``SourceRunStore`` whose launches are critical sections of :class:`ApifyTasks` (see the module notes)."""

    def __init__(self, store: Any, tasks: ApifyTasks) -> None:
        self._store, self._tasks = store, tasks

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def __repr__(self) -> str:
        return repr(self._store)

    async def mark_launching(self, run: Any) -> str | None:
        if self._tasks.stopping:
            return None  # the service stops: no new launch (the run reads it as «busy», nothing changes)
        self._tasks._enter(run.id)
        try:
            token = await self._store.mark_launching(run)
        except BaseException:
            self._tasks._leave(run.id)
            raise
        if token is None:
            self._tasks._leave(run.id)
        return token

    async def save_run(self, run: Any, run_id: str, dataset_id: str | None) -> None:
        try:
            await self._store.save_run(run, run_id, dataset_id)
        finally:
            self._tasks._leave(run.id)

    async def unmark_launching(self, run: Any, token: str, *, refund: bool = False) -> None:
        try:
            await self._store.unmark_launching(run, token, refund=refund)
        finally:
            self._tasks._leave(run.id)

    async def finish(self, run: Any, status: str, *, billed_usd: float | None = None, error: str | None = None) -> None:
        try:
            await self._store.finish(run, status, billed_usd=billed_usd, error=error)
        finally:
            self._tasks._leave(run.id)


class ApifyTasks:
    """The background tasks of the paid Apify runs of this process (social posts, LinkedIn): one per key."""

    def __init__(self, http: Any) -> None:
        self.http = http
        self.stopping = False
        self._stop = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._launching: set[Any] = set()  # run journal rows between mark_launching and save_run / unmark / finish
        self._idle = asyncio.Event()
        self._idle.set()

    def runs(self, store: Any) -> Any:
        """``store`` (a ``SourceRunStore``) with its launches guarded: every source of this process is built on it."""
        return _GuardedRuns(store, self)

    @property
    def launching(self) -> int:
        return len(self._launching)

    def _enter(self, row_id: Any) -> None:
        self._launching.add(row_id)
        self._idle.clear()

    def _leave(self, row_id: Any) -> None:
        self._launching.discard(row_id)
        if not self._launching:
            self._idle.set()

    def running(self, key: str) -> bool:
        task = self._tasks.get(key)
        return task is not None and not task.done()

    def spawn(self, key: str, work: Coroutine[Any, Any, Any]) -> bool:
        """Start ``work`` as its own task unless one runs for ``key`` (or the service stops)."""
        if self.stopping or self.running(key):
            work.close()
            return False
        task = asyncio.create_task(work, name=f"apify:{key}")
        self._tasks[key] = task
        task.add_done_callback(lambda done: self._done(key, done))
        return True

    def _done(self, key: str, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(key) is task:
            del self._tasks[key]
        if not task.cancelled() and task.exception() is not None:
            log.warning("apify_tasks.failed %s", type(task.exception()).__name__, extra={"key": key})

    async def sleep(self, seconds: float) -> None:
        """The pause between two launches after a 429 (nothing was started): ends the run at once on shutdown."""
        if not self.stopping:
            with suppress(TimeoutError):
                async with asyncio.timeout(max(0.0, seconds)):
                    await self._stop.wait()
        if self.stopping:
            raise asyncio.CancelledError

    async def wait(self) -> None:
        """Every task to its end (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)
            await asyncio.sleep(0)  # the done callbacks drop them

    def begin_stop(self) -> None:
        """SIGTERM: no new target, no new launch, and a 429 pause ends now (synchronous: a signal handler calls it)."""
        self.stopping = True
        self._stop.set()

    async def aclose(self, timeout: float = SHUTDOWN_SECONDS) -> None:
        """Stop; every launch section in flight closes (at most ``timeout``); then the rest is cancelled."""
        self.begin_stop()
        with suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self._idle.wait()
        if self._launching:
            log.warning("apify_tasks.launch_cut", extra={"launches": len(self._launching)})
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# --- the store -------------------------------------------------------------------------------------------------


class ApifySocialStore(Protocol):
    async def open_campaigns(self) -> list[str]: ...
    async def campaign_social(self, campaign_id: str, platform: str) -> CampaignSocial: ...
    async def set_campaign_social(self, campaign_id: str, platform: str, state: str, *, rounds: int | None = None,
                                  current_query: str | None = None, note: str | None = None,
                                  current_url: str | None = None) -> None: ...
    async def used_queries(self, campaign_id: str, platform: str, reuse_days: int) -> list[tuple[str, str, str]]: ...
    async def add_queries(self, campaign_id: str, platform: str, round_no: int, queries: list[Any]) -> int: ...
    async def next_query(self, campaign_id: str, platform: str) -> PlannedQuery | None: ...
    async def start_query(self, query_id: str, profile_id: Any) -> None: ...
    async def finish_query(self, query_id: str, state: str, *, found: int = 0, new: int = 0,
                           error: str | None = None) -> None: ...
    async def recover_apify(self) -> int: ...
    async def save_posts(self, campaign_id: str, vertical: str, posts: Sequence[Any]) -> int: ...


def post_title(item: Any) -> str:
    """«Instagram · Casas Demo» for the card (the name as the network gave it, else the username)."""
    platform = item.post.source.platform
    author = item.author
    who = (author.name or author.profile_key.split(":", 1)[-1]) if author is not None else None
    return f"{PLATFORM_NAMES.get(platform, platform)}{f' · {who}' if who else ''}"[:300]


def _handle(item: Any) -> str | None:
    return item.author.profile_key.split(":", 1)[-1][:200] if item.author is not None else None


class PostgresApifySocialStore(PostgresSocialStore):
    """The social search store (migration 022) plus the Apify posts' write; no new table."""

    async def recover_apify(self) -> int:
        """Startup: a target left running by a stop is planned again (its paid run, if any, is resumed by input)."""
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            status = await conn.execute(
                """update social_search_queries set state = 'planned', started_at = null
                    where state = 'running' and browser_profile_id is null and platform = any($1::text[])""",
                list(PLATFORMS))
            await conn.execute(
                """update campaign_social_state set state = 'pending', current_query = null
                    where state = 'running' and platform = any($1::text[])""", list(PLATFORMS))
        return int(status.split()[-1])

    async def save_posts(self, campaign_id: str, vertical: str, posts: Sequence[Any]) -> int:
        """New posts (``SocialPost``) into ``collected_posts``, linked to the campaign; idempotent (a page twice)."""
        import json

        kept = 0
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            for item in posts:
                platform = item.post.source.platform
                row = item.post.collected_post_row()
                if await conn.fetchval("select 1 from collected_posts where canonical_url = $1 limit 1",
                                       row["canonical_url"]):
                    continue  # collected before, by this run or any source
                source_id = await _source(conn, platform, vertical)
                payload = {**row["raw_payload"], "title": post_title(item)}
                post_id = await conn.fetchval(
                    """insert into collected_posts (source_id, platform_post_id, canonical_url, author_handle,
                                                    published_at, body_text, raw_payload, content_hash, state)
                       values ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
                       on conflict do nothing returning id""",
                    source_id, row["platform_post_id"], row["canonical_url"][:2048], _handle(item),
                    row["published_at"], row["body_text"], json.dumps(payload, ensure_ascii=False),
                    row["content_hash"], row["state"])
                if post_id is None:
                    continue
                await conn.execute(
                    """insert into campaign_social_posts (campaign_id, post_id, platform)
                       values ($1::uuid, $2, $3) on conflict do nothing""", campaign_id, post_id, platform)
                kept += 1
        return kept


class MemoryApifySocialStore(MemorySocialStore):
    """Test double with the rules of :class:`PostgresApifySocialStore`."""

    async def recover_apify(self) -> int:
        count = 0
        for query in self.queries:
            if query.state == "running" and query.profile_id is None and query.platform in PLATFORMS:
                query.state, query.started_at, count = "planned", None, count + 1
        for (_cid, platform), row in self.social.items():
            if row.get("state") == "running" and platform in PLATFORMS:
                row.update(state="pending", current_query=None)
        return count

    async def save_posts(self, campaign_id: str, vertical: str, posts: Sequence[Any]) -> int:
        kept = 0
        for item in posts:
            url = item.post.source.post_url
            if url in self.external_urls or any(p["canonical_url"] == url for p in self.posts.values()):
                continue
            post_id = f"post{len(self.posts) + 1}"
            self.posts[post_id] = {"canonical_url": url, "body_text": item.post.raw_text, "vertical": vertical,
                                   "platform": item.post.source.platform, "title": post_title(item),
                                   "author": _handle(item), "published_at": item.post.source.published_at,
                                   "state": "normalised"}
            self.links.add((campaign_id, post_id))
            kept += 1
        return kept


# --- the worker ------------------------------------------------------------------------------------------------


class Campaigns(Protocol):
    async def get(self, campaign_id: str) -> Any: ...


async def remaining_usd(campaign_id: str, budget_usd: float) -> float | None:
    """What is left of the campaign's ``CAMPAIGN_BUDGET_USD`` (its ledger's spend taken off); None: no limit."""
    sink = costs.ledger()
    if budget_usd <= 0 or sink is None:
        return None
    try:
        spent = await sink.spent(campaign_id)
    except Exception:  # noqa: BLE001 - as costs.over_budget: an unreadable ledger never stops a campaign
        log.warning("social_apify.spent_failed", extra={"campaign_id": campaign_id})
        spent = 0.0
    return budget_usd - spent


def deal_targets(platforms: Sequence[str], caps: Mapping[str, float], max_targets: int,
                 budget_usd: float | None) -> dict[str, int]:
    """Targets per platform: ``max_targets`` dealt in turn; with a budget (None: none), only while the run caps fit
    in it."""
    counts = dict.fromkeys(platforms, 0)
    booked = 0.0
    for n in range(max_targets if platforms else 0):
        platform = platforms[n % len(platforms)]
        if budget_usd is not None and booked + caps[platform] > budget_usd + 1e-9:
            break
        booked += caps[platform]
        counts[platform] += 1
    return counts


class ApifySocialWorker:
    """Plans each open campaign's Instagram/TikTok targets once and runs them in a background task."""

    def __init__(self, store: ApifySocialStore, campaigns: Campaigns, sources: Mapping[str, Any],
                 planner: QueryPlanner, tasks: ApifyTasks, *, max_targets: int = 3, budget_usd: float = 0.0,
                 linkedin_usd: float = 0.0) -> None:
        """``linkedin_usd``: the caps of the LinkedIn runs the reach makes for an investor search (P1.8), kept out of
        the budget the Instagram / TikTok targets are dealt from; 0 without them."""
        unknown = set(sources) - set(PLATFORMS)
        if unknown or not sources:
            raise ValueError("apify social sources are instagram and/or tiktok")
        if not 1 <= max_targets <= MAX_TARGETS:
            raise ValueError(f"APIFY_SOCIAL_MAX_TARGETS must be 1-{MAX_TARGETS}")
        self.store, self.campaigns, self.planner, self.tasks = store, campaigns, planner, tasks
        self.sources = {p: sources[p] for p in PLATFORMS if p in sources}  # Instagram first
        self.max_targets, self.budget_usd, self.linkedin_usd = max_targets, budget_usd, linkedin_usd
        self._recovered = False

    @property
    def platforms(self) -> tuple[str, ...]:
        return tuple(self.sources)

    async def recover(self) -> bool:
        """Startup: targets a stop left running are planned again. Until that works no tick runs: a platform with a
        stale ``running`` target must not be taken for done."""
        try:
            recovered = await self.store.recover_apify()
        except Exception:
            log.exception("social_apify.recover_failed")
            return False
        if recovered:
            log.warning("social_apify.recovered", extra={"queries": recovered})
        self._recovered = True
        return True

    async def serve(self, poll_seconds: float, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                if self._recovered or await self.recover():
                    await self.tick()
            except Exception:  # a database outage delays the runs, it never ends the loop
                log.exception("social_apify.tick_failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)

    async def tick(self) -> int:
        """Hand every open campaign with planned targets to its task (never awaited here); returns how many."""
        started = 0
        for campaign_id in await self.store.open_campaigns():
            if self.tasks.running(f"social:{campaign_id}"):
                continue  # its runs are going on: never a second task, never a second launch
            campaign = await self.campaigns.get(campaign_id)
            if campaign is None or campaign.state in TERMINAL_STATES or post_link_only(campaign):
                continue
            try:
                todo = await self._plan(campaign)
            except Exception:
                log.exception("social_apify.plan_failed", extra={"campaign_id": campaign_id})
                continue
            if todo and self.tasks.spawn(f"social:{campaign_id}", self._campaign(campaign, todo)):
                started += 1
        return started

    async def _plan(self, campaign: Any) -> list[str]:
        """The platforms with targets still to run; the first sight plans them (one round, never again)."""
        shares: dict[str, int] | None = None
        todo: list[str] = []
        for platform in self.platforms:
            social = await self.store.campaign_social(campaign.id, platform)
            if social.state == "done":
                continue
            if social.rounds == 0:
                shares = shares or await self._shares(campaign)
                await self._first_round(campaign, platform, shares[platform])
            if await self.store.next_query(campaign.id, platform) is None:
                await self.store.set_campaign_social(campaign.id, platform, "done")
                continue
            todo.append(platform)
        return todo

    async def _shares(self, campaign: Any) -> dict[str, int]:
        """Targets per platform within what is left of the budget, the LinkedIn runs of an investor search kept out."""
        left = await remaining_usd(campaign.id, self.budget_usd)
        if left is not None and campaign.plan.vertical in ("investors", "both"):
            left -= self.linkedin_usd
        return deal_targets(self.platforms, {p: s.config.max_charge_usd for p, s in self.sources.items()},
                            self.max_targets, left)

    async def _first_round(self, campaign: Any, platform: str, count: int) -> None:
        queries = []
        if count:
            used = await self.store.used_queries(campaign.id, platform, 0)
            queries = await self.planner.round(QueryContext.from_campaign(campaign), platform,
                                               {(kind, key) for kind, key, _ in used}, [t for _, _, t in used], count)
            queries = queries[:count]
        added = await self.store.add_queries(campaign.id, platform, 1, queries) if queries else 0
        await self.store.set_campaign_social(campaign.id, platform, "pending" if added else "done", rounds=1)
        log.info("social_apify.planned", extra={"campaign_id": campaign.id, "platform": platform,
                                                 "targets": [q.text for q in queries]})

    async def _campaign(self, campaign: Any, platforms: Sequence[str]) -> None:
        """The campaign's targets one after another (one paid run each); the platform is done when none is left."""
        for platform in platforms:
            again = False
            while not self.tasks.stopping:
                query = await self.store.next_query(campaign.id, platform)
                if query is None:
                    break
                if not await self._target(campaign, platform, query):
                    again = True  # the run is to be resumed later (a known run, or Apify busy): the next tick
                    break
            if self.tasks.stopping:
                return
            await self.store.set_campaign_social(campaign.id, platform, "pending" if again else "done")

    async def _target(self, campaign: Any, platform: str, query: PlannedQuery) -> bool:
        """One target; False when it is to be tried again later."""
        await self.store.set_campaign_social(campaign.id, platform, "running", current_query=query.text)
        await self.store.start_query(query.id, None)  # no browser profile: a run of Apify
        with costs.scope(campaign.id):
            if await costs.over_budget(campaign.id):
                await costs.skip("api", "budget_cap", item=self.sources[platform].actor_id)
                await self.store.finish_query(query.id, "skipped", error="budget_cap")
                log.info("social_apify.over_budget", extra={"campaign_id": campaign.id, "platform": platform})
                return True
            try:
                result = await self._run(campaign, platform, query)
            except ValueError as exc:  # a refused input («all links rejected» too): this target cannot run
                log.warning("social_apify.target_refused %s", type(exc).__name__,
                            extra={"campaign_id": campaign.id, "platform": platform})
                await self.store.finish_query(query.id, "failed", error=type(exc).__name__)
                return True
            except Exception as exc:  # noqa: BLE001 - the database while importing: the run is paid, resume it later
                log.warning("social_apify.target_interrupted %s", type(exc).__name__,
                            extra={"campaign_id": campaign.id, "platform": platform})
                await self.store.finish_query(query.id, "planned")
                return False
        outcome = result.outcome
        if outcome.status in ("retry", "busy"):
            await self.store.finish_query(query.id, "planned")
            return False
        state = {"succeeded": "done", "skipped": "skipped"}.get(outcome.status, "failed")
        await self.store.finish_query(query.id, state, found=outcome.items_imported, new=result.kept,
                                      error=outcome.error or result.error)
        log.info("social_apify.target_done", extra={"campaign_id": campaign.id, "platform": platform,
                                                     "kind": query.kind, "status": outcome.status,
                                                     "items": outcome.items_imported, "new": result.kept,
                                                     "cost_usd": outcome.cost_usd})
        return True

    async def _run(self, campaign: Any, platform: str, query: PlannedQuery) -> Any:
        vertical = campaign.plan.vertical

        async def save(posts: list[Any], _run_id: str) -> int:
            return await self.store.save_posts(campaign.id, vertical, posts)

        owner = campaign.requested_by if isinstance(campaign.requested_by, int) else None
        source = self.sources[platform]
        if platform == "instagram":  # a hashtag or words: both are the actor's ``search``
            return await source.run(campaign.id, save=save, search=query.text, owner_user_id=owner)
        if query.kind == "hashtag":
            return await source.run(campaign.id, save=save, hashtags=[query.text], owner_user_id=owner)
        return await source.run(campaign.id, save=save, search_queries=[query.text], owner_user_id=owner)


__all__ = ["PLATFORMS", "ApifySocialWorker", "ApifyTasks", "MemoryApifySocialStore", "PostgresApifySocialStore",
           "deal_targets", "post_title", "remaining_usd"]
