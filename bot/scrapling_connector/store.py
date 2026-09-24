"""Durable PostgreSQL lifecycle operations for the one-shot website worker."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .models import ScraplingOutcome, ScraplingResult, ScraplingTask

if TYPE_CHECKING:
    import asyncpg


@dataclass(frozen=True)
class ClaimedScraplingRun:
    task: ScraplingTask


class PostgresScraplingStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: asyncpg.Pool[asyncpg.Record] | None = None

    async def connect(self) -> None:
        import asyncpg

        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=2)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    def _pool(self) -> asyncpg.Pool[asyncpg.Record]:
        if self.pool is None:
            raise RuntimeError("PostgresScraplingStore is not connected")
        return self.pool

    async def claim(self, run_id: str) -> ClaimedScraplingRun | None:
        """Claim only a queued Scrapling run; a second worker receives None."""
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', 'scrapling_connector', true)")
            row = await conn.fetchrow(
                """select r.id, r.source_id, s.canonical_url, r.max_pages, r.max_runtime_seconds
                     from acquisition_runs r
                     join monitoring_sources s on s.id=r.source_id
                    where r.id=$1::uuid and r.acquisition_method='scrapling'
                      and r.state='queued' and s.state='active' and s.deleted_at is null
                    for update of r skip locked""",
                run_id,
            )
            if row is None:
                return None
            changed = await conn.execute(
                """update acquisition_runs set state='running', started_at=now()
                     where id=$1 and state='queued'""",
                row["id"],
            )
            if not changed.endswith(" 1"):
                return None
            return ClaimedScraplingRun(
                ScraplingTask(
                    str(row["id"]), str(row["source_id"]), row["canonical_url"],
                    row["max_runtime_seconds"], row["max_pages"],
                )
            )

    async def complete(self, claimed: ClaimedScraplingRun, result: ScraplingResult) -> None:
        """Persist normalized evidence, then close the exact running attempt."""
        task = claimed.task
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', 'scrapling_connector', true)")
            if result.outcome is ScraplingOutcome.COMPLETED and result.page is not None:
                page = result.page
                platform_post_id = hashlib.sha256(page.canonical_url.encode()).hexdigest()
                content_hash = hashlib.sha256(page.text.encode()).hexdigest()
                await conn.execute(
                    """insert into collected_posts
                           (source_id, acquisition_run_id, platform_post_id, canonical_url,
                            body_text, raw_payload, content_hash, state)
                       values ($1::uuid,$2::uuid,$3,$4,$5,$6::jsonb,$7,'normalised')
                       on conflict (source_id, platform_post_id) do nothing""",
                    task.source_id, task.run_id, platform_post_id, page.canonical_url, page.text,
                    json.dumps({"title": page.title, "platform": page.platform, "source_type": page.source_type}),
                    content_hash,
                )
                await conn.execute(
                    """update acquisition_runs set state='succeeded', finished_at=now(),
                           error_code=null, error_detail=null where id=$1::uuid and state='running'""",
                    task.run_id,
                )
                await conn.execute(
                    "update monitoring_sources set last_success_at=now() where id=$1::uuid",
                    task.source_id,
                )
                return
            state = "stopped" if result.outcome is ScraplingOutcome.STOPPED_LIMIT else "failed"
            await conn.execute(
                """update acquisition_runs set state=$2, finished_at=now(), error_code=$3,
                       error_detail=$4 where id=$1::uuid and state='running'""",
                task.run_id, state, result.outcome.value, (result.reason or "unknown")[:500],
            )
            await conn.execute(
                "update monitoring_sources set last_failure_at=now() where id=$1::uuid",
                task.source_id,
            )

    async def health(self) -> bool:
        try:
            async with self._pool().acquire() as conn:
                return bool(await conn.fetchval("select true"))
        except Exception:  # noqa: BLE001 - health probes are boolean only.
            return False
