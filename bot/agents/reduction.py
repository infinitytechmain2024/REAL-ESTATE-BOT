"""SA-2 Reduction agents: Claude extracts -> Jev decides at once -> the gate -> stored.

**Shadow** mode (default): every decision and its whole trace is stored in
``agent_reductions`` and nothing is sent (the analysis path keeps sending); every
platform is reduced. **Live** mode (``AGENT_REDUCTION_MODE=live``, PLAN 4.3) owns the posts of
``AGENT_REDUCTION_SOURCES`` (default ``website``): the post is claimed through the same
lease on ``collected_posts`` the analysis worker uses (``analysis_claim_token``; set
``ANALYSIS_EXCLUDE_PLATFORMS`` to the same list so the analysis worker leaves them alone),
and the decision is turned into what the analysis worker would have written:

* ``send`` -> a ``findings`` row (state ``ready``, same payload shape as
  ``analysis_pipeline/store.py``) that the campaign runner streams like any other
  finding (its own rules, AI check, dedup and card); the outbox row (``to_send``) is
  written through the Recorder;
* ``hold`` -> the same row, filed as a held similar/other finding in ``campaign_findings``
  (the runner offers it after «Одобрить»), recorded as ``held``;
* ``discard`` by the deterministic rules (bucket ``excluded``) -> filed as excluded so the
  final report counts it, recorded as ``excluded``; any other discard stores nothing but
  the trace;
* the post becomes ``analysed`` (a finding was written) or ``rejected``.

A post that fails for good releases its claim (a worker allowed to take its platform may then
pick it up). Several replicas (``docker compose up --scale reduction-worker=N``)
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
import hashlib
import json
import logging
import os
import re
import socket
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from bot.analysis_pipeline.cards import CardTask, render_card
from bot.analysis_pipeline.filters import RELEVANCE, filter_evidence
from bot.analysis_pipeline.formatters import PAYLOAD_VERSION
from bot.analysis_pipeline.models import Evidence
from bot.campaign.models import TERMINAL_STATES, Campaign
from bot.campaign.relevance import reason_category, task_data
from bot.campaign.runner import campaign_request
from bot.campaign.runs import _IN_CAMPAIGN
from bot.campaign.store import CampaignStore

from .extraction import PROMPT_VERSION, ClaudeExtractor, RawPost
from .gate import DEFAULT_POLICY, Decision, Policy, gate
from .jev import Answer, JevDecider
from .llm import LLMError
from .recorder import Recorder, record_of

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
ACTOR = "reduction_worker"
MODES = ("shadow", "live")
_PLATFORM = re.compile(r"^[a-z0-9_]{2,40}$")
# The fact fields of an extraction a finding's payload keeps (what ``analysis_pipeline.formatters.finding_payload`` stores).
PAYLOAD_FIELDS = ("summary", "summary_ru", "source_language", "location", "price_signals", "price_amount",
                  "price_currency", "deal_type", "property_type", "rooms", "who", "listing_kind", "country", "area_m2",
                  "evidence", "district", "address", "floor", "features", "condition", "listing_date", "related_links")


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


@dataclass(frozen=True, slots=True)
class Publication:
    """What a live decision writes next to the trace: the finding the campaign runner streams (or holds)."""

    kind: Literal["send", "hold", "excluded"]
    vertical: str
    payload: dict[str, Any]
    formatted: str
    confidence: float
    language: str
    bucket: str
    why: str | None  # the reason category of a held / excluded one (``campaign_findings.why``)
    model: str = ""


class ReductionStore(Protocol):
    async def open_campaigns(self) -> list[str]: ...
    async def claim(self, campaign_id: str, worker: str, limit: int, lease_seconds: int, *, mode: str = "shadow",
                    sources: tuple[str, ...] = ()) -> list[RawPost]: ...
    async def done(self, post: RawPost, outcome: Outcome, *, policy: Policy, models: dict[str, str], mode: str,
                   publication: Publication | None = None) -> str | None: ...
    async def fail(self, post: RawPost, code: str, *, model_calls: int) -> None: ...
    async def calls_today(self) -> int: ...


@dataclass(frozen=True)
class ReductionConfig:
    batch: int = 5
    concurrency: int = 4
    lease_seconds: int = 300
    max_calls_per_day: int = 1000
    mode: str = "shadow"
    # Live mode only: the platforms (monitoring_sources.platform) this worker owns; the analysis worker keeps the rest.
    sources: tuple[str, ...] = ("website",)

    def __post_init__(self) -> None:
        if not (1 <= self.batch <= 50 and 1 <= self.concurrency <= 16 and 60 <= self.lease_seconds <= 3600
                and 0 <= self.max_calls_per_day <= 100_000 and self.mode in MODES):
            raise ValueError("unsafe reduction settings (mode is shadow or live)")
        if self.mode == "live" and (not self.sources or not all(_PLATFORM.match(s) for s in self.sources)):
            raise ValueError("live reduction needs AGENT_REDUCTION_SOURCES (comma-separated platforms, e.g. website)")


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


def publication_of(post: RawPost, outcome: Outcome, campaign: Campaign, model: str = "") -> Publication | None:
    """What a live decision files as a finding: ``send`` and ``hold`` always, a ``discard`` only when the deterministic
    rules excluded it (so the final report counts it). Anything else stores the trace alone."""
    if outcome.extraction is None:
        return None
    if outcome.action == "send":
        kind, bucket = "send", "exact"
    elif outcome.action == "hold":
        kind, bucket = "hold", outcome.bucket if outcome.bucket in ("similar", "other") else "other"
    elif outcome.action == "discard" and outcome.bucket == "excluded":
        kind, bucket = "excluded", "excluded"
    else:
        return None
    extraction = outcome.extraction
    vertical = campaign.plan.vertical
    if vertical not in ("real_estate", "investors"):  # a campaign for both: what the post is
        vertical = extraction.get("category") if extraction.get("category") in ("real_estate", "investors") else "real_estate"
    payload: dict[str, Any] = {"schema_version": PAYLOAD_VERSION,
                               **{k: extraction[k] for k in PAYLOAD_FIELDS if k in extraction},
                               "original_post_link": post.url}
    raw = extraction.get("confidence", extraction.get("extraction_confidence"))
    confidence = min(1.0, max(0.0, float(raw))) if isinstance(raw, int | float) and not isinstance(raw, bool) else 0.5
    language = extraction.get("source_language") or "unknown"
    formatted = render_card(payload, original=post.text, task=CardTask(vertical=vertical), vertical=vertical,
                            language=language, confidence=confidence)
    if kind == "send":
        why = None
    elif kind == "excluded":
        why = reason_category(outcome.reason.removeprefix("rules:"))
    else:  # held: the model was unsure -> unverified; otherwise the model's judgement
        why = "unverified" if outcome.reason == "jev:low_confidence" else "ai"
    return Publication(kind, vertical, payload, formatted, confidence, language, bucket, why, model)


class ReductionWorker:
    def __init__(self, agent: ReductionAgent, store: ReductionStore, campaigns: CampaignStore, *,
                 config: ReductionConfig | None = None, models: dict[str, str] | None = None,
                 worker_id: str | None = None, recorder: Recorder | None = None) -> None:
        """``recorder`` (live mode): the outbox rows of what the worker files (``to_send`` / ``held`` / ``excluded``)."""
        self.agent, self.store, self.campaigns = agent, store, campaigns
        self.recorder = recorder
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
            posts = await self.store.claim(campaign_id, self.worker_id, self.config.batch, self.config.lease_seconds,
                                           mode=self.config.mode, sources=self.config.sources)
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
            publication = publication_of(post, outcome, campaign, self.models.get("claude", "")) \
                if self.config.mode == "live" else None
            finding_id = await self.store.done(post, outcome, policy=self.agent.policy, models=self.models,
                                               mode=self.config.mode, publication=publication)
            log.info("reduction.decided %s %s", outcome.action, outcome.reason,
                     extra={"post_id": post.post_id, "campaign_id": post.campaign_id, "bucket": outcome.bucket,
                            "finding_id": finding_id})
            if finding_id is not None and publication is not None:
                await self._record(post, finding_id, publication, outcome)

    async def _record(self, post: RawPost, finding_id: str, publication: Publication, outcome: Outcome) -> None:
        """The outbox row of a live decision (bookkeeping after the fact: a failure is logged, the finding is stored)."""
        if self.recorder is None:
            return
        state = {"send": "to_send", "hold": "held", "excluded": "excluded"}[publication.kind]
        record = record_of(post.campaign_id, finding_id, state=state, bucket=publication.bucket,  # type: ignore[arg-type]
                           payload=publication.payload, text=post.text,
                           card_text=publication.formatted[:4000] if publication.kind == "send" else None,
                           reason=outcome.reason if publication.kind == "excluded" else None)
        try:
            write = {"to_send": self.recorder.to_send, "held": self.recorder.held,
                     "excluded": self.recorder.excluded}[state]
            await write(record)
        except Exception:  # noqa: BLE001
            log.warning("reduction.record_failed", extra={"post_id": post.post_id, "finding_id": finding_id})

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


async def _publish(conn: Any, post: RawPost, publication: Publication | None) -> str | None:
    """The ``findings`` row the campaign runner streams (same shape and dedupe key as ``analysis_pipeline/store.py``)
    and, for a held or excluded one, its ``campaign_findings`` filing. Returns the finding id, None without one."""
    if publication is None:
        return None
    key = hashlib.sha256(f"{publication.vertical}:{post.post_id}".encode()).hexdigest()
    finding_id = await conn.fetchval(
        """insert into findings (vertical, source_id, post_id, finding_type, dedupe_key, structured_payload, confidence,
                                 state, analysis_metadata)
           select $1, p.source_id, p.id, $2, $3, $4::jsonb, $5, 'ready', $6::jsonb
             from collected_posts p where p.id = $7::uuid
           on conflict (dedupe_key) do update set structured_payload = excluded.structured_payload,
               confidence = excluded.confidence, analysis_metadata = excluded.analysis_metadata
           returning id::text""",
        publication.vertical, "real_estate_proposition" if publication.vertical == "real_estate" else "investor_lead",
        key, json.dumps({**publication.payload, "formatted": publication.formatted}, ensure_ascii=False),
        publication.confidence,
        json.dumps({"prompt_version": PROMPT_VERSION, "model": publication.model, "language": publication.language,
                    "agent": "reduction"}),
        post.post_id)
    if finding_id and publication.kind in ("hold", "excluded"):
        await conn.execute(
            """insert into campaign_findings (finding_id, campaign_id, bucket, state, why)
               values ($1::uuid, $2::uuid, $3, 'held', $4) on conflict do nothing""",
            finding_id, post.campaign_id, publication.bucket, publication.why)
    return str(finding_id) if finding_id else None


class PostgresReductionStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def open_campaigns(self) -> list[str]:
        rows = await self.pool.fetch(
            "select id::text from campaigns where state <> all($1::text[]) order by created_at", list(TERMINAL_STATES))
        return [r[0] for r in rows]

    async def claim(self, campaign_id: str, worker: str, limit: int, lease_seconds: int, *, mode: str = "shadow",
                    sources: tuple[str, ...] = ()) -> list[RawPost]:
        """Shadow: any post of the campaign, once. Live: only ``normalised`` posts of ``sources``, claimed through the
        same lease on ``collected_posts`` the analysis worker uses, so no post is ever taken by both."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", ACTOR)
            # A lapsed claim (crashed agent) of this mode is taken over first, then new posts of the campaign.
            ids = [r[0] for r in await conn.fetch(
                """update agent_reductions set claimed_by = $2, claimed_until = now() + make_interval(secs => $4),
                          updated_at = now()
                    where (post_id, campaign_id) in (
                          select post_id, campaign_id from agent_reductions
                           where campaign_id = $1::uuid and state = 'claimed' and claimed_until < now()
                             and attempts < $5 and mode = $6 limit $3 for update skip locked)
                returning post_id""", campaign_id, worker, limit, lease_seconds, MAX_ATTEMPTS, mode)]
            if ids and mode == "live":  # keep the post's own lease alive while it is retried
                await conn.execute("update collected_posts set analysis_claimed_at = now() where id = any($1::uuid[])", ids)
            if len(ids) < limit and mode == "live":
                ids += [r[0] for r in await conn.fetch(
                    f"""with picked as (
                            select p.id from collected_posts p join monitoring_sources s on s.id = p.source_id
                             where {_IN_CAMPAIGN} and p.state = 'normalised' and coalesce(p.body_text, '') <> ''
                               and s.platform = any($5::text[])
                               and (p.analysis_claimed_at is null
                                    or p.analysis_claimed_at < now() - make_interval(secs => $4))
                               and not exists (select 1 from agent_reductions r
                                                where r.post_id = p.id and r.campaign_id = $1::uuid and r.mode = 'live')
                             order by p.collected_at, p.id limit $3 for update of p skip locked),
                        stamped as (
                            update collected_posts p set analysis_claim_token = gen_random_uuid(), analysis_claimed_at = now()
                              from picked where p.id = picked.id returning p.id)
                        insert into agent_reductions (post_id, campaign_id, state, mode, claimed_by, claimed_until)
                        select id, $1::uuid, 'claimed', 'live', $2, now() + make_interval(secs => $4) from stamped
                        on conflict (post_id, campaign_id) do update
                           set state = 'claimed', mode = 'live', claimed_by = excluded.claimed_by,
                               claimed_until = excluded.claimed_until, attempts = 0, updated_at = now()
                         where agent_reductions.mode = 'shadow'
                        returning post_id""",
                    campaign_id, worker, limit - len(ids), lease_seconds, list(sources))]
            elif len(ids) < limit:
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

    async def done(self, post: RawPost, outcome: Outcome, *, policy: Policy, models: dict[str, str], mode: str,
                   publication: Publication | None = None) -> str | None:
        """Store the decision trace; in live mode also the finding, the held / excluded filing and the post's final
        state, in the same transaction. Returns the finding id written (live), else None."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", ACTOR)
            await conn.execute(
                """update agent_reductions
                      set state = 'done', mode = $3, extraction = $4::jsonb, jev = $5::jsonb, action = $6, bucket = $7,
                          reason = $8, score = $9, rules_bucket = $10, policy = $11::jsonb, models = $12::jsonb,
                          prompt_version = $13, model_calls = model_calls + $14, claimed_until = null, error = null,
                          updated_at = now()
                    where post_id = $1::uuid and campaign_id = $2::uuid""",
                post.post_id, post.campaign_id, mode,
                json.dumps(outcome.extraction, ensure_ascii=False) if outcome.extraction is not None else None,
                _answers_json(outcome.jev), outcome.action, outcome.bucket, outcome.reason[:120], outcome.score,
                outcome.rules_bucket, json.dumps(policy.as_dict()), json.dumps(models), PROMPT_VERSION,
                outcome.model_calls)
            if mode != "live":
                return None
            finding_id = await _publish(conn, post, publication)
            await conn.execute(
                """update collected_posts set state = $2, analysis_claim_token = null, analysis_claimed_at = null
                    where id = $1::uuid and state = 'normalised'""",
                post.post_id, "analysed" if finding_id else "rejected")
            return finding_id

    async def fail(self, post: RawPost, code: str, *, model_calls: int) -> None:
        # Retried after a minute, at most MAX_ATTEMPTS times; then 'failed' for good.
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """update agent_reductions
                      set attempts = attempts + 1, model_calls = model_calls + $4, error = $3,
                          state = case when attempts + 1 >= $5 then 'failed' else 'claimed' end,
                          claimed_until = now() + interval '60 seconds', updated_at = now()
                    where post_id = $1::uuid and campaign_id = $2::uuid""",
                post.post_id, post.campaign_id, code[:120], model_calls, MAX_ATTEMPTS)
            # A live post given up on gives its lease back (a worker allowed to take its platform may pick it up).
            await conn.execute(
                """update collected_posts set analysis_claim_token = null, analysis_claimed_at = null
                    where id = $1::uuid and state = 'normalised'
                      and exists (select 1 from agent_reductions r where r.post_id = $1::uuid
                                   and r.campaign_id = $2::uuid and r.state = 'failed' and r.mode = 'live')""",
                post.post_id, post.campaign_id)

    async def calls_today(self) -> int:
        return int(await self.pool.fetchval(
            "select coalesce(sum(model_calls), 0) from agent_reductions where updated_at > now() - interval '24 hours'"))


class MemoryReductionStore:
    """In-process twin for tests: ``posts`` per campaign; ``rows`` keyed by (post, campaign)."""

    def __init__(self, campaigns: Any, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.campaigns, self.now = campaigns, now
        self.posts: dict[str, list[RawPost]] = {}
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.published: dict[str, Publication] = {}  # post id -> what live mode filed as a finding
        self.findings: dict[str, Publication] = {}  # finding id -> the same
        self.post_states: dict[str, str] = {}  # post id -> analysed / rejected once a live decision was made

    def add_post(self, post: RawPost) -> None:
        self.posts.setdefault(post.campaign_id, []).append(post)

    async def open_campaigns(self) -> list[str]:
        return [c.id for c in self.campaigns.campaigns.values() if c.state not in TERMINAL_STATES]

    async def claim(self, campaign_id: str, worker: str, limit: int, lease_seconds: int, *, mode: str = "shadow",
                    sources: tuple[str, ...] = ()) -> list[RawPost]:
        taken: list[RawPost] = []
        for post in self.posts.get(campaign_id, []):
            if len(taken) >= limit:
                break
            if mode == "live" and (post.platform not in sources or self.post_states.get(post.post_id, "normalised") != "normalised"):
                continue
            row = self.rows.get((post.post_id, campaign_id))
            now = self.now().timestamp()
            if row is None or (mode == "live" and row.get("mode") != "live" and row["state"] != "claimed"):
                self.rows[(post.post_id, campaign_id)] = {"state": "claimed", "worker": worker, "until": now + lease_seconds,
                                                         "attempts": 0, "model_calls": 0, "mode": mode}
                taken.append(post)
            elif (row["state"] == "claimed" and row["until"] < now and row["attempts"] < MAX_ATTEMPTS
                  and row.get("mode", "shadow") == mode):
                row.update(worker=worker, until=now + lease_seconds)
                taken.append(post)
        return taken

    async def done(self, post: RawPost, outcome: Outcome, *, policy: Policy, models: dict[str, str], mode: str,
                   publication: Publication | None = None) -> str | None:
        row = self.rows[(post.post_id, post.campaign_id)]
        row.update(state="done", mode=mode, outcome=outcome, policy=policy, models=models,
                   model_calls=row["model_calls"] + outcome.model_calls)
        if mode != "live":
            return None
        finding_id = f"finding-{post.post_id}" if publication is not None else None
        if publication is not None:
            self.published[post.post_id] = publication
            self.findings[finding_id] = publication  # type: ignore[index]
        self.post_states[post.post_id] = "analysed" if finding_id else "rejected"
        return finding_id

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
    try:
        unknown = await llm.unknown_models(list(models.values()))
    except LLMError as exc:
        unknown = []
        log.warning("reduction.catalogue_unreachable %s", exc.code)
    if unknown:
        # A wrong id would fail every call: stay idle with one clear line in the log instead.
        log.error("reduction.unknown_models %s: set OPENROUTER_CLAUDE_MODEL / OPENROUTER_JEV_MODEL "
                  "to ids from https://openrouter.ai/api/v1/models", unknown)
        await llm.aclose()
        await pool.close()
        await asyncio.Event().wait()
    agent = ReductionAgent(ClaudeExtractor(llm, settings.claude_model), JevDecider(llm, settings.jev_model))
    from .recorder import PostgresRecorder

    config = settings.config()
    worker = ReductionWorker(agent, PostgresReductionStore(pool), PostgresCampaignStore(pool),
                             config=config, models=models, recorder=PostgresRecorder(pool))
    log.info("reduction.ready", extra={"models": models, "mode": config.mode, "concurrency": settings.concurrency,
                                       "sources": list(config.sources) if config.mode == "live" else "all"})
    try:
        await worker.serve(settings.poll_seconds)
    finally:
        await llm.aclose()
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
