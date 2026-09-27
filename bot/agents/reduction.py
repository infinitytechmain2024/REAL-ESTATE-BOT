"""SA-2 Reduction agents: Claude extracts -> Jev decides at once -> the gate -> stored.

Phase 3 runs in **shadow** mode: every decision and its whole trace is stored
in ``agent_reductions`` and nothing is sent (the current analysis path keeps
sending). Several replicas (``docker compose up --scale reduction-worker=N``)
and several coroutines per replica work at once: a post is claimed by an
``insert ... on conflict do nothing``, so no two agents ever take one post, and
a crashed agent's claim is taken over when its lease ends.

Per post: the deterministic prefilter (``analysis_pipeline.filters``) drops junk
without any model call; then one Claude call (extraction) and, in the same
coroutine, one Jev call (decision); then the gate. A daily cap on model calls
bounds the cost.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from bot.analysis_pipeline.filters import RELEVANCE, filter_evidence
from bot.analysis_pipeline.models import Evidence
from bot.campaign.models import TERMINAL_STATES, Campaign
from bot.campaign.relevance import task_data
from bot.campaign.runner import campaign_request
from bot.campaign.runs import _IN_CAMPAIGN
from bot.campaign.store import CampaignStore

from .extraction import PROMPT_VERSION, ClaudeExtractor, RawPost
from .gate import DEFAULT_POLICY, Decision, Policy, gate
from .jev import Answer, JevDecider
from .llm import LLMError

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


@dataclass(frozen=True, slots=True)
class Outcome:
    action: str
    bucket: str | None
    reason: str
    score: float | None = None
    rules_bucket: str | None = None
    extraction: dict[str, Any] | None = None
    jev: dict[str, Answer] | None = None
    model_calls: int = 0


class ReductionStore(Protocol):
    async def open_campaigns(self) -> list[str]: ...
    async def claim(self, campaign_id: str, worker: str, limit: int, lease_seconds: int) -> list[RawPost]: ...
    async def done(self, post: RawPost, outcome: Outcome, *, policy: Policy, models: dict[str, str], mode: str) -> None: ...
    async def fail(self, post: RawPost, code: str, *, model_calls: int) -> None: ...
    async def calls_today(self) -> int: ...


@dataclass(frozen=True)
class ReductionConfig:
    batch: int = 5
    concurrency: int = 4
    lease_seconds: int = 300
    max_calls_per_day: int = 1000
    mode: str = "shadow"

    def __post_init__(self) -> None:
        if not (1 <= self.batch <= 50 and 1 <= self.concurrency <= 16 and 60 <= self.lease_seconds <= 3600
                and 0 <= self.max_calls_per_day <= 100_000 and self.mode == "shadow"):
            raise ValueError("unsafe reduction settings (only shadow mode exists in this phase)")


class ReductionAgent:
    """One post: prefilter -> Claude extraction -> Jev decision (same coroutine) -> gate."""

    def __init__(self, extractor: ClaudeExtractor, decider: JevDecider, *, policy: Policy = DEFAULT_POLICY) -> None:
        self.extractor, self.decider, self.policy = extractor, decider, policy

    async def reduce(self, post: RawPost, campaign: Campaign) -> Outcome:
        vertical = campaign.plan.vertical
        if vertical in RELEVANCE:
            verdict = filter_evidence(Evidence(post_id=post.post_id, source_id=post.post_id, canonical_url=post.url,
                                               text=post.text, title=post.title, published_at=post.published_at),
                                      vertical)
            if not verdict.accepted:
                return Outcome("discard", None, f"prefilter:{verdict.reason}")
        task = task_data(campaign)
        extraction = await self.extractor.extract(task, post)
        # Claude extracted -> Jev decides right away, on the extraction (never the raw post).
        answers = await self.decider.decide(task, extraction)
        decision: Decision = gate(extraction, answers, campaign_request(campaign), vertical=vertical,
                                  policy=self.policy)
        return Outcome(decision.action, decision.bucket, decision.reason, decision.score, decision.rules_bucket,
                       extraction, answers, model_calls=2)


class ReductionWorker:
    def __init__(self, agent: ReductionAgent, store: ReductionStore, campaigns: CampaignStore, *,
                 config: ReductionConfig | None = None, models: dict[str, str] | None = None,
                 worker_id: str | None = None) -> None:
        self.agent, self.store, self.campaigns = agent, store, campaigns
        self.config = config or ReductionConfig()
        self.models = models or {}
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._sem = asyncio.Semaphore(self.config.concurrency)

    async def tick(self) -> int:
        """Claim and reduce one batch per open campaign; returns how many posts were handled."""
        handled = 0
        for campaign_id in await self.store.open_campaigns():
            campaign = await self.campaigns.get(campaign_id)
            if campaign is None or campaign.state in TERMINAL_STATES:
                continue
            if await self.store.calls_today() >= self.config.max_calls_per_day:
                log.warning("reduction.daily_cap", extra={"cap": self.config.max_calls_per_day})
                return handled
            posts = await self.store.claim(campaign_id, self.worker_id, self.config.batch, self.config.lease_seconds)
            await asyncio.gather(*(self._one(post, campaign) for post in posts))
            handled += len(posts)
        return handled

    async def _one(self, post: RawPost, campaign: Campaign) -> None:
        async with self._sem:
            try:
                outcome = await self.agent.reduce(post, campaign)
            except LLMError as exc:
                log.warning("reduction.model_failed %s", exc.code, extra={"post_id": post.post_id})
                await self.store.fail(post, exc.code, model_calls=1)
                return
            except (ValueError, KeyError, TypeError) as exc:  # an answer outside the schema
                log.warning("reduction.unreadable %s", type(exc).__name__, extra={"post_id": post.post_id})
                await self.store.fail(post, f"unreadable:{type(exc).__name__}", model_calls=1)
                return
            await self.store.done(post, outcome, policy=self.agent.policy, models=self.models, mode=self.config.mode)
            log.info("reduction.decided %s %s", outcome.action, outcome.reason,
                     extra={"post_id": post.post_id, "campaign_id": post.campaign_id, "bucket": outcome.bucket})

    async def serve(self, poll_seconds: float, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        while not stop.is_set():
            handled = 0
            try:
                handled = await self.tick()
            except Exception:  # a database outage delays the work, it never ends the loop
                log.exception("reduction.tick_failed")
            if handled:
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)


# -- stores --


def _answers_json(answers: dict[str, Answer] | None) -> str | None:
    if answers is None:
        return None
    from .jev import QUESTIONS

    return json.dumps([{"id": q, "question": QUESTIONS[q], "p": a.p, "confidence": a.confidence}
                       for q, a in answers.items()], ensure_ascii=False)


class PostgresReductionStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def open_campaigns(self) -> list[str]:
        rows = await self.pool.fetch(
            "select id::text from campaigns where state <> all($1::text[]) order by created_at", list(TERMINAL_STATES))
        return [r[0] for r in rows]

    async def claim(self, campaign_id: str, worker: str, limit: int, lease_seconds: int) -> list[RawPost]:
        async with self.pool.acquire() as conn, conn.transaction():
            # A lapsed claim (crashed agent) is taken over first, then new posts of the campaign.
            ids = [r[0] for r in await conn.fetch(
                """update agent_reductions set claimed_by = $2, claimed_until = now() + make_interval(secs => $4),
                          updated_at = now()
                    where (post_id, campaign_id) in (
                          select post_id, campaign_id from agent_reductions
                           where campaign_id = $1::uuid and state = 'claimed' and claimed_until < now()
                             and attempts < $5 limit $3 for update skip locked)
                returning post_id""", campaign_id, worker, limit, lease_seconds, MAX_ATTEMPTS)]
            if len(ids) < limit:
                ids += [r[0] for r in await conn.fetch(
                    f"""insert into agent_reductions (post_id, campaign_id, state, claimed_by, claimed_until)
                        select p.id, $1::uuid, 'claimed', $2, now() + make_interval(secs => $4)
                          from collected_posts p
                         where {_IN_CAMPAIGN} and coalesce(p.body_text, '') <> ''
                           and not exists (select 1 from agent_reductions r
                                            where r.post_id = p.id and r.campaign_id = $1::uuid)
                         order by p.collected_at, p.id limit $3
                        on conflict do nothing returning post_id""",
                    campaign_id, worker, limit - len(ids), lease_seconds)]
            if not ids:
                return []
            rows = await conn.fetch(
                """select p.id::text, p.canonical_url, coalesce(p.body_text, '') as body,
                          coalesce(nullif(p.raw_payload->>'title', ''), s.configuration->'facebook_last_read'->>'title', '') as title,
                          s.platform, p.published_at
                     from collected_posts p join monitoring_sources s on s.id = p.source_id
                    where p.id = any($1::uuid[])""", ids)
        return [RawPost(r["id"], campaign_id, r["canonical_url"], r["body"], r["title"], r["platform"], r["published_at"])
                for r in rows]

    async def done(self, post: RawPost, outcome: Outcome, *, policy: Policy, models: dict[str, str], mode: str) -> None:
        await self.pool.execute(
            """update agent_reductions
                  set state = 'done', mode = $3, extraction = $4::jsonb, jev = $5::jsonb, action = $6, bucket = $7,
                      reason = $8, score = $9, rules_bucket = $10, policy = $11::jsonb, models = $12::jsonb,
                      prompt_version = $13, model_calls = model_calls + $14, claimed_until = null, error = null,
                      updated_at = now()
                where post_id = $1::uuid and campaign_id = $2::uuid""",
            post.post_id, post.campaign_id, mode,
            json.dumps(outcome.extraction, ensure_ascii=False) if outcome.extraction is not None else None,
            _answers_json(outcome.jev), outcome.action, outcome.bucket, outcome.reason[:120], outcome.score,
            outcome.rules_bucket, json.dumps(policy.as_dict()), json.dumps(models), PROMPT_VERSION, outcome.model_calls)

    async def fail(self, post: RawPost, code: str, *, model_calls: int) -> None:
        # Retried after a minute, at most MAX_ATTEMPTS times; then 'failed' for good.
        await self.pool.execute(
            """update agent_reductions
                  set attempts = attempts + 1, model_calls = model_calls + $4, error = $3,
                      state = case when attempts + 1 >= $5 then 'failed' else 'claimed' end,
                      claimed_until = now() + interval '60 seconds', updated_at = now()
                where post_id = $1::uuid and campaign_id = $2::uuid""",
            post.post_id, post.campaign_id, code[:120], model_calls, MAX_ATTEMPTS)

    async def calls_today(self) -> int:
        return int(await self.pool.fetchval(
            "select coalesce(sum(model_calls), 0) from agent_reductions where updated_at > now() - interval '24 hours'"))


class MemoryReductionStore:
    """In-process twin for tests: ``posts`` per campaign; ``rows`` keyed by (post, campaign)."""

    def __init__(self, campaigns: Any, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.campaigns, self.now = campaigns, now
        self.posts: dict[str, list[RawPost]] = {}
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def add_post(self, post: RawPost) -> None:
        self.posts.setdefault(post.campaign_id, []).append(post)

    async def open_campaigns(self) -> list[str]:
        return [c.id for c in self.campaigns.campaigns.values() if c.state not in TERMINAL_STATES]

    async def claim(self, campaign_id: str, worker: str, limit: int, lease_seconds: int) -> list[RawPost]:
        taken: list[RawPost] = []
        for post in self.posts.get(campaign_id, []):
            if len(taken) >= limit:
                break
            row = self.rows.get((post.post_id, campaign_id))
            now = self.now().timestamp()
            if row is None:
                self.rows[(post.post_id, campaign_id)] = {"state": "claimed", "worker": worker, "until": now + lease_seconds,
                                                         "attempts": 0, "model_calls": 0}
                taken.append(post)
            elif row["state"] == "claimed" and row["until"] < now and row["attempts"] < MAX_ATTEMPTS:
                row.update(worker=worker, until=now + lease_seconds)
                taken.append(post)
        return taken

    async def done(self, post: RawPost, outcome: Outcome, *, policy: Policy, models: dict[str, str], mode: str) -> None:
        row = self.rows[(post.post_id, post.campaign_id)]
        row.update(state="done", mode=mode, outcome=outcome, policy=policy, models=models,
                   model_calls=row["model_calls"] + outcome.model_calls)

    async def fail(self, post: RawPost, code: str, *, model_calls: int) -> None:
        row = self.rows[(post.post_id, post.campaign_id)]
        row["attempts"] += 1
        row["model_calls"] += model_calls
        row.update(error=code, state="failed" if row["attempts"] >= MAX_ATTEMPTS else "claimed",
                   until=self.now().timestamp() + 60)

    async def calls_today(self) -> int:
        return sum(r["model_calls"] for r in self.rows.values())


# -- service --


async def main() -> None:
    import asyncpg

    from bot.campaign.store import PostgresCampaignStore

    from .llm import OpenRouterJSON
    from .settings import ReductionSettings

    settings = ReductionSettings()
    missing = settings.missing()
    if missing:
        # Idle rather than exit: the Compose restart policy must not loop a switched-off service.
        log.warning("reduction.disabled", extra={"missing": missing})
        await asyncio.Event().wait()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=settings.concurrency + 2)
    llm = OpenRouterJSON(settings.openrouter_api_key, timeout_seconds=settings.timeout_seconds)
    models = {"claude": settings.claude_model, "jev": settings.jev_model}
    agent = ReductionAgent(ClaudeExtractor(llm, settings.claude_model), JevDecider(llm, settings.jev_model))
    worker = ReductionWorker(agent, PostgresReductionStore(pool), PostgresCampaignStore(pool),
                             config=settings.config(), models=models)
    log.info("reduction.ready", extra={"models": models, "mode": "shadow", "concurrency": settings.concurrency})
    try:
        await worker.serve(settings.poll_seconds)
    finally:
        await llm.aclose()
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
