"""Durable state for the campaign runner (migration 016): windows, streamed findings, status.

A window is at most 20 of a campaign's queued groups, collected as one
ordinary Facebook batch. The batch is planned by the same code as ``/run
facebook-groups`` (``bot.orchestra.store.plan_facebook_batch``), so every
quota, breaker, profile and source check applies, and facebook-runner (the
only process that reads Facebook groups) executes it from its launch request.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from .models import WINDOW_SIZE
from .offers import MemoryOffer, MemoryOfferDesk
from .relevance import Relevance

if TYPE_CHECKING:
    import asyncpg

    from bot.orchestra.store import SafetyLimits

TERMINAL_BATCH_STATES = frozenset({"succeeded", "failed", "cancelled"})
RECENT_SECONDS = 86_400  # finished campaigns still stream late findings for a day


@dataclass(frozen=True, slots=True)
class RunState:
    status_text: str | None = None
    next_window_at: datetime | None = None
    drain_started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class QueuedGroup:
    group_key: str
    canonical_url: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class Window:
    window_no: int
    batch_id: str
    batch_state: str
    current_group: str | None = None  # name of the group being read right now


@dataclass(frozen=True, slots=True)
class WindowStart:
    window_no: int
    batch_id: str
    groups: int
    skipped: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StreamFinding:
    """A finding to stream. ``payload`` (the stored structured payload) is rendered
    as a Russian card with the post's ``original`` text; without it ``text`` is sent."""

    id: str
    text: str
    payload: dict[str, Any] | None = None
    original: str = ""
    language: str | None = None
    confidence: float | None = None
    vertical: str | None = None


@dataclass(frozen=True, slots=True)
class SocialActivity:
    """Social network search for a campaign (``bot.social_search``, migration 022).

    ``searching``: the platform a query runs on right now; ``pending``: more
    queries are to come (the runner waits for them, bounded, before it
    completes); ``notes``: technical lines for owners only.
    """

    searching: str | None = None
    query: str | None = None
    pending: bool = False
    notes: tuple[str, ...] = ()


RECOVERY_ACTOR = "campaign:runner:recovery"


class RunStore(Protocol):
    async def recover_discovery_profile(self) -> int: ...
    async def active_campaigns(self) -> list[str]: ...
    async def get_run(self, campaign_id: str) -> RunState: ...
    async def save_run(self, campaign_id: str, run: RunState) -> None: ...
    async def profile_state(self) -> str | None: ...
    async def breaker_reason(self) -> str | None: ...
    async def busy_elsewhere(self, campaign_id: str) -> bool: ...
    async def open_window(self, campaign_id: str) -> Window | None: ...
    async def close_window(self, campaign_id: str, window: Window, actor: str) -> None: ...
    async def windows_started(self, campaign_id: str) -> int: ...
    async def next_groups(self, campaign_id: str, limit: int) -> list[QueuedGroup]: ...
    async def start_window(self, campaign_id: str, groups: list[QueuedGroup], *, vertical: str,
                           actor: str) -> WindowStart | None: ...
    async def cancel_window_batch(self, batch_id: str, actor: str) -> None: ...
    async def pending_analysis(self, campaign_id: str) -> int: ...
    async def unstreamed_findings(self, campaign_id: str, limit: int) -> list[StreamFinding]: ...
    async def claim_finding(self, campaign_id: str, finding_id: str) -> int | None: ...
    async def finding_sent(self, finding_id: str, message_id: int) -> None: ...
    async def release_finding(self, finding_id: str) -> None: ...
    async def streamed_count(self, campaign_id: str) -> int: ...
    # exact / similar / other (migration 019, ``tolerance`` and ``offers``)
    async def hold_finding(self, campaign_id: str, finding_id: str, bucket: str, distance: float | None) -> bool: ...
    async def held_findings(self, campaign_id: str, bucket: str, limit: int) -> list[StreamFinding]: ...
    async def claim_held(self, campaign_id: str, finding_id: str) -> int | None: ...
    async def exact_count(self, campaign_id: str) -> int: ...
    async def offer_state(self, campaign_id: str, bucket: str) -> str | None: ...
    async def open_offer(self, campaign_id: str, bucket: str) -> bool: ...
    async def offer_sent(self, campaign_id: str, bucket: str, message_id: int) -> None: ...
    async def drop_offer(self, campaign_id: str, bucket: str) -> None: ...
    async def social_activity(self, campaign_id: str) -> SocialActivity: ...
    # the AI relevance verdict, once per finding (migration 023, ``relevance``)
    async def relevance(self, campaign_id: str, finding_id: str) -> Relevance | None: ...
    async def save_relevance(self, campaign_id: str, finding_id: str, relevance: Relevance) -> None: ...
    async def relevance_calls(self, campaign_id: str) -> int: ...


def _distance(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _payload(raw: str | None) -> dict[str, Any] | None:
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _window_size(limit: int) -> int:
    return max(1, min(limit, WINDOW_SIZE))


class PostgresRunStore:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record], limits: SafetyLimits) -> None:
        self.pool, self.limits = pool, limits

    async def recover_discovery_profile(self) -> int:
        """Free a Facebook profile a crashed discovery left ``in_use``; returns how many were freed.

        Only while a campaign is still ``discovering`` (so a discovery held it)
        and nothing else can hold it: no running batch run or acquisition run
        on the profile and no facebook-runner launch in progress.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn, RECOVERY_ACTOR)
            rows = await conn.fetch(
                """update browser_profiles p set state = 'ready'
                    where p.platform = 'facebook' and p.state = 'in_use' and p.deleted_at is null
                      and exists (select 1 from campaigns where state = 'discovering')
                      and not exists (select 1 from batch_runs b where b.browser_profile_id = p.id and b.state = 'running')
                      and not exists (select 1 from acquisition_runs a where a.browser_profile_id = p.id and a.state = 'running')
                      and not exists (select 1 from collector_launch_requests l where l.state = 'running')
                returning p.id""",
            )
        return len(rows)

    async def active_campaigns(self) -> list[str]:
        rows = await self.pool.fetch(
            """select id::text from campaigns
                where state not in ('completed', 'cancelled', 'failed')
                   or finished_at > now() - make_interval(secs => $1)
                order by created_at""",
            RECENT_SECONDS,
        )
        return [r["id"] for r in rows]

    async def get_run(self, campaign_id: str) -> RunState:
        row = await self.pool.fetchrow(
            "select status_text, next_window_at, drain_started_at from campaign_runs where campaign_id = $1::uuid",
            campaign_id,
        )
        return RunState(row["status_text"], row["next_window_at"], row["drain_started_at"]) if row else RunState()

    async def save_run(self, campaign_id: str, run: RunState) -> None:
        await self.pool.execute(
            """insert into campaign_runs (campaign_id, status_text, next_window_at, drain_started_at)
               values ($1::uuid, $2, $3, $4)
               on conflict (campaign_id) do update set status_text = excluded.status_text,
                   next_window_at = excluded.next_window_at, drain_started_at = excluded.drain_started_at,
                   updated_at = now()""",
            campaign_id, run.status_text and run.status_text[:1000], run.next_window_at, run.drain_started_at,
        )

    async def profile_state(self) -> str | None:
        """The most usable Facebook profile's state: ready, else in_use, else whatever it is."""
        state: str | None = await self.pool.fetchval(
            """select state from browser_profiles where platform = 'facebook' and deleted_at is null
                order by state = 'ready' desc, state = 'in_use' desc, created_at limit 1""",
        )
        return state

    async def breaker_reason(self) -> str | None:
        from bot.orchestra.store import challenge_breaker

        async with self.pool.acquire() as conn:
            return await challenge_breaker(conn, self.limits, "facebook")

    async def busy_elsewhere(self, campaign_id: str) -> bool:
        return bool(await self.pool.fetchval(
            """select exists (select 1 from campaign_windows where state = 'active' and campaign_id <> $1::uuid)
                   or exists (select 1 from campaigns where state = 'discovering' and id <> $1::uuid)""",
            campaign_id,
        ))

    async def open_window(self, campaign_id: str) -> Window | None:
        row = await self.pool.fetchrow(
            """select w.window_no, w.batch_id::text as batch_id, b.state,
                      (select coalesce(g.name, g.group_key)
                         from acquisition_batch_items i
                         join monitoring_sources s on s.id = i.source_id
                         join campaign_groups g on g.campaign_id = w.campaign_id and g.canonical_url = s.canonical_url
                        where i.batch_id = w.batch_id and i.state = 'running' limit 1) as current_group
                 from campaign_windows w join acquisition_batches b on b.id = w.batch_id
                where w.campaign_id = $1::uuid and w.state = 'active'""",
            campaign_id,
        )
        return Window(row["window_no"], row["batch_id"], row["state"], row["current_group"]) if row else None

    async def close_window(self, campaign_id: str, window: Window, actor: str) -> None:
        """Groups read by the batch become collected, the rest skipped; the window finishes."""
        outcome = window.batch_state if window.batch_state in TERMINAL_BATCH_STATES else "failed"
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn, actor)
            await conn.execute(
                """update campaign_groups g
                      set state = case when i.state = 'succeeded' then 'collected' else 'skipped' end,
                          reject_reason = case when i.state = 'succeeded' then null else 'window_' || i.state end
                     from acquisition_batch_items i join monitoring_sources s on s.id = i.source_id
                    where i.batch_id = $2::uuid and g.campaign_id = $1::uuid and g.batch_id = $2::uuid
                      and g.canonical_url = s.canonical_url and g.state = 'queued'""",
                campaign_id, window.batch_id,
            )
            await conn.execute(
                """update campaign_groups set state = 'skipped', reject_reason = 'window_' || $3
                    where campaign_id = $1::uuid and batch_id = $2::uuid and state = 'queued'""",
                campaign_id, window.batch_id, outcome,
            )
            await conn.execute(
                """update campaign_windows set state = 'finished', outcome = $3, finished_at = now()
                    where campaign_id = $1::uuid and batch_id = $2::uuid and state = 'active'""",
                campaign_id, window.batch_id, outcome,
            )

    async def windows_started(self, campaign_id: str) -> int:
        return int(await self.pool.fetchval(
            "select count(*) from campaign_windows where campaign_id = $1::uuid", campaign_id))

    async def next_groups(self, campaign_id: str, limit: int) -> list[QueuedGroup]:
        rows = await self.pool.fetch(
            """select group_key, canonical_url, name from campaign_groups
                where campaign_id = $1::uuid and state = 'queued' and batch_id is null
                order by window_no nulls last, relevance_score desc, group_key limit $2""",
            campaign_id, _window_size(limit),
        )
        return [QueuedGroup(r["group_key"], r["canonical_url"], r["name"]) for r in rows]

    async def start_window(self, campaign_id: str, groups: list[QueuedGroup], *, vertical: str,
                           actor: str) -> WindowStart | None:
        """Plan the window's batch in one transaction; ``ValueError`` (quota, breaker, profile) plans nothing."""
        from bot.orchestra.models import AcquisitionMethod, RunRequest
        from bot.orchestra.store import plan_facebook_batch

        if not 1 <= len(groups) <= WINDOW_SIZE:
            raise ValueError(f"a campaign window holds 1..{WINDOW_SIZE} groups")
        urls = [g.canonical_url for g in groups]
        request = RunRequest(platform="facebook", source_kind="group", targets=tuple(urls), vertical=vertical,
                             method=AcquisitionMethod.FACEBOOK_CONNECTOR)
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn, actor)
            window_no = await conn.fetchval(
                "select count(*) + 1 from campaign_windows where campaign_id = $1::uuid", campaign_id)
            planned = await plan_facebook_batch(conn, self.limits, request, actor=actor, notify_telegram_id=None,
                                                campaign_id=campaign_id, skip_unavailable=True)
            skipped = list(planned.skipped) if planned else urls
            if skipped:
                await conn.execute(
                    """update campaign_groups set state = 'skipped', reject_reason = 'source_unavailable'
                        where campaign_id = $1::uuid and canonical_url = any($2::text[]) and state = 'queued'""",
                    campaign_id, skipped,
                )
            if planned is None:
                return None
            included = [u for u in urls if u not in set(skipped)]
            await conn.execute(
                """update campaign_groups set batch_id = $3::uuid
                    where campaign_id = $1::uuid and canonical_url = any($2::text[]) and state = 'queued'""",
                campaign_id, included, planned.batch_id,
            )
            await conn.execute(
                """insert into campaign_windows (campaign_id, window_no, batch_id, group_count)
                   values ($1::uuid, $2, $3::uuid, $4)""",
                campaign_id, window_no, planned.batch_id, len(included),
            )
            return WindowStart(window_no, planned.batch_id, len(included), tuple(skipped))

    async def cancel_window_batch(self, batch_id: str, actor: str) -> None:
        from bot.orchestra.store import cancel_batches

        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn, actor)
            await cancel_batches(
                conn, "id = $1::uuid and state in ('planned', 'queued', 'running', 'human_verification_required')", batch_id)

    async def pending_analysis(self, campaign_id: str) -> int:
        return int(await self.pool.fetchval(
            f"""select count(*) {_CAMPAIGN_POSTS} and p.state = 'normalised'""", campaign_id))

    async def unstreamed_findings(self, campaign_id: str, limit: int) -> list[StreamFinding]:
        rows = await self.pool.fetch(
            f"""select f.id::text as id,
                       coalesce(nullif(f.structured_payload->>'formatted', ''), f.structured_payload->>'summary', '') as text,
                       f.structured_payload::text as payload, coalesce(p.body_text, '') as original,
                       f.analysis_metadata->>'language' as language, f.confidence::float8 as confidence, f.vertical
                  from findings f
                  join collected_posts p on p.id = f.post_id
                 where {_IN_CAMPAIGN} and f.state in ('ready', 'delivery_failed')
                   and not exists (select 1 from campaign_findings cf where cf.finding_id = f.id)
                 order by f.created_at, f.id limit {int(limit)}""",
            campaign_id,
        )
        return [StreamFinding(r["id"], r["text"], _payload(r["payload"]), r["original"], r["language"],
                              r["confidence"], r["vertical"]) for r in rows]

    async def claim_finding(self, campaign_id: str, finding_id: str) -> int | None:
        """Take the one send slot for a finding; returns the campaign's finding count, or None if taken."""
        async with self.pool.acquire() as conn, conn.transaction():
            claimed = await conn.fetchval(
                """insert into campaign_findings (finding_id, campaign_id) values ($1::uuid, $2::uuid)
                   on conflict do nothing returning 1""",
                finding_id, campaign_id,
            )
            if not claimed:
                return None
            return await _sent_count(conn, campaign_id)

    async def finding_sent(self, finding_id: str, message_id: int) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn, "campaign:runner")
            await conn.execute(
                """update campaign_findings set state = 'sent', telegram_message_id = $2, sent_at = now()
                    where finding_id = $1::uuid""",
                finding_id, message_id,
            )
            await conn.execute("update findings set state = 'delivered' where id = $1::uuid and state = 'ready'", finding_id)

    async def release_finding(self, finding_id: str) -> None:
        """Give a failed send back: an exact finding is picked up again, a held one is held again."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "delete from campaign_findings where finding_id = $1::uuid and state = 'sending' and bucket = 'exact'",
                finding_id)
            await conn.execute(
                "update campaign_findings set state = 'held' where finding_id = $1::uuid and state = 'sending'",
                finding_id)

    async def streamed_count(self, campaign_id: str) -> int:
        return int(await self.pool.fetchval(
            "select count(*) from campaign_findings where campaign_id = $1::uuid and state = 'sent'", campaign_id))

    async def hold_finding(self, campaign_id: str, finding_id: str, bucket: str, distance: float | None) -> bool:
        """File a similar/other finding without sending it; False if it already has a bucket."""
        return bool(await self.pool.fetchval(
            """insert into campaign_findings (finding_id, campaign_id, bucket, distance, state)
               values ($1::uuid, $2::uuid, $3, $4, 'held') on conflict do nothing returning 1""",
            finding_id, campaign_id, bucket, _distance(distance),
        ))

    async def held_findings(self, campaign_id: str, bucket: str, limit: int) -> list[StreamFinding]:
        """Held findings of one bucket, closest to the request first."""
        rows = await self.pool.fetch(
            f"""select f.id::text as id,
                       coalesce(nullif(f.structured_payload->>'formatted', ''), f.structured_payload->>'summary', '') as text,
                       f.structured_payload::text as payload, coalesce(p.body_text, '') as original,
                       f.analysis_metadata->>'language' as language, f.confidence::float8 as confidence, f.vertical
                  from campaign_findings cf
                  join findings f on f.id = cf.finding_id
                  join collected_posts p on p.id = f.post_id
                 where cf.campaign_id = $1::uuid and cf.bucket = $2 and cf.state = 'held'
                 order by cf.distance nulls last, f.created_at, f.id limit {int(limit)}""",
            campaign_id, bucket,
        )
        return [StreamFinding(r["id"], r["text"], _payload(r["payload"]), r["original"], r["language"],
                              r["confidence"], r["vertical"]) for r in rows]

    async def social_activity(self, campaign_id: str) -> SocialActivity:
        rows = await self.pool.fetch(
            """select platform, state, current_query, note from campaign_social_state
                where campaign_id = $1::uuid order by platform""", campaign_id)
        running = next((r for r in rows if r["state"] == "running"), None)
        return SocialActivity(
            searching=running["platform"] if running else None,
            query=running["current_query"] if running else None,
            pending=any(r["state"] in ("pending", "running") for r in rows),
            notes=tuple(r["note"] for r in rows if r["note"]),
        )

    async def claim_held(self, campaign_id: str, finding_id: str) -> int | None:
        """Take the send slot of a held finding (held -> sending); None if it is not held any more."""
        async with self.pool.acquire() as conn, conn.transaction():
            claimed = await conn.fetchval(
                """update campaign_findings set state = 'sending'
                    where finding_id = $1::uuid and campaign_id = $2::uuid and state = 'held' returning 1""",
                finding_id, campaign_id,
            )
            return await _sent_count(conn, campaign_id) if claimed else None

    async def exact_count(self, campaign_id: str) -> int:
        return int(await self.pool.fetchval(
            "select count(*) from campaign_findings where campaign_id = $1::uuid and bucket = 'exact'", campaign_id))

    async def offer_state(self, campaign_id: str, bucket: str) -> str | None:
        state: str | None = await self.pool.fetchval(
            "select state from campaign_offers where campaign_id = $1::uuid and bucket = $2", campaign_id, bucket)
        return state

    async def open_offer(self, campaign_id: str, bucket: str) -> bool:
        """Take the one question slot of a bucket; False if it was already asked."""
        return bool(await self.pool.fetchval(
            """insert into campaign_offers (campaign_id, bucket) values ($1::uuid, $2)
               on conflict do nothing returning 1""",
            campaign_id, bucket,
        ))

    async def offer_sent(self, campaign_id: str, bucket: str, message_id: int) -> None:
        await self.pool.execute(
            "update campaign_offers set telegram_message_id = $3 where campaign_id = $1::uuid and bucket = $2",
            campaign_id, bucket, message_id,
        )

    async def relevance(self, campaign_id: str, finding_id: str) -> Relevance | None:
        row = await self.pool.fetchrow(
            """select verdict, reason, deviation, model from campaign_finding_relevance
                where finding_id = $1::uuid and campaign_id = $2::uuid""", finding_id, campaign_id)
        return Relevance(row["verdict"], row["reason"], row["deviation"], row["model"]) if row else None

    async def save_relevance(self, campaign_id: str, finding_id: str, relevance: Relevance) -> None:
        await self.pool.execute(
            """insert into campaign_finding_relevance (finding_id, campaign_id, verdict, reason, deviation, model)
               values ($1::uuid, $2::uuid, $3, $4, $5, $6) on conflict do nothing""",
            finding_id, campaign_id, relevance.verdict, relevance.reason[:300],
            (relevance.deviation or None) and relevance.deviation[:120], (relevance.model or None) and relevance.model[:120])

    async def relevance_calls(self, campaign_id: str) -> int:
        return int(await self.pool.fetchval(
            "select count(*) from campaign_finding_relevance where campaign_id = $1::uuid", campaign_id))

    async def drop_offer(self, campaign_id: str, bucket: str) -> None:
        """The question could not be sent: free the slot so the next tick asks again."""
        await self.pool.execute(
            """delete from campaign_offers where campaign_id = $1::uuid and bucket = $2 and state = 'asked'
                  and telegram_message_id is null""",
            campaign_id, bucket,
        )


async def _sent_count(conn: asyncpg.Connection[asyncpg.Record], campaign_id: str) -> int:
    """The number on a card's «🔎 Найдено: N»: findings sent or being sent, never held ones."""
    return int(await conn.fetchval(
        "select count(*) from campaign_findings where campaign_id = $1::uuid and state in ('sending', 'sent')",
        campaign_id))


# A post belongs to a campaign through its Facebook batch (acquisition_runs ->
# batch items -> acquisition_batches.campaign_id) or through the social search
# that found it (campaign_social_posts, migration 022).
_IN_CAMPAIGN = """(exists (select 1 from acquisition_runs r
                      join acquisition_batch_items i on i.id = r.batch_item_id
                      join acquisition_batches b on b.id = i.batch_id
                     where r.id = p.acquisition_run_id and b.campaign_id = $1::uuid)
          or exists (select 1 from campaign_social_posts sp where sp.post_id = p.id and sp.campaign_id = $1::uuid))"""
_CAMPAIGN_POSTS = f"""from collected_posts p
     where {_IN_CAMPAIGN}"""


async def _set_actor(conn: asyncpg.Connection[asyncpg.Record], actor: str) -> None:
    await conn.execute("select set_config('app.actor', $1, true)", actor)


# --- in-process twin ------------------------------------------------------------------


@dataclass
class _MemGroup:
    group_key: str
    canonical_url: str
    name: str | None
    window_no: int | None
    score: float
    state: str = "queued"
    batch_id: str | None = None


@dataclass
class _MemBatch:
    id: str
    campaign_id: str
    urls: list[str]
    state: str = "queued"
    items: dict[str, str] = field(default_factory=dict)  # url -> item state


class MemoryRunStore:
    """In-process twin of ``PostgresRunStore``; tests drive batches and findings by hand."""

    def __init__(self, campaigns: object | None = None) -> None:
        self.campaigns = campaigns  # a MemoryCampaignStore, for active_campaigns()
        self.runs: dict[str, RunState] = {}
        self.groups: dict[str, list[_MemGroup]] = {}
        self.batches: dict[str, _MemBatch] = {}
        self.windows: dict[str, list[tuple[int, str, str]]] = {}  # cid -> [(window_no, batch_id, state)]
        self.findings: dict[str, list[StreamFinding]] = {}
        self.findings_state: dict[str, str] = {}
        self.streamed: dict[str, dict[str, int | None]] = {}  # cid -> finding -> message id (None = sending)
        self.profile = "ready"
        self.breaker: str | None = None
        self.refusal: str | None = None
        self.unavailable: set[str] = set()
        self.normalised: dict[str, int] = {}
        self.busy = False
        self.cancelled_batches: list[str] = []
        self.collector_running = False  # a batch run / acquisition run / launch holds the profile
        self.recovered: list[str] = []  # actors that freed the profile
        self.buckets: dict[str, tuple[str, float | None]] = {}  # finding -> (bucket, distance)
        self.held: dict[str, dict[str, None]] = {}  # cid -> held finding ids, in filing order
        self.desk = MemoryOfferDesk()  # the control plane's side of campaign_offers
        self.social: dict[str, SocialActivity] = {}  # cid -> social search activity
        self.relevances: dict[tuple[str, str], Relevance] = {}  # (cid, finding) -> stored AI verdict

    async def recover_discovery_profile(self) -> int:
        items = getattr(self.campaigns, "campaigns", {})
        discovering = any(c.state == "discovering" for c in items.values())
        if self.profile == "in_use" and discovering and not self.collector_running:
            self.profile = "ready"
            self.recovered.append(RECOVERY_ACTOR)
            return 1
        return 0

    # test helpers
    def add_groups(self, campaign_id: str, count: int, *, window_size: int = WINDOW_SIZE) -> None:
        rows = self.groups.setdefault(campaign_id, [])
        start = len(rows)
        for n in range(start, start + count):
            key = f"g{n + 1:03d}"
            rows.append(_MemGroup(key, f"https://www.facebook.com/groups/{key}/", f"Group {n + 1}",
                                  n // window_size + 1, 1.0 - n / 1000))

    def finish_batch(self, batch_id: str, state: str = "succeeded") -> None:
        batch = self.batches[batch_id]
        batch.state = state
        for url in batch.items:
            batch.items[url] = "succeeded" if state == "succeeded" else "cancelled"

    def add_finding(self, campaign_id: str, finding_id: str, text: str, **card: Any) -> None:
        self.findings.setdefault(campaign_id, []).append(StreamFinding(finding_id, text, **card))
        self.findings_state[finding_id] = "ready"

    # protocol
    async def active_campaigns(self) -> list[str]:
        items = getattr(self.campaigns, "campaigns", {})
        return [c.id for c in sorted(items.values(), key=lambda c: c.created_at)]

    async def get_run(self, campaign_id: str) -> RunState:
        return self.runs.get(campaign_id, RunState())

    async def save_run(self, campaign_id: str, run: RunState) -> None:
        self.runs[campaign_id] = run

    async def profile_state(self) -> str | None:
        return self.profile

    async def breaker_reason(self) -> str | None:
        return self.breaker

    async def busy_elsewhere(self, campaign_id: str) -> bool:
        return self.busy or any(
            cid != campaign_id and any(state == "active" for _, _, state in windows)
            for cid, windows in self.windows.items()
        )

    async def open_window(self, campaign_id: str) -> Window | None:
        for window_no, batch_id, state in self.windows.get(campaign_id, []):
            if state == "active":
                batch = self.batches[batch_id]
                running = next((u for u, s in batch.items.items() if s == "running"), None)
                name = next((g.name for g in self.groups.get(campaign_id, []) if g.canonical_url == running), None)
                return Window(window_no, batch_id, batch.state, name)
        return None

    async def close_window(self, campaign_id: str, window: Window, actor: str) -> None:
        batch = self.batches[window.batch_id]
        for group in self.groups.get(campaign_id, []):
            if group.batch_id == window.batch_id and group.state == "queued":
                group.state = "collected" if batch.items.get(group.canonical_url) == "succeeded" else "skipped"
        self.windows[campaign_id] = [(n, b, "finished" if b == window.batch_id else s)
                                     for n, b, s in self.windows[campaign_id]]

    async def windows_started(self, campaign_id: str) -> int:
        return len(self.windows.get(campaign_id, []))

    async def next_groups(self, campaign_id: str, limit: int) -> list[QueuedGroup]:
        waiting = [g for g in self.groups.get(campaign_id, []) if g.state == "queued" and g.batch_id is None]
        waiting.sort(key=lambda g: (g.window_no is None, g.window_no or 0, -g.score, g.group_key))
        return [QueuedGroup(g.group_key, g.canonical_url, g.name) for g in waiting[:_window_size(limit)]]

    async def start_window(self, campaign_id: str, groups: list[QueuedGroup], *, vertical: str,
                           actor: str) -> WindowStart | None:
        if not 1 <= len(groups) <= WINDOW_SIZE:
            raise ValueError(f"a campaign window holds 1..{WINDOW_SIZE} groups")
        if self.refusal:
            raise ValueError(self.refusal)
        if self.profile != "ready":
            raise ValueError("no ready facebook browser profile is provisioned")
        rows = {g.canonical_url: g for g in self.groups.get(campaign_id, [])}
        skipped = tuple(g.canonical_url for g in groups if g.canonical_url in self.unavailable)
        for url in skipped:
            rows[url].state = "skipped"
        included = [g.canonical_url for g in groups if g.canonical_url not in skipped]
        if not included:
            return None
        batch_id = str(uuid.uuid4())
        self.batches[batch_id] = _MemBatch(batch_id, campaign_id, included, items=dict.fromkeys(included, "queued"))
        for url in included:
            rows[url].batch_id = batch_id
        windows = self.windows.setdefault(campaign_id, [])
        windows.append((len(windows) + 1, batch_id, "active"))
        return WindowStart(len(windows), batch_id, len(included), skipped)

    async def cancel_window_batch(self, batch_id: str, actor: str) -> None:
        batch = self.batches[batch_id]
        if batch.state not in TERMINAL_BATCH_STATES:
            self.finish_batch(batch_id, "cancelled")
            self.cancelled_batches.append(batch_id)

    async def pending_analysis(self, campaign_id: str) -> int:
        return self.normalised.get(campaign_id, 0)

    async def social_activity(self, campaign_id: str) -> SocialActivity:
        return self.social.get(campaign_id, SocialActivity())

    async def unstreamed_findings(self, campaign_id: str, limit: int) -> list[StreamFinding]:
        done = self.streamed.get(campaign_id, {})
        return [f for f in self.findings.get(campaign_id, []) if f.id not in done and f.id not in self.buckets][:limit]

    async def claim_finding(self, campaign_id: str, finding_id: str) -> int | None:
        done = self.streamed.setdefault(campaign_id, {})
        if finding_id in done or finding_id in self.buckets:
            return None
        done[finding_id] = None
        self.buckets[finding_id] = ("exact", 0.0)
        return len(done)

    async def finding_sent(self, finding_id: str, message_id: int) -> None:
        for done in self.streamed.values():
            if finding_id in done:
                done[finding_id] = message_id
        self.findings_state[finding_id] = "delivered"

    async def release_finding(self, finding_id: str) -> None:
        for cid, done in self.streamed.items():
            if done.get(finding_id, 0) is None:
                del done[finding_id]
                if self.buckets[finding_id][0] == "exact":
                    del self.buckets[finding_id]
                else:
                    self.held.setdefault(cid, {})[finding_id] = None

    async def streamed_count(self, campaign_id: str) -> int:
        return sum(1 for m in self.streamed.get(campaign_id, {}).values() if m is not None)

    async def hold_finding(self, campaign_id: str, finding_id: str, bucket: str, distance: float | None) -> bool:
        if finding_id in self.buckets or finding_id in self.streamed.get(campaign_id, {}):
            return False
        self.buckets[finding_id] = (bucket, _distance(distance))
        self.held.setdefault(campaign_id, {})[finding_id] = None
        return True

    async def held_findings(self, campaign_id: str, bucket: str, limit: int) -> list[StreamFinding]:
        order = list(self.held.get(campaign_id, {}))
        by_id = {f.id: f for f in self.findings.get(campaign_id, [])}
        ids = [i for i in order if self.buckets[i][0] == bucket]
        ids.sort(key=lambda i: (self.buckets[i][1] is None, self.buckets[i][1] or 0.0, order.index(i)))
        return [by_id[i] for i in ids[:limit]]

    async def claim_held(self, campaign_id: str, finding_id: str) -> int | None:
        held = self.held.get(campaign_id, {})
        if finding_id not in held:
            return None
        del held[finding_id]
        done = self.streamed.setdefault(campaign_id, {})
        done[finding_id] = None
        return len(done)

    async def exact_count(self, campaign_id: str) -> int:
        return sum(1 for f in self.findings.get(campaign_id, [])
                   if self.buckets.get(f.id, ("", None))[0] == "exact")

    async def offer_state(self, campaign_id: str, bucket: str) -> str | None:
        offer = self.desk.offers.get((campaign_id, bucket))
        return offer.state if offer else None

    async def open_offer(self, campaign_id: str, bucket: str) -> bool:
        if (campaign_id, bucket) in self.desk.offers:
            return False
        campaign = getattr(self.campaigns, "campaigns", {}).get(campaign_id)
        self.desk.offers[(campaign_id, bucket)] = MemoryOffer(campaign.requested_by if campaign else 0)
        return True

    async def offer_sent(self, campaign_id: str, bucket: str, message_id: int) -> None:
        self.desk.offers[(campaign_id, bucket)].message_id = message_id

    async def drop_offer(self, campaign_id: str, bucket: str) -> None:
        offer = self.desk.offers.get((campaign_id, bucket))
        if offer is not None and offer.state == "asked" and offer.message_id is None:
            del self.desk.offers[(campaign_id, bucket)]

    async def relevance(self, campaign_id: str, finding_id: str) -> Relevance | None:
        return self.relevances.get((campaign_id, finding_id))

    async def save_relevance(self, campaign_id: str, finding_id: str, relevance: Relevance) -> None:
        self.relevances.setdefault((campaign_id, finding_id), relevance)

    async def relevance_calls(self, campaign_id: str) -> int:
        return sum(1 for cid, _ in self.relevances if cid == campaign_id)
