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
from collections.abc import Collection
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from .leads import Person, Sighting
from .models import WINDOW_SIZE
from .offers import MemoryOffer, MemoryOfferDesk
from .reach import KIND_ORDER, Contact
from .relevance import Relevance

if TYPE_CHECKING:
    import asyncpg

    from bot.orchestra.store import SafetyLimits

TERMINAL_BATCH_STATES = frozenset({"succeeded", "failed", "cancelled"})
RECENT_SECONDS = 86_400  # finished campaigns still stream late findings for a day
DEAD_DAYS = 7          # a group whose newest post is this much older than its last read is dead ...
DEAD_RECHECK_DAYS = 30  # ... until that read is this old: then it is read once more (it may have revived)


@dataclass(frozen=True, slots=True)
class RunState:
    status_text: str | None = None
    next_window_at: datetime | None = None
    drain_started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SourceCount:
    """What one source gave a campaign (``summary``): ``platform`` facebook / website / tiktok / instagram /
    linkedin; ``name`` the site's host for a website, else the platform; ``sources`` groups (or pages) read."""

    platform: str
    name: str
    sources: int = 0
    posts: int = 0       # posts / pages read
    relevant: int = 0    # the analysis kept them (findings)
    sent: int = 0        # cards sent
    held: int = 0        # similar / other, waiting for «Одобрить»


@dataclass(frozen=True, slots=True)
class OutcomeCount:
    """How many findings of a campaign ended in one state (``final_report``): ``state`` held / sending / sent /
    duplicate, ``bucket`` exact / similar / other / excluded, ``why`` the reason category (None for exact ones)."""

    state: str
    bucket: str
    why: str | None
    count: int


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
    current_group_url: str | None = None  # its Facebook address


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
    url: str | None = None  # the post's own link (a Facebook post's comments are read for leads)
    hold_reason: str | None = None  # why it is held unverified (owner-facing note), from ``hold_finding``


@dataclass(frozen=True, slots=True)
class SentFinding:
    """An exact card that was sent: a candidate cluster head for ``dedup.same_object``.

    ``number`` is the card's place among the campaign's sent cards (its «Найдено: N»);
    ``links`` the other sightings already attached, ``{"url", "site"}`` each.
    """

    finding: StreamFinding
    message_id: int | None
    number: int = 0
    cluster_id: str | None = None
    links: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class ClusterHead:
    """The head card a duplicate was attached to, with all its links so far."""

    message_id: int | None
    links: tuple[dict[str, str], ...]


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
    url: str | None = None  # the search page the running query has open


@dataclass(frozen=True, slots=True)
class ReachActivity:
    """The investor reach stage (``bot.campaign.reach``): the platform its running query is aimed at, if any."""

    platform: str | None = None
    query: str | None = None


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
    async def analysis_progress(self, campaign_id: str) -> tuple[int, int]:
        """(done, total): the campaign's collected posts already analysed or rejected, and all of them."""
        ...
    async def unstreamed_findings(self, campaign_id: str, limit: int,
                                  skip: Collection[str] = ()) -> list[StreamFinding]: ...
    async def claim_finding(self, campaign_id: str, finding_id: str) -> int | None: ...
    async def finding_sent(self, finding_id: str, message_id: int) -> None: ...
    async def release_finding(self, finding_id: str) -> None: ...
    async def streamed_count(self, campaign_id: str) -> int: ...
    async def recent_sent_findings(self, campaign_id: str, limit: int = 200) -> list[SentFinding]: ...
    async def stream_finding(self, finding_id: str) -> StreamFinding | None: ...
    async def attach_to_cluster(self, finding_id: str, cluster_id: str, link: dict[str, str]) -> ClusterHead | None: ...
    # exact / similar / other (migration 019, ``tolerance`` and ``offers``)
    async def hold_finding(self, campaign_id: str, finding_id: str, bucket: str, distance: float | None,
                           reason: str | None = None, why: str | None = None) -> bool: ...
    async def held_findings(self, campaign_id: str, bucket: str, limit: int) -> list[StreamFinding]: ...
    async def claim_held(self, campaign_id: str, finding_id: str) -> int | None: ...
    async def exact_count(self, campaign_id: str) -> int: ...
    async def offer_state(self, campaign_id: str, bucket: str) -> str | None: ...
    async def open_offer(self, campaign_id: str, bucket: str) -> bool: ...
    async def offer_sent(self, campaign_id: str, bucket: str, message_id: int) -> None: ...
    async def drop_offer(self, campaign_id: str, bucket: str) -> None: ...
    async def social_activity(self, campaign_id: str) -> SocialActivity: ...
    async def reach_activity(self, campaign_id: str) -> ReachActivity: ...
    # the AI relevance verdict, once per finding (migration 023, ``relevance``)
    async def relevance(self, campaign_id: str, finding_id: str) -> Relevance | None: ...
    async def save_relevance(self, campaign_id: str, finding_id: str, relevance: Relevance) -> None: ...
    async def relevance_calls(self, campaign_id: str) -> int: ...
    # investor leads from the comments under sent objects (migration 026, ``leads``)
    async def queue_comment_read(self, campaign_id: str, finding_id: str, post_url: str, max_posts: int) -> bool: ...
    async def stored_people(self, campaign_id: str, location: str, days: int, limit: int) -> list[Person]: ...
    async def people_sent(self, campaign_id: str) -> int: ...
    async def claim_person(self, campaign_id: str, profile_key: str) -> bool: ...
    async def person_sent(self, campaign_id: str, profile_key: str, message_id: int) -> None: ...
    async def release_person(self, campaign_id: str, profile_key: str) -> None: ...
    # investor reach across platforms (migration 027, ``reach``)
    async def stored_contacts(self, campaign_id: str, location: str, days: int, limit: int) -> list[Contact]: ...
    async def reach_pending(self, campaign_id: str) -> bool: ...
    # the end-of-campaign summary (migration 030, ``summary``)
    async def source_counts(self, campaign_id: str) -> list[SourceCount]: ...
    async def claim_summary(self, campaign_id: str) -> bool: ...
    async def release_summary(self, campaign_id: str) -> None: ...
    # the user's final report (migration 038, ``final_report``)
    async def outcome_counts(self, campaign_id: str) -> list[OutcomeCount]: ...
    async def claim_final_report(self, campaign_id: str) -> bool: ...
    async def release_final_report(self, campaign_id: str) -> None: ...


def _distance(value: float | None) -> float | None:
    return value if value is not None and math.isfinite(value) else None


def _links(raw: str | None) -> tuple[dict[str, str], ...]:
    try:
        value = json.loads(raw) if raw else []
    except ValueError:
        return ()
    return tuple({"url": str(x.get("url") or ""), "site": str(x.get("site") or "")}
                 for x in value if isinstance(x, dict) and x.get("url")) if isinstance(value, list) else ()


def _payload(raw: str | None) -> dict[str, Any] | None:
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _window_size(limit: int) -> int:
    return max(1, min(limit, WINDOW_SIZE))


class PostgresRunStore:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record], limits: SafetyLimits, *,
                 dead_days: int = DEAD_DAYS) -> None:
        if not 1 <= dead_days <= 365:
            raise ValueError("dead_days must be 1..365")
        self.pool, self.limits, self.dead_days = pool, limits, dead_days

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
                        where i.batch_id = w.batch_id and i.state = 'running' limit 1) as current_group,
                      (select g.canonical_url
                         from acquisition_batch_items i
                         join monitoring_sources s on s.id = i.source_id
                         join campaign_groups g on g.campaign_id = w.campaign_id and g.canonical_url = s.canonical_url
                        where i.batch_id = w.batch_id and i.state = 'running' limit 1) as current_group_url
                 from campaign_windows w join acquisition_batches b on b.id = w.batch_id
                where w.campaign_id = $1::uuid and w.state = 'active'""",
            campaign_id,
        )
        return (Window(row["window_no"], row["batch_id"], row["state"], row["current_group"],
                       row["current_group_url"]) if row else None)

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
        """The next groups to read: the Facebook read quota goes to live groups first.

        A dead group is skipped: a read in the last ``DEAD_RECHECK_DAYS`` days found no posts, or its
        newest post was ``dead_days`` older than that read (FACEBOOK_GROUP_DEAD_DAYS). A group any
        campaign read in the last ``EMPTY_READ_DAYS`` days that gave no finding in the last
        ``FINDINGS_DAYS`` days is skipped too (state ``skipped``, the reason stored); groups that gave
        findings recently come first, then the discovery order.
        """
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                f"""update campaign_groups g set state = 'skipped',
                           reject_reason = 'dead: no new posts for ' || $2::int || ' days', updated_at = now()
                     where g.campaign_id = $1::uuid and g.state = 'queued' and g.batch_id is null
                       and exists ({_DEAD_GROUP})""",
                campaign_id, self.dead_days)
            await conn.execute(
                f"""update campaign_groups g set state = 'skipped', reject_reason = 'no findings in recent reads',
                           updated_at = now()
                     where g.campaign_id = $1::uuid and g.state = 'queued' and g.batch_id is null
                       and exists (select 1 from monitoring_sources s join acquisition_runs r on r.source_id = s.id
                                    where s.canonical_url = g.canonical_url and r.state = 'succeeded'
                                      and r.finished_at > now() - interval '{EMPTY_READ_DAYS} days')
                       and not exists ({_RECENT_FINDING})""",
                campaign_id)
            rows = await conn.fetch(
                f"""select g.group_key, g.canonical_url, g.name from campaign_groups g
                     where g.campaign_id = $1::uuid and g.state = 'queued' and g.batch_id is null
                     order by exists ({_RECENT_FINDING}) desc, g.window_no nulls last, g.relevance_score desc,
                              g.group_key limit $2""",
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
            # The comment reader (bot/campaign/leads.py) claims the profile under the same row lock,
            # so it either sees this window's batch or holds the profile and the plan is refused.
            await conn.execute("select 1 from browser_profiles where platform = 'facebook' and deleted_at is null for update")
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

    async def analysis_progress(self, campaign_id: str) -> tuple[int, int]:
        row = await self.pool.fetchrow(
            f"""select count(*) filter (where p.state in ('analysed', 'rejected')) as done, count(*) as total
                {_CAMPAIGN_POSTS}""", campaign_id)
        return int(row["done"]), int(row["total"])

    async def unstreamed_findings(self, campaign_id: str, limit: int,
                                  skip: Collection[str] = ()) -> list[StreamFinding]:
        rows = await self.pool.fetch(
            f"""select f.id::text as id,
                       coalesce(nullif(f.structured_payload->>'formatted', ''), f.structured_payload->>'summary', '') as text,
                       f.structured_payload::text as payload, coalesce(p.body_text, '') as original,
                       f.analysis_metadata->>'language' as language, f.confidence::float8 as confidence, f.vertical,
                       p.canonical_url as url
                  from findings f
                  join collected_posts p on p.id = f.post_id
                 where {_IN_CAMPAIGN} and f.state in ('ready', 'delivery_failed')
                   and not exists (select 1 from campaign_findings cf where cf.finding_id = f.id)
                   and f.id::text <> all($2::text[])
                 order by f.created_at, f.id limit {int(limit)}""",
            campaign_id, list(skip),
        )
        return [StreamFinding(r["id"], r["text"], _payload(r["payload"]), r["original"], r["language"],
                              r["confidence"], r["vertical"], r["url"]) for r in rows]

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
            return await _number_card(conn, campaign_id, finding_id)

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

    async def source_counts(self, campaign_id: str) -> list[SourceCount]:
        """Per source: posts read, findings, cards sent and held (similar/other); a site by its host."""
        rows = await self.pool.fetch(
            f"""select s.platform,
                       case when s.platform = 'website' then coalesce(s.display_name, s.canonical_url)
                            else s.platform end as name,
                       count(distinct s.id) as sources, count(distinct p.id) as posts,
                       count(distinct f.id) as relevant,
                       count(distinct cf.finding_id) filter (where cf.state = 'sent') as sent,
                       count(distinct cf.finding_id) filter (where cf.state = 'held'
                                                              and cf.bucket in ('similar', 'other')) as held
                  from collected_posts p
                  join monitoring_sources s on s.id = p.source_id
                  left join findings f on f.post_id = p.id
                  left join campaign_findings cf on cf.finding_id = f.id and cf.campaign_id = $1::uuid
                 where {_IN_CAMPAIGN}
                 group by 1, 2""",
            campaign_id,
        )
        return [SourceCount(r["platform"], r["name"], r["sources"], r["posts"], r["relevant"], r["sent"], r["held"])
                for r in rows]

    async def claim_summary(self, campaign_id: str) -> bool:
        """Take the one chance to send the summary; False when it was sent (or is being sent) already."""
        return bool(await self.pool.fetchval(
            """insert into campaign_runs (campaign_id, summary_sent_at) values ($1::uuid, now())
               on conflict (campaign_id) do update set summary_sent_at = now(), updated_at = now()
                 where campaign_runs.summary_sent_at is null
               returning 1""", campaign_id))

    async def release_summary(self, campaign_id: str) -> None:
        await self.pool.execute("update campaign_runs set summary_sent_at = null where campaign_id = $1::uuid",
                                campaign_id)

    async def outcome_counts(self, campaign_id: str) -> list[OutcomeCount]:
        """What became of every finding of the campaign: (state, bucket, why) -> how many."""
        rows = await self.pool.fetch(
            """select state, bucket, why, count(*) as n from campaign_findings
                where campaign_id = $1::uuid group by state, bucket, why order by state, bucket, why""", campaign_id)
        return [OutcomeCount(r["state"], r["bucket"], r["why"], r["n"]) for r in rows]

    async def claim_final_report(self, campaign_id: str) -> bool:
        """Take the one chance to send the final report; False when it was sent (or is being sent) already."""
        return bool(await self.pool.fetchval(
            """insert into campaign_runs (campaign_id, final_report_sent_at) values ($1::uuid, now())
               on conflict (campaign_id) do update set final_report_sent_at = now(), updated_at = now()
                 where campaign_runs.final_report_sent_at is null
               returning 1""", campaign_id))

    async def release_final_report(self, campaign_id: str) -> None:
        await self.pool.execute("update campaign_runs set final_report_sent_at = null where campaign_id = $1::uuid",
                                campaign_id)

    async def recent_sent_findings(self, campaign_id: str, limit: int = 200) -> list[SentFinding]:
        """The campaign's newest sent exact cards (cluster heads), with their listing fields and links.

        The post's own text is not fetched (it can be large and the comparison does not read it): ``original`` is
        empty here, ``stream_finding`` loads a whole finding. ``number`` is the number stored when the card was
        claimed; rows from before it was stored count the sent exact cards instead.
        """
        rows = await self.pool.fetch(
            f"""select f.id::text as id,
                       coalesce(nullif(f.structured_payload->>'formatted', ''), f.structured_payload->>'summary', '') as text,
                       f.structured_payload::text as payload,
                       f.analysis_metadata->>'language' as language, f.confidence::float8 as confidence, f.vertical,
                       p.canonical_url as url, cf.telegram_message_id, cf.cluster_id::text as cluster_id,
                       cf.cluster_links::text as links, coalesce(cf.card_number, cf.counted)::int as number
                  from (select cf2.*, row_number() over (order by cf2.sent_at, cf2.finding_id) as counted
                          from campaign_findings cf2
                         where cf2.campaign_id = $1::uuid and cf2.state = 'sent' and cf2.bucket = 'exact') cf
                  join findings f on f.id = cf.finding_id
                  join collected_posts p on p.id = f.post_id
                 where cf.duplicate_of is null
                 order by cf.sent_at desc, cf.finding_id limit {int(limit)}""",
            campaign_id,
        )
        return [SentFinding(
            StreamFinding(r["id"], r["text"], _payload(r["payload"]), "", r["language"], r["confidence"],
                          r["vertical"], r["url"]),
            r["telegram_message_id"], int(r["number"]), r["cluster_id"], _links(r["links"])) for r in rows]

    async def stream_finding(self, finding_id: str) -> StreamFinding | None:
        """One finding with the post's own text (what the card is rendered from), or None."""
        row = await self.pool.fetchrow(
            """select f.id::text as id,
                      coalesce(nullif(f.structured_payload->>'formatted', ''), f.structured_payload->>'summary', '') as text,
                      f.structured_payload::text as payload, coalesce(p.body_text, '') as original,
                      f.analysis_metadata->>'language' as language, f.confidence::float8 as confidence, f.vertical,
                      p.canonical_url as url
                 from findings f join collected_posts p on p.id = f.post_id
                where f.id = $1::uuid""", finding_id)
        if row is None:
            return None
        return StreamFinding(row["id"], row["text"], _payload(row["payload"]), row["original"], row["language"],
                             row["confidence"], row["vertical"], row["url"])

    async def attach_to_cluster(self, finding_id: str, cluster_id: str, link: dict[str, str]) -> ClusterHead | None:
        """Record ``finding_id`` as a duplicate of the head ``cluster_id`` (never sent) and add ``link`` to the head.

        Returns the head's message id and all its links, or None when the head is not a sent card of the same
        campaign, or when the finding itself is already sent or held (nothing is written then: the transaction is
        rolled back). Attaching twice adds the link once. The finding is delivered like a sent one: its ``findings``
        row leaves ``ready`` (nothing else must pick it up).
        """
        try:
            async with self.pool.acquire() as conn, conn.transaction():
                await _set_actor(conn, "campaign:runner")
                head = await conn.fetchrow(
                    """select campaign_id, telegram_message_id, cluster_links::text as links from campaign_findings
                        where finding_id = $1::uuid and state = 'sent' and duplicate_of is null for update""", cluster_id)
                if head is None:
                    return None
                stored = await conn.fetchval(
                    """insert into campaign_findings (finding_id, campaign_id, state, duplicate_of, cluster_id)
                       values ($1::uuid, $2::uuid, 'duplicate', $3::uuid, $3::uuid)
                       on conflict (finding_id) do update set state = 'duplicate', duplicate_of = excluded.duplicate_of,
                           cluster_id = excluded.cluster_id
                         where campaign_findings.state in ('sending', 'duplicate')
                       returning 1""",
                    finding_id, head["campaign_id"], cluster_id)
                if not stored:  # the finding was sent or held meanwhile: it is no duplicate
                    raise _NotAttached
                links = list(_links(head["links"]))
                if link.get("url") and all(x.get("url") != link["url"] for x in links):
                    links.append({"url": str(link["url"]), "site": str(link.get("site") or "")})
                await conn.execute(
                    "update campaign_findings set cluster_id = $1::uuid, cluster_links = $2::jsonb where finding_id = $1::uuid",
                    cluster_id, json.dumps(links))
                await conn.execute("update findings set state = 'delivered' where id = $1::uuid and state = 'ready'",
                                   finding_id)
                return ClusterHead(head["telegram_message_id"], tuple(links))
        except _NotAttached:
            return None

    async def hold_finding(self, campaign_id: str, finding_id: str, bucket: str, distance: float | None,
                           reason: str | None = None, why: str | None = None) -> bool:
        """File a similar/other/excluded finding without sending it; False if it already has a bucket.

        ``reason``: the owner-facing note of an unverified one; ``why``: the category the final report counts it under."""
        return bool(await self.pool.fetchval(
            """insert into campaign_findings (finding_id, campaign_id, bucket, distance, state, hold_reason, why)
               values ($1::uuid, $2::uuid, $3, $4, 'held', $5, $6) on conflict do nothing returning 1""",
            finding_id, campaign_id, bucket, _distance(distance), reason[:300] if reason else None,
            why[:40] if why else None,
        ))

    async def held_findings(self, campaign_id: str, bucket: str, limit: int) -> list[StreamFinding]:
        """Held findings of one bucket, closest to the request first."""
        rows = await self.pool.fetch(
            f"""select f.id::text as id,
                       coalesce(nullif(f.structured_payload->>'formatted', ''), f.structured_payload->>'summary', '') as text,
                       f.structured_payload::text as payload, coalesce(p.body_text, '') as original,
                       f.analysis_metadata->>'language' as language, f.confidence::float8 as confidence, f.vertical,
                       p.canonical_url as url, cf.hold_reason
                  from campaign_findings cf
                  join findings f on f.id = cf.finding_id
                  join collected_posts p on p.id = f.post_id
                 where cf.campaign_id = $1::uuid and cf.bucket = $2 and cf.state = 'held'
                 order by cf.distance nulls last, f.created_at, f.id limit {int(limit)}""",
            campaign_id, bucket,
        )
        return [StreamFinding(r["id"], r["text"], _payload(r["payload"]), r["original"], r["language"],
                              r["confidence"], r["vertical"], r["url"], r["hold_reason"]) for r in rows]

    async def social_activity(self, campaign_id: str) -> SocialActivity:
        rows = await self.pool.fetch(
            """select platform, state, current_query, note, current_url from campaign_social_state
                where campaign_id = $1::uuid order by platform""", campaign_id)
        running = next((r for r in rows if r["state"] == "running"), None)
        return SocialActivity(
            searching=running["platform"] if running else None,
            query=running["current_query"] if running else None,
            pending=any(r["state"] in ("pending", "running") for r in rows),
            notes=tuple(r["note"] for r in rows if r["note"]),
            url=running["current_url"] if running else None,
        )

    async def reach_activity(self, campaign_id: str) -> ReachActivity:
        row = await self.pool.fetchrow(
            "select platform, current from campaign_reach where campaign_id = $1::uuid and state = 'running'",
            campaign_id)
        return ReachActivity(row["platform"], row["current"]) if row and row["current"] else ReachActivity()

    async def claim_held(self, campaign_id: str, finding_id: str) -> int | None:
        """Take the send slot of a held finding (held -> sending); None if it is not held any more."""
        async with self.pool.acquire() as conn, conn.transaction():
            claimed = await conn.fetchval(
                """update campaign_findings set state = 'sending'
                    where finding_id = $1::uuid and campaign_id = $2::uuid and state = 'held' returning 1""",
                finding_id, campaign_id,
            )
            return await _number_card(conn, campaign_id, finding_id) if claimed else None

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
            """select verdict, reason, deviation, model, review::text as review from campaign_finding_relevance
                where finding_id = $1::uuid and campaign_id = $2::uuid""", finding_id, campaign_id)
        return (Relevance(row["verdict"], row["reason"], row["deviation"], row["model"], _payload(row["review"]))
                if row else None)

    async def save_relevance(self, campaign_id: str, finding_id: str, relevance: Relevance) -> None:
        await self.pool.execute(
            """insert into campaign_finding_relevance (finding_id, campaign_id, verdict, reason, deviation, model, review)
               values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7::jsonb) on conflict do nothing""",
            finding_id, campaign_id, relevance.verdict, relevance.reason[:300],
            (relevance.deviation or None) and relevance.deviation[:120], (relevance.model or None) and relevance.model[:120],
            json.dumps(relevance.review, ensure_ascii=False) if relevance.review else None)

    async def relevance_calls(self, campaign_id: str) -> int:
        return int(await self.pool.fetchval(
            "select count(*) from campaign_finding_relevance where campaign_id = $1::uuid", campaign_id))

    async def queue_comment_read(self, campaign_id: str, finding_id: str, post_url: str, max_posts: int) -> bool:
        """Queue a sent Facebook post for one comment read, at most ``max_posts`` per campaign."""
        return bool(await self.pool.fetchval(
            """insert into campaign_comment_reads (campaign_id, finding_id, post_url)
               select $1::uuid, $2::uuid, $3
                where (select count(*) from campaign_comment_reads where campaign_id = $1::uuid) < $4
               on conflict do nothing returning 1""",
            campaign_id, finding_id, post_url[:2000], max_posts,
        ))

    async def stored_people(self, campaign_id: str, location: str, days: int, limit: int) -> list[Person]:
        """People stored from comments under objects in ``location`` (last ``days``) this campaign has not sent:
        investors first, then those with more objects, then the most recent."""
        rows = await self.pool.fetch(
            f"""with people as (
                    select l.profile_key, bool_or(l.role = 'investor') as investor, count(*) as objects,
                           max(l.created_at) as last_seen
                      from investor_leads l
                     where l.location = $2 and l.created_at > now() - make_interval(days => $3)
                       and not exists (select 1 from campaign_lead_deliveries d
                                        where d.campaign_id = $1::uuid and d.profile_key = l.profile_key)
                     group by l.profile_key
                     order by investor desc, objects desc, last_seen desc, l.profile_key limit {int(limit)})
                select l.profile_key, l.profile_url, l.author_name, l.post_url, l.comment_text, l.role, l.summary_ru,
                       l.object_facts::text as facts, l.created_at
                  from investor_leads l join people p on p.profile_key = l.profile_key
                 where l.location = $2 and l.created_at > now() - make_interval(days => $3)
                 order by p.investor desc, p.objects desc, p.last_seen desc, l.profile_key, l.created_at desc""",
            campaign_id, location, days,
        )
        people: dict[str, list[Any]] = {}
        for r in rows:
            people.setdefault(r["profile_key"], []).append(r)
        return [Person(key, group[0]["profile_url"], next((r["author_name"] for r in group if r["author_name"]), None),
                       tuple(Sighting(r["post_url"], r["comment_text"], r["role"], r["created_at"], r["summary_ru"],
                                      _payload(r["facts"]) or {}) for r in group))
                for key, group in people.items()]

    async def people_sent(self, campaign_id: str) -> int:
        return int(await self.pool.fetchval(
            "select count(*) from campaign_lead_deliveries where campaign_id = $1::uuid", campaign_id))

    async def claim_person(self, campaign_id: str, profile_key: str) -> bool:
        return bool(await self.pool.fetchval(
            """insert into campaign_lead_deliveries (campaign_id, profile_key) values ($1::uuid, $2)
               on conflict do nothing returning 1""", campaign_id, profile_key))

    async def person_sent(self, campaign_id: str, profile_key: str, message_id: int) -> None:
        await self.pool.execute(
            """update campaign_lead_deliveries set state = 'sent', telegram_message_id = $3, sent_at = now()
                where campaign_id = $1::uuid and profile_key = $2 and state = 'sending'""",
            campaign_id, profile_key, message_id)

    async def release_person(self, campaign_id: str, profile_key: str) -> None:
        await self.pool.execute(
            """delete from campaign_lead_deliveries
                where campaign_id = $1::uuid and profile_key = $2 and state = 'sending'""", campaign_id, profile_key)

    async def stored_contacts(self, campaign_id: str, location: str, days: int, limit: int) -> list[Contact]:
        """Relevant reach results of ``location`` (last ``days``) this campaign has not sent: investors first."""
        order = " ".join(f"when '{kind}' then {rank}" for kind, rank in KIND_ORDER.items())
        rows = await self.pool.fetch(
            f"""select url_key, url, platform, kind, name, coalesce(title, '') as title,
                       coalesce(snippet, '') as snippet, summary_ru, created_at
                  from reach_contacts r
                 where r.relevant and r.location = $2 and r.created_at > now() - make_interval(days => $3)
                   and not exists (select 1 from campaign_lead_deliveries d
                                    where d.campaign_id = $1::uuid and d.profile_key = 'reach:' || r.url_key)
                 order by case r.kind {order} else 9 end, r.confidence desc nulls last, r.created_at, r.url_key
                 limit {int(limit)}""",
            campaign_id, location, days)
        return [Contact(r["url_key"], r["url"], r["platform"], r["kind"], r["name"], r["title"], r["snippet"],
                        r["summary_ru"], r["created_at"]) for r in rows]

    async def reach_pending(self, campaign_id: str) -> bool:
        return bool(await self.pool.fetchval(
            "select exists (select 1 from campaign_reach where campaign_id = $1::uuid and state = 'running')",
            campaign_id))

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


async def _number_card(conn: asyncpg.Connection[asyncpg.Record], campaign_id: str, finding_id: str) -> int:
    """The finding's card number (the count of cards sent or being sent, itself included), stored on its row so an
    edit of the card later shows the same «Найдено: N»."""
    number = await _sent_count(conn, campaign_id)
    await conn.execute("update campaign_findings set card_number = $2 where finding_id = $1::uuid",
                       finding_id, number)
    return number


# A post belongs to a campaign through its Facebook batch (acquisition_runs ->
# batch items -> acquisition_batches.campaign_id) or through the social search
# that found it (campaign_social_posts, migration 022).
# $2: dead_days. The last successful read of the group's source, recent enough to trust.
_DEAD_GROUP = f"""select 1 from monitoring_sources s3
                   cross join lateral (select max(r3.finished_at) as last_read from acquisition_runs r3
                                        where r3.source_id = s3.id and r3.state = 'succeeded') lr
                   where s3.canonical_url = g.canonical_url and s3.platform = 'facebook'
                     and lr.last_read > now() - interval '{DEAD_RECHECK_DAYS} days'
                     and (s3.configuration->>'facebook_group_state' = 'INACTIVE'
                          or (select max(p3.published_at) from collected_posts p3 where p3.source_id = s3.id)
                             < lr.last_read - make_interval(days => $2::int))"""
EMPTY_READ_DAYS = 3   # a group read this recently ...
FINDINGS_DAYS = 14    # ... with no finding for this long is skipped: the quota goes to live groups
_RECENT_FINDING = f"""select 1 from monitoring_sources s2 join findings f on f.source_id = s2.id
                      where s2.canonical_url = g.canonical_url
                        and f.created_at > now() - interval '{FINDINGS_DAYS} days'"""
_IN_CAMPAIGN = """(exists (select 1 from acquisition_runs r
                      join acquisition_batch_items i on i.id = r.batch_item_id
                      join acquisition_batches b on b.id = i.batch_id
                     where r.id = p.acquisition_run_id and b.campaign_id = $1::uuid)
          or exists (select 1 from campaign_social_posts sp where sp.post_id = p.id and sp.campaign_id = $1::uuid))"""
_CAMPAIGN_POSTS = f"""from collected_posts p
     where {_IN_CAMPAIGN}"""


async def _set_actor(conn: asyncpg.Connection[asyncpg.Record], actor: str) -> None:
    await conn.execute("select set_config('app.actor', $1, true)", actor)


class _NotAttached(Exception):
    """Raised inside ``attach_to_cluster``'s transaction to roll it back: the finding is not a duplicate any more."""


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
        self.dead_groups: set[str] = set()   # canonical URLs with no new posts for days: skipped
        self.empty_groups: set[str] = set()  # canonical URLs read lately with nothing found: skipped
        self.live_groups: set[str] = set()   # canonical URLs with recent findings: read first
        self.unavailable: set[str] = set()
        self.normalised: dict[str, int] = {}
        self.progress: dict[str, tuple[int, int]] = {}  # cid -> (analysed or rejected posts, all collected posts)
        self.busy = False
        self.cancelled_batches: list[str] = []
        self.collector_running = False  # a batch run / acquisition run / launch holds the profile
        self.recovered: list[str] = []  # actors that freed the profile
        self.buckets: dict[str, tuple[str, float | None]] = {}  # finding -> (bucket, distance)
        self.hold_reasons: dict[str, str | None] = {}  # finding -> why it was held unverified
        self.whys: dict[str, str | None] = {}  # finding -> its reason category (held / excluded ones)
        self.final_reports: set[str] = set()  # campaigns whose final report was claimed
        self.held: dict[str, dict[str, None]] = {}  # cid -> held finding ids, in filing order
        self.desk = MemoryOfferDesk()  # the control plane's side of campaign_offers
        self.social: dict[str, SocialActivity] = {}  # cid -> social search activity
        self.relevances: dict[tuple[str, str], Relevance] = {}  # (cid, finding) -> stored AI verdict
        self.comment_reads: dict[str, dict[str, str]] = {}  # cid -> finding -> post URL (queued for a read)
        self.comment_done: set[str] = set()  # finding ids whose comments were read
        self.people: dict[str, list[Person]] = {}  # city -> stored people, in the order they are offered
        self.deliveries: dict[tuple[str, str], int | None] = {}  # (cid, person) -> message id (None = sending)
        self.contacts: dict[str, list[Contact]] = {}  # city -> relevant reach results, in the order they are offered
        self.reaching: set[str] = set()  # campaigns whose reach stage still runs
        self.reach: dict[str, ReachActivity] = {}  # cid -> the reach query running now
        self.summaries: set[str] = set()  # campaigns whose summary was claimed
        self.cluster_links: dict[str, list[dict[str, str]]] = {}  # head finding -> other sightings
        self.duplicates: dict[str, str] = {}  # duplicate finding -> head finding (stored, never sent)
        self.finding_reads: list[str] = []  # ids ``stream_finding`` loaded (dedup edits only)
        self.card_numbers: dict[str, int] = {}  # finding -> the number its card was sent with
        self.posts_read: dict[str, dict[tuple[str, str], tuple[int, int]]] = {}  # cid -> source -> (sources, posts)

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
                return Window(window_no, batch_id, batch.state, name, running if name else None)
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
        """Same rules as PostgreSQL: ``dead_groups`` (no new posts for days) and ``empty_groups`` (read lately,
        nothing found) are skipped, ``live_groups`` (recent findings) come first."""
        for g in self.groups.get(campaign_id, []):
            if g.state == "queued" and g.batch_id is None and g.canonical_url in self.dead_groups | self.empty_groups:
                g.state = "skipped"
        waiting = [g for g in self.groups.get(campaign_id, []) if g.state == "queued" and g.batch_id is None]
        waiting.sort(key=lambda g: (g.canonical_url not in self.live_groups, g.window_no is None, g.window_no or 0,
                                    -g.score, g.group_key))
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

    async def analysis_progress(self, campaign_id: str) -> tuple[int, int]:
        return self.progress.get(campaign_id, (0, 0))

    async def social_activity(self, campaign_id: str) -> SocialActivity:
        return self.social.get(campaign_id, SocialActivity())

    async def unstreamed_findings(self, campaign_id: str, limit: int,
                                  skip: Collection[str] = ()) -> list[StreamFinding]:
        done = self.streamed.get(campaign_id, {})
        return [f for f in self.findings.get(campaign_id, [])
                if f.id not in done and f.id not in self.buckets and f.id not in skip][:limit]

    async def claim_finding(self, campaign_id: str, finding_id: str) -> int | None:
        done = self.streamed.setdefault(campaign_id, {})
        if finding_id in done or finding_id in self.buckets:
            return None
        done[finding_id] = None
        self.buckets[finding_id] = ("exact", 0.0)
        self.card_numbers[finding_id] = len(done)
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

    async def source_counts(self, campaign_id: str) -> list[SourceCount]:
        """Findings by their link's site (facebook.com: Facebook); posts read come from ``posts_read``."""
        counts: dict[tuple[str, str], list[int]] = {
            key: [sources, posts, 0, 0, 0] for key, (sources, posts) in self.posts_read.get(campaign_id, {}).items()}
        done = self.streamed.get(campaign_id, {})
        held = self.held.get(campaign_id, {})
        for finding in self.findings.get(campaign_id, []):
            host = (urlsplit(finding.url or "").hostname or "").removeprefix("www.").removeprefix("m.")
            key = ("website", host) if host and not host.endswith("facebook.com") else ("facebook", "facebook")
            row = counts.setdefault(key, [1, 1, 0, 0, 0])
            row[2] += 1
            row[3] += done.get(finding.id) is not None
            row[4] += finding.id in held and self.buckets[finding.id][0] in ("similar", "other")
        return [SourceCount(platform, name, *row) for (platform, name), row in counts.items()]

    async def claim_summary(self, campaign_id: str) -> bool:
        if campaign_id in self.summaries:
            return False
        self.summaries.add(campaign_id)
        return True

    async def release_summary(self, campaign_id: str) -> None:
        self.summaries.discard(campaign_id)

    async def outcome_counts(self, campaign_id: str) -> list[OutcomeCount]:
        counts: dict[tuple[str, str, str | None], int] = {}
        done = self.streamed.get(campaign_id, {})
        for finding in self.findings.get(campaign_id, []):
            fid = finding.id
            bucket = self.buckets.get(fid, (None, None))[0]
            if fid in self.duplicates:
                key = ("duplicate", "exact", None)
            elif fid in done:
                key = ("sent" if done[fid] is not None else "sending", bucket or "exact", self.whys.get(fid))
            elif bucket is not None:
                key = ("held", bucket, self.whys.get(fid))
            else:
                continue
            counts[key] = counts.get(key, 0) + 1
        return [OutcomeCount(*key, n) for key, n in sorted(counts.items(), key=lambda kv: tuple(map(str, kv[0])))]

    async def claim_final_report(self, campaign_id: str) -> bool:
        if campaign_id in self.final_reports:
            return False
        self.final_reports.add(campaign_id)
        return True

    async def release_final_report(self, campaign_id: str) -> None:
        self.final_reports.discard(campaign_id)

    async def recent_sent_findings(self, campaign_id: str, limit: int = 200) -> list[SentFinding]:
        by_id = {f.id: f for f in self.findings.get(campaign_id, [])}
        sent = [fid for fid, m in self.streamed.get(campaign_id, {}).items()
                if m is not None and self.buckets.get(fid, ("", None))[0] == "exact" and fid in by_id]
        return [SentFinding(replace(by_id[fid], original=""), self.streamed[campaign_id][fid],
                            self.card_numbers.get(fid) or sent.index(fid) + 1,
                            fid if fid in self.cluster_links else None, tuple(self.cluster_links.get(fid, ())))
                for fid in reversed(sent)][:limit]

    async def stream_finding(self, finding_id: str) -> StreamFinding | None:
        self.finding_reads.append(finding_id)
        return next((f for items in self.findings.values() for f in items if f.id == finding_id), None)

    async def attach_to_cluster(self, finding_id: str, cluster_id: str, link: dict[str, str]) -> ClusterHead | None:
        message_id = next((done[cluster_id] for done in self.streamed.values() if done.get(cluster_id) is not None), None)
        if message_id is None or cluster_id in self.duplicates:
            return None
        if finding_id not in self.duplicates and (any(finding_id in done for done in self.streamed.values())
                                                  or any(finding_id in held for held in self.held.values())):
            return None  # sent or held meanwhile: no duplicate
        links = self.cluster_links.setdefault(cluster_id, [])
        if link.get("url") and all(x.get("url") != link["url"] for x in links):
            links.append({"url": str(link["url"]), "site": str(link.get("site") or "")})
        self.duplicates[finding_id] = cluster_id
        self.buckets[finding_id] = ("exact", 0.0)
        self.findings_state[finding_id] = "delivered"
        return ClusterHead(message_id, tuple(links))

    async def hold_finding(self, campaign_id: str, finding_id: str, bucket: str, distance: float | None,
                           reason: str | None = None, why: str | None = None) -> bool:
        if finding_id in self.buckets or finding_id in self.streamed.get(campaign_id, {}):
            return False
        self.buckets[finding_id] = (bucket, _distance(distance))
        self.hold_reasons[finding_id] = reason[:300] if reason else None
        self.whys[finding_id] = why
        self.held.setdefault(campaign_id, {})[finding_id] = None
        return True

    async def held_findings(self, campaign_id: str, bucket: str, limit: int) -> list[StreamFinding]:
        order = list(self.held.get(campaign_id, {}))
        by_id = {f.id: f for f in self.findings.get(campaign_id, [])}
        ids = [i for i in order if self.buckets[i][0] == bucket]
        ids.sort(key=lambda i: (self.buckets[i][1] is None, self.buckets[i][1] or 0.0, order.index(i)))
        return [replace(by_id[i], hold_reason=self.hold_reasons.get(i)) for i in ids[:limit]]

    async def claim_held(self, campaign_id: str, finding_id: str) -> int | None:
        held = self.held.get(campaign_id, {})
        if finding_id not in held:
            return None
        del held[finding_id]
        done = self.streamed.setdefault(campaign_id, {})
        done[finding_id] = None
        self.card_numbers[finding_id] = len(done)
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

    async def queue_comment_read(self, campaign_id: str, finding_id: str, post_url: str, max_posts: int) -> bool:
        reads = self.comment_reads.setdefault(campaign_id, {})
        if finding_id in reads or len(reads) >= max_posts:
            return False
        reads[finding_id] = post_url
        return True

    async def stored_people(self, campaign_id: str, location: str, days: int, limit: int) -> list[Person]:
        return [p for p in self.people.get(location, []) if (campaign_id, p.profile_key) not in self.deliveries][:limit]

    async def people_sent(self, campaign_id: str) -> int:
        return sum(1 for cid, _ in self.deliveries if cid == campaign_id)

    async def claim_person(self, campaign_id: str, profile_key: str) -> bool:
        if (campaign_id, profile_key) in self.deliveries:
            return False
        self.deliveries[(campaign_id, profile_key)] = None
        return True

    async def person_sent(self, campaign_id: str, profile_key: str, message_id: int) -> None:
        if (campaign_id, profile_key) in self.deliveries:
            self.deliveries[(campaign_id, profile_key)] = message_id

    async def release_person(self, campaign_id: str, profile_key: str) -> None:
        if self.deliveries.get((campaign_id, profile_key), 0) is None:
            del self.deliveries[(campaign_id, profile_key)]

    async def stored_contacts(self, campaign_id: str, location: str, days: int, limit: int) -> list[Contact]:
        return [c for c in self.contacts.get(location, [])
                if (campaign_id, c.delivery_key) not in self.deliveries][:limit]

    async def reach_pending(self, campaign_id: str) -> bool:
        return campaign_id in self.reaching

    async def reach_activity(self, campaign_id: str) -> ReachActivity:
        return self.reach.get(campaign_id, ReachActivity())
