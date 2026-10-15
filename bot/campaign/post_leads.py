"""Investor leads from the comments under a Facebook post the user sent by link (v2 B1).

``APIFY_FACEBOOK_COMMENTS_ENABLED`` (off by default). The intake turns a post link into an investor campaign whose
``spec.sources.required`` holds the link. The :class:`PostLeadWorker` (a loop beside the comment worker in
``campaign.runner.main``) queues each such link once in ``campaign_post_reads`` (migration 048), reads the comments
through Apify (``bot.acquisition.apify_facebook_comments``), asks the same judge as the browser comment worker who is
a possible investor or buyer (the deterministic rules when there is no key or the call fails) and stores the leads
in ``investor_leads`` for the campaign. The runner then sends one card per person (``runner._people``).

Safe to repeat: the run id is stored BEFORE the run is awaited, so a second tick, a restart or a failed later step
resumes the same run and never launches a second paid one (a lost launch answer is booked at its cap and the read
fails). Its cost is booked once (``costs.record_unique``, key ``post-read:<campaign>:<url key>``). Only reading:
nothing is posted, liked or messaged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from bot.acquisition.apify_client import ApifyError, LaunchUnknown, TemporaryApifyError
from bot.acquisition.apify_facebook_comments import POST_TEXT_MAX_USD
from bot.utils import costs

from .leads import (
    MAX_COMMENTS,
    Comment,
    CommentRead,
    Lead,
    PostContext,
    Verdict,
    is_facebook_post,
    rule_verdict,
)

if TYPE_CHECKING:
    import asyncpg

    from bot.acquisition.apify_facebook_comments import ApifyCommentsSource, PostComments

    from .leads import LeadJudge

log = logging.getLogger(__name__)

ACTOR = "campaign:post_reads"
MAX_ATTEMPTS = 3
_STOPPED = "('completed', 'cancelled', 'failed')"


def url_key(post_url: str) -> str:
    """A short stable key of a post link (the ledger key of its run)."""
    return hashlib.sha256(post_url.encode()).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class PostRead:
    campaign_id: str
    post_url: str
    attempts: int
    run_id: str | None  # the paid run, once launched: resumed, never launched again
    context: PostContext


class PostReadStore(Protocol):
    async def recover(self) -> int: ...
    async def candidates(self) -> list[tuple[str, str]]: ...
    async def enqueue(self, campaign_id: str, post_url: str) -> bool: ...
    async def claim(self) -> PostRead | None: ...
    async def save_run(self, read: PostRead, run_id: str) -> None: ...
    async def requeue(self, read: PostRead) -> None: ...
    async def finish(self, read: PostRead, state: str, *, comments: int = 0, leads: int = 0, cost_usd: float = 0.0,
                     error: str | None = None) -> None: ...


class LeadSink(Protocol):
    async def save_leads(self, leads: Sequence[Lead]) -> int: ...


class PostLeadWorker:
    def __init__(self, store: PostReadStore, source: ApifyCommentsSource, leads: LeadSink,
                 judge: LeadJudge | None = None) -> None:
        self.store, self.source, self.leads, self.judge = store, source, leads, judge

    async def serve(self, poll_seconds: float, stop: asyncio.Event) -> None:
        with suppress(Exception):
            recovered = await self.store.recover()
            if recovered:
                log.warning("campaign.post_reads.recovered", extra={"reads": recovered})
        while not stop.is_set():
            try:
                await self.step()
            except Exception:  # a database or network outage delays leads, it never ends the loop
                log.exception("campaign.post_reads.step_failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)

    async def step(self) -> bool:
        """Queue the new post links, then read one queued post; False when nothing was read."""
        for campaign_id, post_url in await self.store.candidates():
            if is_facebook_post(post_url):
                await self.store.enqueue(campaign_id, post_url)
        read = await self.store.claim()
        if read is None:
            return False
        with costs.scope(read.campaign_id):  # the run and the judge are booked on the campaign
            try:
                await self._read(read)
            except Exception as exc:  # noqa: BLE001 - the run id is stored: a retry resumes it, never pays twice
                await self._failed(read, exc)
        return True

    async def _failed(self, read: PostRead, exc: Exception) -> None:
        code = exc.code if isinstance(exc, ApifyError) else f"error:{type(exc).__name__}"
        log.warning("campaign.post_reads.read_failed %s", code, extra={"campaign_id": read.campaign_id})
        await costs.error("api", code[:120], item=self.source.config.actor_id)
        retry = isinstance(exc, TemporaryApifyError) or not isinstance(exc, ApifyError)
        if retry and read.attempts < MAX_ATTEMPTS:
            await self.store.requeue(read)
        else:
            await self.store.finish(read, "failed", error=code[:200])

    async def _read(self, read: PostRead) -> None:
        cfg = self.source.config
        run_id = read.run_id
        if run_id is None:
            if await costs.over_budget(read.campaign_id):
                await costs.skip("api", "budget_cap", item=cfg.actor_id)
                await self.store.finish(read, "failed", error="budget_cap")
                return
            try:
                run_id = await self.source.launch(read.post_url)
            except LaunchUnknown:
                # A run may exist remotely: booked at its cap, and never launched a second time for this post.
                await costs.record_unique("api", key=f"post-read:{read.campaign_id}:{url_key(read.post_url)}",
                                          provider="apify", item=cfg.actor_id, cost_usd=cfg.max_usd_per_post,
                                          code="estimated")
                await self.store.finish(read, "failed", cost_usd=cfg.max_usd_per_post, error="apify_launch_unknown")
                return
            await self.store.save_run(read, run_id)
        run = await self.source.wait(run_id)
        cost = await self._bill(read, run)
        status = str(run.get("status"))
        if status != "SUCCEEDED":
            await self.store.finish(read, "failed", cost_usd=cost,
                                    error="apify_run_" + status.lower().replace("-", "_"))
            return
        result = await self.source.comments(str(run.get("defaultDatasetId") or ""))
        if result.error:
            await self.store.finish(read, "failed", cost_usd=cost, error=result.error)
            return
        context = read.context
        if result.comments and not result.post_text and cfg.posts_actor_id and read.attempts == 1:
            # Best effort and once: a resumed read (attempts > 1) never launches this second paid run again.
            text, extra = await self._post_text(read)
            result, cost = replace(result, post_text=text), cost + extra
        if result.post_text:
            context = replace(context, post_text=result.post_text)
        await self._finish(read, context, result, cost)

    async def _finish(self, read: PostRead, context: PostContext, result: PostComments, cost: float) -> None:
        comments = result.comments
        verdicts = await self._judge(context, comments)
        sent = CommentRead(read.campaign_id, None, read.post_url, read.attempts, context)
        leads = [Lead(sent, comment, verdict, by) for comment, (verdict, by) in zip(comments, verdicts, strict=True)
                 if verdict.lead]
        saved = await self.leads.save_leads(leads) if leads else 0
        if len(comments) > len(leads):  # agents, sellers, noise: not leads, but counted
            await costs.record("api", kind="skip", provider="apify", item=self.source.config.actor_id,
                               code="not_a_lead", units=len(comments) - len(leads))
        await self.store.finish(read, "done", comments=len(comments), leads=saved, cost_usd=cost)
        log.info("campaign.post_reads.read", extra={"campaign_id": read.campaign_id, "comments": len(comments),
                                                    "leads": saved})

    async def _bill(self, read: PostRead, run: dict[str, Any], *, key: str = "post-read",
                    estimate: float | None = None) -> float:
        """The run's real cost (``usageTotalUsd``) booked once; unknown: its charge cap, marked ``estimate``."""
        usage = run.get("usageTotalUsd")
        known = isinstance(usage, int | float) and not isinstance(usage, bool) and math.isfinite(usage) and usage >= 0
        cost = float(usage) if known else (estimate if estimate is not None else self.source.config.max_usd_per_post)
        await costs.record_unique("api", key=f"{key}:{read.campaign_id}:{url_key(read.post_url)}", provider="apify",
                                  item=self.source.config.actor_id, cost_usd=cost, code="" if known else "estimated",
                                  campaign_id=read.campaign_id)
        return cost

    async def _post_text(self, read: PostRead) -> tuple[str | None, float]:
        """The post's own text from the posts actor (one item); a failure costs nothing but the text."""
        try:
            run_id = await self.source.launch(read.post_url, posts=True)
            run = await self.source.wait(run_id)
            cost = await self._bill(read, run, key="post-text", estimate=POST_TEXT_MAX_USD)
            if run.get("status") != "SUCCEEDED":
                return None, cost
            return await self.source.post_text(str(run.get("defaultDatasetId") or "")), cost
        except ApifyError as exc:
            log.warning("campaign.post_reads.post_text_failed %s", exc.code, extra={"campaign_id": read.campaign_id})
            return None, 0.0

    async def _judge(self, context: PostContext, comments: list[Comment]) -> list[tuple[Verdict, str]]:
        """A verdict and the judge's name per comment: the model, a chunk at a time; the rules where it fails."""
        verdicts: list[tuple[Verdict, str]] = []
        for start in range(0, len(comments), MAX_COMMENTS):
            chunk = comments[start:start + MAX_COMMENTS]
            if self.judge is not None:
                try:
                    verdicts += [(v, self.judge.model[:120]) for v in await self.judge.judge(context, chunk)]
                    continue
                except Exception as exc:  # noqa: BLE001 - fail open to the rules
                    log.warning("campaign.post_reads.judge_failed %s", getattr(exc, "code", type(exc).__name__))
            verdicts += [(rule_verdict(c, context), "rules") for c in chunk]
        return verdicts


# --- PostgreSQL (migration 048) -------------------------------------------------------------------


class PostgresPostReadStore:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self.pool = pool

    async def recover(self) -> int:
        """Startup: reads a crashed worker left ``reading`` go back to the queue (their run id is kept)."""
        result = await self.pool.execute(
            "update campaign_post_reads set state = 'queued', started_at = null where state = 'reading'")
        return int(result.split()[-1])

    async def candidates(self) -> list[tuple[str, str]]:
        """Links in the specs of live campaigns that are not queued yet."""
        rows = await self.pool.fetch(
            f"""select c.id::text as campaign_id, u.url
                  from campaigns c, jsonb_array_elements_text(
                           case when jsonb_typeof(c.spec #> '{{sources,required}}') = 'array'
                                then c.spec #> '{{sources,required}}' else '[]'::jsonb end) as u(url)
                 where c.state not in {_STOPPED} and u.url like 'https://%'
                   and not exists (select 1 from campaign_post_reads r
                                    where r.campaign_id = c.id and r.post_url = u.url)""")
        return [(r["campaign_id"], r["url"]) for r in rows]

    async def enqueue(self, campaign_id: str, post_url: str) -> bool:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", ACTOR)
            return bool(await conn.fetchval(
                """insert into campaign_post_reads (campaign_id, post_url) values ($1::uuid, $2)
                   on conflict do nothing returning 1""", campaign_id, post_url[:2000]))

    async def claim(self) -> PostRead | None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", ACTOR)
            row = await conn.fetchrow(
                f"""update campaign_post_reads r set state = 'reading', attempts = r.attempts + 1, started_at = now()
                     where (r.campaign_id, r.post_url) = (
                           select q.campaign_id, q.post_url from campaign_post_reads q
                             join campaigns c on c.id = q.campaign_id
                            where q.state = 'queued' and c.state not in {_STOPPED}
                            order by q.created_at limit 1 for update of q skip locked)
                 returning r.campaign_id::text as campaign_id, r.post_url, r.attempts, r.run_id,
                           (select c.plan::text from campaigns c where c.id = r.campaign_id) as plan""")
        if row is None:
            return None
        try:
            plan = json.loads(row["plan"] or "{}")
        except ValueError:
            plan = {}
        plan = plan if isinstance(plan, dict) else {}
        context = PostContext(str(plan.get("vertical") or "investors"), str(plan.get("goal") or ""))
        return PostRead(row["campaign_id"], row["post_url"], row["attempts"], row["run_id"], context)

    async def save_run(self, read: PostRead, run_id: str) -> None:
        await self.pool.execute(
            """update campaign_post_reads set run_id = $3, provider = 'apify'
                where campaign_id = $1::uuid and post_url = $2 and state = 'reading'""",
            read.campaign_id, read.post_url, run_id[:100])

    async def requeue(self, read: PostRead) -> None:
        await self.pool.execute(
            """update campaign_post_reads set state = 'queued'
                where campaign_id = $1::uuid and post_url = $2 and state = 'reading'""",
            read.campaign_id, read.post_url)

    async def finish(self, read: PostRead, state: str, *, comments: int = 0, leads: int = 0, cost_usd: float = 0.0,
                     error: str | None = None) -> None:
        await self.pool.execute(
            """update campaign_post_reads
                  set state = $3, comments_found = $4, leads_found = $5, cost_usd = $6, error_code = $7, read_at = now()
                where campaign_id = $1::uuid and post_url = $2 and state = 'reading'""",
            read.campaign_id, read.post_url, state, comments, leads, cost_usd, error)


class MemoryPostReadStore:
    """In-memory :class:`PostReadStore` for tests."""

    def __init__(self, candidates: Sequence[tuple[str, str]] = (), *, goal: str = "лиды из комментариев под постом"
                 ) -> None:
        self.links, self.goal = list(candidates), goal
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    async def recover(self) -> int:
        stuck = [r for r in self.rows.values() if r["state"] == "reading"]
        for row in stuck:
            row["state"] = "queued"
        return len(stuck)

    async def candidates(self) -> list[tuple[str, str]]:
        return [link for link in self.links if link not in self.rows]

    async def enqueue(self, campaign_id: str, post_url: str) -> bool:
        if (campaign_id, post_url) in self.rows:
            return False
        self.rows[(campaign_id, post_url)] = {"state": "queued", "attempts": 0, "run_id": None, "comments": None,
                                              "leads": None, "cost": None, "error": None}
        return True

    async def claim(self) -> PostRead | None:
        for (cid, url), row in self.rows.items():
            if row["state"] == "queued":
                row["state"], row["attempts"] = "reading", row["attempts"] + 1
                return PostRead(cid, url, row["attempts"], row["run_id"], PostContext("investors", self.goal))
        return None

    async def save_run(self, read: PostRead, run_id: str) -> None:
        self.rows[(read.campaign_id, read.post_url)]["run_id"] = run_id

    async def requeue(self, read: PostRead) -> None:
        self.rows[(read.campaign_id, read.post_url)]["state"] = "queued"

    async def finish(self, read: PostRead, state: str, *, comments: int = 0, leads: int = 0, cost_usd: float = 0.0,
                     error: str | None = None) -> None:
        self.rows[(read.campaign_id, read.post_url)].update(state=state, comments=comments, leads=leads,
                                                            cost=cost_usd, error=error)
