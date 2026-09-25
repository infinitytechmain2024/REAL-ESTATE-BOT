"""PostgreSQL-only durable orchestration; no browser calls occur here."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

from .models import (
    ClaimedCommand,
    ClaimLost,
    CommandReceipt,
    CommandState,
    ConfirmedCommand,
    RunRequest,
)

DISPATCHER_ACTOR = "orchestra:dispatcher"


@dataclass(frozen=True)
class SafetyLimits:
    """Quotas and circuit breakers checked before /run creates any work.

    Counts come from the lifecycle tables themselves, so they cannot drift
    from what actually ran. A breaker closes on its own once the window passes
    (or, for failures, after the next success).
    """

    facebook_batches_per_day: int = 6
    facebook_groups_per_day: int = 60
    runs_per_day: int = 40  # per method, for single Agent Reach / Scrapling runs
    breaker_failures: int = 3
    breaker_challenges: int = 2
    breaker_window_hours: int = 6

    def __post_init__(self) -> None:
        bounds = {
            "facebook_batches_per_day": (1, 48), "facebook_groups_per_day": (1, 500), "runs_per_day": (1, 500),
            "breaker_failures": (1, 20), "breaker_challenges": (1, 20), "breaker_window_hours": (1, 72),
        }
        for name, (low, high) in bounds.items():
            if not low <= getattr(self, name) <= high:
                raise ValueError(f"unsafe safety limit {name}")


class PostgresOrchestraStore:
    def __init__(self, database_url: str, limits: SafetyLimits | None = None) -> None:
        self.database_url = database_url
        self.limits = limits or SafetyLimits()
        self.pool: asyncpg.Pool[asyncpg.Record] | None = None

    async def connect(self) -> None:
        import asyncpg

        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool[asyncpg.Record]:
        if self.pool is None:
            raise RuntimeError("PostgresOrchestraStore is not connected")
        return self.pool

    async def enqueue(self, item: ConfirmedCommand) -> CommandReceipt:
        key = f"telegram:{item.chat_id}:{item.message_id}:{item.command}"
        async with self._pool().acquire() as conn, conn.transaction():
            await _set_actor(conn, f"telegram:{item.user_id}")
            row = await conn.fetchrow(
                """insert into orchestration_commands
                       (confirmation_id, telegram_chat_id, telegram_user_id, telegram_message_id, command, arguments, idempotency_key)
                   values ($1::uuid,$2,$3,$4,$5,$6,$7)
                   on conflict (idempotency_key) do update set idempotency_key=excluded.idempotency_key
                   returning id, state, (xmax <> 0) as duplicate""",
                item.confirmation_id, item.chat_id, item.user_id, item.message_id, item.command, item.arguments, key,
            )
        assert row is not None
        return CommandReceipt(str(row["id"]), CommandState(row["state"]), bool(row["duplicate"]))

    async def reclaim_expired(self) -> int:
        async with self._pool().acquire() as conn, conn.transaction():
            await _set_actor(conn, DISPATCHER_ACTOR)
            result = await conn.execute(
                """update orchestration_commands
                     set state=case when attempt_count >= 10 then 'failed' else 'queued' end,
                         lease_expires_at=null,
                         error_code=case when attempt_count >= 10 then 'lease_retry_exhausted' else error_code end,
                         finished_at=case when attempt_count >= 10 then now() else finished_at end
                     where state='running' and lease_expires_at < now()"""
            )
        return _affected(result)

    async def claim_next(self, *, lease_seconds: int) -> ClaimedCommand | None:
        async with self._pool().acquire() as conn, conn.transaction():
            await _set_actor(conn, DISPATCHER_ACTOR)
            row = await conn.fetchrow(
                """select id from orchestration_commands where state='queued' and attempt_count < 10
                     order by created_at for update skip locked limit 1"""
            )
            if row is None:
                return None
            updated = await conn.fetchrow(
                """update orchestration_commands
                     set state='running', attempt_count=attempt_count+1, started_at=coalesce(started_at, now()),
                         lease_expires_at=now() + ($2 * interval '1 second')
                   where id=$1 returning id, command, arguments, telegram_chat_id, telegram_user_id, attempt_count""",
                row["id"], lease_seconds,
            )
            assert updated is not None
            return ClaimedCommand(
                str(updated["id"]), updated["command"], updated["arguments"],
                updated["telegram_chat_id"], updated["telegram_user_id"], updated["attempt_count"],
            )

    async def complete(self, item: ClaimedCommand, state: CommandState, result: dict[str, Any], *, error_code: str | None = None, error_detail: str | None = None) -> None:
        """Record a failure outcome; a no-op when the claim was cancelled or reclaimed."""
        async with self._pool().acquire() as conn, conn.transaction():
            await _set_actor(conn, f"telegram:{item.user_id}")
            await _finish(conn, item, state, result, error_code=error_code, error_detail=error_detail)

    async def _ready_profile(self, conn: asyncpg.Connection[asyncpg.Record], platform: str) -> str:
        profile = await conn.fetchval(
            """select id from browser_profiles where platform=$1 and state='ready' and deleted_at is null
                 order by created_at limit 1""", platform
        )
        if profile is None:
            raise ValueError(f"no ready {platform} browser profile is provisioned")
        return str(profile)

    async def _source(self, conn: asyncpg.Connection[asyncpg.Record], request: RunRequest, target: str) -> str:
        """Create an active source, or reuse an existing one only while it may run.

        Reusing a paused, disabled, retired, deleted, or verification-blocked
        source would let ``/run`` silently override ``/pause`` or a challenge.
        """
        source_id = await conn.fetchval(
            """insert into monitoring_sources(platform, source_kind, vertical, canonical_url, acquisition_method, state)
               values($1,$2,$3,$4,$5,'active')
               on conflict(platform, canonical_url) do nothing
               returning id""",
            request.platform, request.source_kind, request.vertical, target, request.method.value,
        )
        if source_id is not None:
            return str(source_id)
        existing = await conn.fetchrow(
            "select id, state, deleted_at from monitoring_sources where platform=$1 and canonical_url=$2 for update",
            request.platform, target,
        )
        assert existing is not None
        source_id, state = str(existing["id"]), existing["state"]
        if existing["deleted_at"] is not None:
            raise ValueError(f"source {source_id} is deleted and cannot be run")
        if state == "paused":
            raise ValueError(f"source {source_id} is paused; send /resume source:{source_id} first")
        if state == "human_verification_required":
            raise ValueError(f"source {source_id} is waiting for human verification")
        if state == "draft":
            await conn.execute("update monitoring_sources set state='active' where id=$1", source_id)
        elif state != "active":
            raise ValueError(f"source {source_id} is {state} and cannot be run")
        return source_id

    async def _check_safety(self, conn: asyncpg.Connection[asyncpg.Record], request: RunRequest) -> None:
        """Refuse work over a daily quota or while a breaker is open (raises ValueError)."""
        limits, method, platform = self.limits, request.method.value, request.platform
        # Serialise quota checks so two commands cannot both take the last slot.
        await conn.execute("select pg_advisory_xact_lock(hashtext('orchestra_safety_quota'))")
        window = limits.breaker_window_hours
        challenges = await conn.fetchval(
            """select count(*) from verification_jobs j join monitoring_sources s on s.id=j.source_id
                where s.platform=$1 and j.requested_at > now() - make_interval(hours => $2)""",
            platform, window,
        )
        if challenges >= limits.breaker_challenges:
            raise ValueError(
                f"safety breaker open: {challenges} {platform} challenges in the last {window} h; "
                "new work on this platform waits until the window passes"
            )
        if method == "facebook_connector":
            row = await conn.fetchrow(
                """select count(*) as batches, coalesce(sum(max_items), 0) as groups from acquisition_batches
                    where platform='facebook' and created_at > now() - interval '1 day'"""
            )
            if row["batches"] + 1 > limits.facebook_batches_per_day:
                raise ValueError(f"daily quota reached: {row['batches']} of {limits.facebook_batches_per_day} Facebook batches in 24 h")
            if row["groups"] + len(request.targets) > limits.facebook_groups_per_day:
                raise ValueError(
                    f"daily quota reached: {row['groups']} of {limits.facebook_groups_per_day} Facebook group reads in 24 h; "
                    f"this batch needs {len(request.targets)}"
                )
            failures = await conn.fetchval(
                """select count(*) from acquisition_batches
                    where platform='facebook' and state='failed' and finished_at > now() - make_interval(hours => $1)
                      and finished_at > coalesce((select max(finished_at) from acquisition_batches
                                                   where platform='facebook' and state='succeeded'), '-infinity')""",
                window,
            )
        else:
            runs = await conn.fetchval(
                """select count(*) from acquisition_runs where acquisition_method=$1 and batch_item_id is null
                    and created_at > now() - interval '1 day'""",
                method,
            )
            if runs + 1 > limits.runs_per_day:
                raise ValueError(f"daily quota reached: {runs} of {limits.runs_per_day} {method} runs in 24 h")
            failures = await conn.fetchval(
                """select count(*) from acquisition_runs
                    where acquisition_method=$1 and batch_item_id is null and state='failed'
                      and finished_at > now() - make_interval(hours => $2)
                      and finished_at > coalesce((select max(finished_at) from acquisition_runs
                                                   where acquisition_method=$1 and batch_item_id is null and state='succeeded'), '-infinity')""",
                method, window,
            )
        if failures >= limits.breaker_failures:
            raise ValueError(
                f"safety breaker open: {failures} failed {method} runs in a row in the last {window} h; "
                "check the worker logs, it closes after the window or the next success"
            )

    async def plan_run(self, request: RunRequest, item: ClaimedCommand, *, actor: str) -> dict[str, Any]:
        """Persist bounded work and finish the command in one transaction.

        A crash before commit leaves nothing behind, so a reclaimed retry cannot
        create a duplicate batch or run. Worker launch stays outside this path.
        """
        async with self._pool().acquire() as conn, conn.transaction():
            await _set_actor(conn, actor)
            await self._check_safety(conn, request)
            profile_id = None
            if request.method.value != "scrapling":
                profile_id = await self._ready_profile(conn, request.platform)
            source_ids = [await self._source(conn, request, target) for target in request.targets]
            if request.method.value == "facebook_connector":
                batch_id = await conn.fetchval(
                    """insert into acquisition_batches(platform, acquisition_method, vertical, state, max_items, requested_by)
                       values ('facebook','facebook_connector',$1,'planned',$2,$3) returning id""",
                    request.vertical, len(source_ids), actor,
                )
                for sequence_no, source_id in enumerate(source_ids, 1):
                    await conn.execute(
                        "insert into acquisition_batch_items(batch_id, source_id, sequence_no) values($1,$2,$3)",
                        batch_id, source_id, sequence_no,
                    )
                await conn.execute("update acquisition_batches set state='queued' where id=$1", batch_id)
                # facebook-runner starts it (migration 012); the requester hears the outcome.
                await conn.execute(
                    """insert into collector_launch_requests(batch_id, requested_by, notify_telegram_id)
                       values ($1, $2, $3) on conflict do nothing""",
                    batch_id, actor, item.user_id,
                )
                result = {"status": "queued", "method": request.method.value, "batch_id": str(batch_id), "source_ids": source_ids, "max_groups": len(source_ids), "max_posts_per_group": 20, "browser_profile_id": profile_id}
            elif request.method.value == "agent_ridge":
                run_id = await conn.fetchval(
                    """insert into acquisition_runs(source_id, browser_profile_id, acquisition_method, state, max_pages, max_runtime_seconds, allowed_skills)
                       values($1,$2,'agent_ridge','queued',5,120,'[\"read_public_page\",\"extract_public_text\"]'::jsonb) returning id""",
                    source_ids[0], profile_id,
                )
                result = {"status": "queued", "method": request.method.value, "run_id": str(run_id), "source_ids": source_ids, "max_pages": 5, "max_runtime_seconds": 120, "browser_profile_id": profile_id}
            else:
                run_id = await conn.fetchval(
                    """insert into acquisition_runs(source_id, acquisition_method, state, max_pages, max_runtime_seconds, allowed_skills)
                       values($1,'scrapling','queued',1,45,'[\"http_get\",\"scrapling_parse\"]'::jsonb) returning id""",
                    source_ids[0],
                )
                result = {"status": "queued", "method": request.method.value, "run_id": str(run_id), "source_ids": source_ids, "max_pages": 1, "max_runtime_seconds": 45, "browser_profile_id": None}
            await _finish(conn, item, CommandState.FINISHED, result)
            return result

    async def apply_lifecycle(self, item: ClaimedCommand, scope_kind: str, identifier: str, *, actor: str) -> dict[str, Any]:
        """Use only legal transitions and finish the command in the same transaction.

        Running work is never forced into a terminal state here: the Facebook
        collector sees a cancelled batch before its next group and closes its own
        running item, batch run, and browser profile.
        """
        command = item.command
        async with self._pool().acquire() as conn, conn.transaction():
            await _set_actor(conn, actor)
            if scope_kind == "all":
                if command == "pause":
                    affected = _affected(await conn.execute("update monitoring_sources set state='paused' where state='active'"))
                elif command == "resume":
                    affected = _affected(await conn.execute("update monitoring_sources set state='active' where state='paused'"))
                elif command == "cancel":
                    affected = await _cancel_batches(conn, "state in ('planned','queued','running','human_verification_required')")
                    affected += _affected(await conn.execute("update acquisition_runs set state='cancelled' where state in ('queued','awaiting_human_verification') and batch_item_id is null"))
                    affected += _affected(await conn.execute("update orchestration_commands set state='cancelled', finished_at=now() where state='queued'"))
                else:
                    raise ValueError("/run requires a run target, not a lifecycle scope")
                result = {"status": {"pause": "paused", "resume": "resumed", "cancel": "cancelled"}[command], "scope": "all", "affected": affected}
            else:
                if command == "pause" and scope_kind == "source":
                    sql, status = "update monitoring_sources set state='paused' where id=$1 and state='active'", "paused"
                elif command == "resume" and scope_kind == "source":
                    sql, status = "update monitoring_sources set state='active' where id=$1 and state='paused'", "resumed"
                elif command == "cancel" and scope_kind == "batch":
                    sql, status = "", "cancelled"
                elif command == "cancel" and scope_kind == "run":
                    # Running Facebook item runs are closed by the collector; cancel its batch instead.
                    sql, status = "update acquisition_runs set state='cancelled' where id=$1 and state in ('queued','awaiting_human_verification')", "cancelled"
                elif command == "cancel" and scope_kind == "command":
                    sql, status = "update orchestration_commands set state='cancelled', finished_at=now() where id=$1 and state in ('queued','running','paused')", "cancelled"
                else:
                    raise ValueError("pause/resume apply to sources; cancel applies to batches, runs, commands, or all")
                if scope_kind == "batch":
                    affected = await _cancel_batches(conn, "id=$1 and state in ('planned','queued','running','human_verification_required')", identifier)
                else:
                    affected = _affected(await conn.execute(sql, identifier))
                result = {"status": status if affected else "unchanged", "scope": f"{scope_kind}:{identifier}", "affected": affected}
            await _finish(conn, item, CommandState.FINISHED, result)
            return result


    async def reap_stale_batches(self, *, stale_seconds: int) -> list[str]:
        """Fail batches whose collector stopped reporting, and free their profile.

        A live collector touches its batch run or items at least once per
        group (at most ~2 minutes apart), so silence for ``stale_seconds`` means
        the worker died. A challenge is always recorded before the collector
        exits, so a profile still ``in_use`` here was not challenged.
        """
        async with self._pool().acquire() as conn, conn.transaction():
            await _set_actor(conn, DISPATCHER_ACTOR)
            stale = await conn.fetch(
                """select r.id, r.batch_id, r.browser_profile_id
                     from batch_runs r
                    where r.state = 'running'
                      and greatest(
                            r.updated_at,
                            coalesce((select max(i.updated_at) from acquisition_batch_items i where i.batch_id = r.batch_id), r.updated_at),
                            coalesce((select max(a.updated_at) from acquisition_runs a where a.batch_run_id = r.id), r.updated_at)
                          ) < now() - ($1 * interval '1 second')
                    for update of r skip locked""",
                stale_seconds,
            )
            for row in stale:
                await conn.execute(
                    "update acquisition_runs set state='failed', finished_at=now(), error_code='collector_lost' where batch_run_id=$1 and state='running'",
                    row["id"],
                )
                await conn.execute(
                    "update acquisition_batch_items set state='failed', finished_at=now(), last_error_code='collector_lost' where batch_id=$1 and state='running'",
                    row["batch_id"],
                )
                await conn.execute(
                    "update acquisition_batch_items set state='skipped', last_error_code='collector_lost' where batch_id=$1 and state='queued'",
                    row["batch_id"],
                )
                await conn.execute(
                    "update batch_runs set state='failed', finished_at=now(), stop_reason='collector_lost' where id=$1 and state='running'",
                    row["id"],
                )
                await conn.execute(
                    "update acquisition_batches set state='failed', finished_at=now() where id=$1 and state='running'",
                    row["batch_id"],
                )
                if row["browser_profile_id"] is not None:
                    await conn.execute(
                        """update browser_profiles set state='ready' where id=$1 and state='in_use'
                             and not exists (select 1 from batch_runs other where other.browser_profile_id=$1 and other.state='running')""",
                        row["browser_profile_id"],
                    )
            # A single Agent Reach or Scrapling run whose worker died: fail it and free its profile.
            lost = await conn.fetch(
                """update acquisition_runs set state='failed', finished_at=now(), error_code='worker_lost'
                    where state='running' and batch_item_id is null and acquisition_method in ('agent_ridge','scrapling')
                      and updated_at < now() - ($1 * interval '1 second')
                returning browser_profile_id""",
                stale_seconds,
            )
            for row in lost:
                if row["browser_profile_id"] is not None:
                    await conn.execute(
                        """update browser_profiles set state='ready' where id=$1 and state='in_use'
                             and not exists (select 1 from batch_runs b where b.browser_profile_id=$1 and b.state='running')
                             and not exists (select 1 from acquisition_runs a where a.browser_profile_id=$1 and a.state='running')""",
                        row["browser_profile_id"],
                    )
            return [str(row["batch_id"]) for row in stale]


async def _set_actor(conn: asyncpg.Connection[asyncpg.Record], actor: str) -> None:
    """Attribute every audit row written by this transaction."""
    await conn.execute("select set_config('app.actor', $1, true)", actor)


def _affected(status: str) -> int:
    return int(status.rsplit(" ", 1)[-1])


async def _finish(conn: asyncpg.Connection[asyncpg.Record], item: ClaimedCommand, state: CommandState, result: dict[str, Any], *, error_code: str | None = None, error_detail: str | None = None) -> None:
    """Finish only the exact claim this worker holds, else roll back its work."""
    status = await conn.execute(
        """update orchestration_commands set state=$3, result=$4::jsonb, error_code=$5, error_detail=$6,
               lease_expires_at=null, finished_at=case when $3 in ('finished','needs_verification','failed','cancelled') then now() else null end
           where id=$1 and attempt_count=$2 and state='running'""",
        item.id, item.attempt, state.value, json.dumps(result), error_code, error_detail,
    )
    if _affected(status) != 1:
        raise ClaimLost(item.id)


async def _cancel_batches(conn: asyncpg.Connection[asyncpg.Record], condition: str, *args: object) -> int:
    """Cancel batches plus their not-yet-running items and verification holds."""
    batch_ids = [row["id"] for row in await conn.fetch(
        f"update acquisition_batches set state='cancelled' where {condition} returning id", *args
    )]
    if not batch_ids:
        return 0
    await conn.execute(
        "update acquisition_batch_items set state='cancelled' where batch_id = any($1::uuid[]) and state in ('queued','awaiting_human_verification')",
        batch_ids,
    )
    await conn.execute(
        """update acquisition_runs set state='cancelled' where state='awaiting_human_verification'
             and batch_item_id in (select id from acquisition_batch_items where batch_id = any($1::uuid[]))""",
        batch_ids,
    )
    await conn.execute(
        "update batch_runs set state='cancelled' where batch_id = any($1::uuid[]) and state in ('queued','human_verification_required')",
        batch_ids,
    )
    return len(batch_ids)
