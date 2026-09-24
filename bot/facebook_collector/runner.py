"""Long-running launcher for batches resumed after human verification.

It runs only batches that have a ``collector_launch_requests`` row, which the
verification service writes in the same transaction that requeues the batch.
Other queued batches stay manual. One batch at a time, through the same
one-shot collector code, so every limit of the collector still applies.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Launch:
    id: str
    batch_id: str


class LaunchStore(Protocol):
    async def recover_interrupted(self) -> int: ...
    async def claim(self) -> Launch | None: ...
    async def batch_state(self, batch_id: str) -> str | None: ...
    async def finish(self, launch_id: str, state: str, result: str | None = None, error: str | None = None) -> None: ...


class PostgresLaunchStore:
    def __init__(self, pool: Any) -> None:
        self.pool = pool

    async def recover_interrupted(self) -> int:
        # A launch left running belongs to a runner that stopped mid-batch; the
        # dispatcher's stale-batch rule already closes the batch itself.
        result = await self.pool.execute(
            """update public.collector_launch_requests set state = 'failed', finished_at = now(), error = 'runner_restarted'
                where state = 'running'""")
        return int(result.split()[-1])

    async def claim(self) -> Launch | None:
        row = await self.pool.fetchrow(
            """update public.collector_launch_requests set state = 'running', started_at = now()
                where id = (select id from public.collector_launch_requests where state = 'pending'
                             order by requested_at for update skip locked limit 1)
               returning id::text as id, batch_id::text as batch_id""")
        return Launch(row["id"], row["batch_id"]) if row else None

    async def batch_state(self, batch_id: str) -> str | None:
        state: str | None = await self.pool.fetchval("select state from public.acquisition_batches where id = $1::uuid", batch_id)
        return state

    async def finish(self, launch_id: str, state: str, result: str | None = None, error: str | None = None) -> None:
        await self.pool.execute(
            """update public.collector_launch_requests set state = $2, result = $3, error = $4, finished_at = now()
                where id = $1::uuid and state = 'running'""", launch_id, state, result, error)


class CollectorRunner:
    def __init__(self, store: LaunchStore, run: Callable[[str], Awaitable[str]], *, poll_seconds: float = 15,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self.store, self.run, self.poll_seconds, self.sleep = store, run, poll_seconds, sleep

    async def step(self) -> bool:
        """Run at most one requested batch; False when nothing was waiting."""
        launch = await self.store.claim()
        if launch is None:
            return False
        state = await self.store.batch_state(launch.batch_id)
        if state != "queued":
            # Cancelled, failed or already started by hand in the meantime.
            await self.store.finish(launch.id, "skipped", error=f"batch is {state or 'missing'}")
            log.info("facebook_runner.skipped %s %s", launch.batch_id, state)
            return True
        log.info("facebook_runner.started %s", launch.batch_id)
        try:
            result = await self.run(launch.batch_id)
        except Exception as exc:  # recorded and reported; the runner keeps going
            log.exception("facebook_runner.failed %s", launch.batch_id)
            await self.store.finish(launch.id, "failed", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return True
        await self.store.finish(launch.id, "finished", result=result)
        log.info("facebook_runner.finished %s %s", launch.batch_id, result)
        return True

    async def serve(self) -> None:
        recovered = await self.store.recover_interrupted()
        if recovered:
            log.warning("facebook_runner.recovered_interrupted %s", recovered)
        while True:
            try:
                if await self.step():
                    continue
            except Exception:
                log.exception("facebook_runner.step_failed")
            await self.sleep(self.poll_seconds)


async def main() -> None:
    import asyncpg

    from .main import run_batch_with
    from .settings import FacebookCollectorSettings

    settings = FacebookCollectorSettings()
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=3)
    try:
        runner = CollectorRunner(PostgresLaunchStore(pool), lambda batch_id: run_batch_with(pool, settings, batch_id),
                                 poll_seconds=settings.runner_poll_seconds)
        log.info("facebook_runner.ready")
        await runner.serve()
    finally:
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
