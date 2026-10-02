"""Durable PostgreSQL lifecycle for Agent Reach runs queued by the Orchestra.

The worker takes tasks only from ``acquisition_runs`` rows that ``/run``
created (method ``agent_ridge``), never from free-form input, and stores what
it read as normalised evidence for the analysis worker. A challenge stops the
run and opens a verification job exactly as the Facebook collector does.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .models import ReachOutcome, ReachPlatform, ReachResult, ReachTask

ACTOR = "agent_reach"
SUPPORTED_PLATFORMS = [platform.value for platform in ReachPlatform]


@dataclass(frozen=True)
class ClaimedReachRun:
    task: ReachTask
    source_id: str


class PostgresReachStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def claim_next(self) -> ClaimedReachRun | None:
        """Claim the oldest queued run whose browser profile is free; None when none can start."""
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", ACTOR)
            # A run for a platform this adapter cannot read (LinkedIn, ...) is cancelled at once with a clear code;
            # left queued it would be the oldest row forever and hold every run behind it.
            await conn.execute(
                """update acquisition_runs r set state = 'cancelled', finished_at = now(),
                          error_code = 'unsupported_platform', error_detail = s.platform
                     from monitoring_sources s
                    where s.id = r.source_id and r.acquisition_method = 'agent_ridge' and r.state = 'queued'
                      and r.batch_item_id is null and not (s.platform = any($1::text[]))""",
                SUPPORTED_PLATFORMS)
            row = await conn.fetchrow(
                """select r.id, r.source_id, r.max_pages, s.canonical_url, s.platform,
                          p.id as profile_id, p.profile_name, p.state as profile_state
                     from acquisition_runs r
                     join monitoring_sources s on s.id = r.source_id
                     join browser_profiles p on p.id = r.browser_profile_id
                    where r.acquisition_method = 'agent_ridge' and r.state = 'queued' and r.batch_item_id is null
                      and s.state = 'active' and s.deleted_at is null and s.platform = any($1::text[])
                      and p.state = 'ready' and p.deleted_at is null
                    order by r.created_at
                    for update of r, p skip locked limit 1""", SUPPORTED_PLATFORMS)
            if row is None:
                return None
            await conn.execute("update acquisition_runs set state='running', started_at=now() where id=$1 and state='queued'", row["id"])
            await conn.execute("update browser_profiles set state='in_use', last_used_at=now() where id=$1 and state='ready'", row["profile_id"])
            return ClaimedReachRun(
                ReachTask(
                    task_id=str(row["id"]), platform=ReachPlatform(row["platform"]), targets=(row["canonical_url"],),
                    browser_profile_id=str(row["profile_id"]), browser_profile_name=row["profile_name"],
                    browser_profile_state="ready",
                ),
                str(row["source_id"]),
            )

    async def complete(self, claimed: ClaimedReachRun, result: ReachResult) -> None:
        task = claimed.task
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", ACTOR)
            for page in result.pages:
                if not page.text.strip():
                    continue
                await conn.execute(
                    """insert into collected_posts(source_id, acquisition_run_id, platform_post_id, canonical_url,
                                                   body_text, raw_payload, content_hash, state)
                       values ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, $7, 'normalised')
                       on conflict do nothing""",
                    claimed.source_id, task.task_id, hashlib.sha256(page.canonical_url.encode()).hexdigest(),
                    page.canonical_url, page.text,
                    json.dumps({"title": page.title, "platform": page.platform, "source_type": page.source_type}),
                    hashlib.sha256(page.text.encode()).hexdigest(),
                )
            if result.outcome is ReachOutcome.STOPPED_CHALLENGE:
                reason = result.stop_reason or "challenge"
                await conn.execute(
                    "update acquisition_runs set state='awaiting_human_verification', stop_reason=$2 where id=$1::uuid and state='running'",
                    task.task_id, reason)
                await conn.execute(
                    "update monitoring_sources set state='human_verification_required' where id=$1::uuid and state='active'",
                    claimed.source_id)
                await conn.execute(
                    "update browser_profiles set state='human_verification_required' where id=$1::uuid and state='in_use'",
                    task.browser_profile_id)
                await conn.execute(
                    """insert into verification_jobs(source_id, acquisition_run_id, job_type, state, requested_by, resolution_note)
                       values ($1::uuid, $2::uuid, $3, 'requested', 'agent_reach', $4)
                       on conflict (source_id, job_type) where state in ('requested','active') do nothing""",
                    claimed.source_id, task.task_id,
                    "facebook_challenge" if task.platform is ReachPlatform.FACEBOOK else "login", reason)
                return
            state, code = {
                ReachOutcome.COMPLETED: ("succeeded", None),
                ReachOutcome.STOPPED_LIMIT: ("stopped", "limit"),
                ReachOutcome.FAILED: ("failed", "reach_failed"),
            }[result.outcome]
            await self._close(conn, claimed, state, code, result.stop_reason)

    async def fail(self, claimed: ClaimedReachRun, code: str, detail: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", ACTOR)
            await self._close(conn, claimed, "failed", code, detail)

    async def _close(self, conn: Any, claimed: ClaimedReachRun, state: str, code: str | None, detail: str | None) -> None:
        await conn.execute(
            """update acquisition_runs set state=$2, finished_at=now(), error_code=$3, error_detail=$4
                where id=$1::uuid and state='running'""",
            claimed.task.task_id, state, code, (detail or None) and detail[:500])
        column = "last_success_at" if state == "succeeded" else "last_failure_at"
        await conn.execute(f"update monitoring_sources set {column}=now() where id=$1::uuid", claimed.source_id)
        await conn.execute("update browser_profiles set state='ready' where id=$1::uuid and state='in_use'", claimed.task.browser_profile_id)
