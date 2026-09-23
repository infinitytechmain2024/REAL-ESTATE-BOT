"""PostgreSQL-only durable orchestration; no browser calls occur here."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncpg

from .models import ClaimedCommand, CommandReceipt, CommandState, ConfirmedCommand, RunRequest


class PostgresOrchestraStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
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
        row = await self._pool().fetchrow(
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
        result = await self._pool().execute(
            """update orchestration_commands
                 set state=case when attempt_count >= 10 then 'failed' else 'queued' end,
                     lease_expires_at=null,
                     error_code=case when attempt_count >= 10 then 'lease_retry_exhausted' else error_code end,
                     finished_at=case when attempt_count >= 10 then now() else finished_at end
                 where state='running' and lease_expires_at < now()"""
        )
        return int(result.rsplit(" ", 1)[-1])

    async def claim_next(self, *, lease_seconds: int) -> ClaimedCommand | None:
        async with self._pool().acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """select id, command, arguments, telegram_chat_id, telegram_user_id
                     from orchestration_commands where state='queued' and attempt_count < 10
                     order by created_at for update skip locked limit 1"""
            )
            if row is None:
                return None
            updated = await conn.fetchrow(
                """update orchestration_commands
                     set state='running', attempt_count=attempt_count+1, started_at=coalesce(started_at, now()),
                         lease_expires_at=now() + ($2 * interval '1 second')
                   where id=$1 returning id, command, arguments, telegram_chat_id, telegram_user_id""",
                row["id"], lease_seconds,
            )
            assert updated is not None
            return ClaimedCommand(str(updated["id"]), updated["command"], updated["arguments"], updated["telegram_chat_id"], updated["telegram_user_id"])

    async def complete(self, command_id: str, state: CommandState, result: dict[str, Any], *, error_code: str | None = None, error_detail: str | None = None) -> None:
        await self._pool().execute(
            """update orchestration_commands set state=$2, result=$3::jsonb, error_code=$4, error_detail=$5,
                   lease_expires_at=null, finished_at=case when $2 in ('finished','needs_verification','failed','cancelled') then now() else null end
               where id=$1 and state='running'""",
            command_id, state.value, json.dumps(result), error_code, error_detail,
        )

    async def _ready_profile(self, conn: asyncpg.Connection[asyncpg.Record], platform: str) -> str:
        profile = await conn.fetchval(
            """select id from browser_profiles where platform=$1 and state='ready' and deleted_at is null
                 order by created_at limit 1""", platform
        )
        if profile is None:
            raise ValueError(f"no ready {platform} browser profile is provisioned")
        return str(profile)

    async def _source(self, conn: asyncpg.Connection[asyncpg.Record], request: RunRequest, target: str) -> str:
        source_id = await conn.fetchval(
            """insert into monitoring_sources(platform, source_kind, vertical, canonical_url, acquisition_method, state)
               values($1,$2,$3,$4,$5,'active')
               on conflict(platform, canonical_url) do update set canonical_url=excluded.canonical_url
               returning id""",
            request.platform, request.source_kind, request.vertical, target, request.method.value,
        )
        return str(source_id)

    async def plan_run(self, request: RunRequest, *, actor: str) -> dict[str, Any]:
        """Persist bounded work. Worker launch is intentionally outside this transaction."""
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", actor)
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
                return {"status": "queued", "method": request.method.value, "batch_id": str(batch_id), "source_ids": source_ids, "max_groups": len(source_ids), "max_posts_per_group": 20, "browser_profile_id": profile_id}
            source_id = source_ids[0]
            run_id = await conn.fetchval(
                """insert into acquisition_runs(source_id, browser_profile_id, acquisition_method, state, max_pages, max_runtime_seconds, allowed_skills)
                   values($1,$2,'agent_ridge','queued',5,120,'[\"read_public_page\",\"extract_public_text\"]'::jsonb) returning id""",
                source_id, profile_id,
            )
            return {"status": "queued", "method": request.method.value, "run_id": str(run_id), "source_ids": source_ids, "max_pages": 5, "max_runtime_seconds": 120, "browser_profile_id": profile_id}

    async def apply_lifecycle(self, command: str, scope_kind: str, identifier: str) -> dict[str, Any]:
        """Use only legal transitions; active workers see cancellation via durable run state."""
        async with self._pool().acquire() as conn, conn.transaction():
            if scope_kind == "all":
                if command == "pause":
                    count = await conn.execute("update monitoring_sources set state='paused' where state='active'")
                elif command == "resume":
                    count = await conn.execute("update monitoring_sources set state='active' where state='paused'")
                elif command == "cancel":
                    count = await conn.execute("update acquisition_batches set state='cancelled' where state='queued'")
                    await conn.execute("update acquisition_runs set state='cancelled' where state='queued'")
                else:
                    raise ValueError("/run requires a run target, not a lifecycle scope")
                return {"status": command + "d", "scope": "all", "affected": int(count.rsplit(" ", 1)[-1])}
            table = {"source": "monitoring_sources", "batch": "acquisition_batches", "run": "acquisition_runs", "command": "orchestration_commands"}[scope_kind]
            if command == "pause" and scope_kind == "source":
                sql, target = f"update {table} set state='paused' where id=$1 and state='active'", "paused"
            elif command == "resume" and scope_kind == "source":
                sql, target = f"update {table} set state='active' where id=$1 and state='paused'", "resumed"
            elif command == "cancel" and scope_kind in {"batch", "run", "command"}:
                sql, target = f"update {table} set state='cancelled' where id=$1 and state in ('queued','running','paused')", "cancelled"
            else:
                raise ValueError("pause/resume apply to sources; cancel applies to batches, runs, commands, or all")
            result = await conn.execute(sql, identifier)
            return {"status": target, "scope": f"{scope_kind}:{identifier}", "affected": int(result.rsplit(" ", 1)[-1])}
