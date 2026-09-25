"""Long-running Agent Reach worker: runs /run-queued Agent Reach tasks one at a time."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .policy import PolicyViolation
from .runner import ControlledAgentReach
from .store import PostgresReachStore

log = logging.getLogger(__name__)


async def step(store: PostgresReachStore, runner: ControlledAgentReach) -> bool:
    """Run at most one queued task; False when nothing could start."""
    claimed = await store.claim_next()
    if claimed is None:
        return False
    try:
        result = await runner.run(claimed.task)
    except PolicyViolation as exc:
        log.warning("agent_reach.policy_violation %s", claimed.task.task_id)
        await store.fail(claimed, "policy_violation", str(exc))
        return True
    except Exception as exc:
        log.exception("agent_reach.failed %s", claimed.task.task_id)
        await store.fail(claimed, type(exc).__name__, str(exc))
        return True
    await store.complete(claimed, result)
    log.info("agent_reach.finished %s %s pages=%s", claimed.task.task_id, result.outcome.value, len(result.pages))
    return True


async def serve(store: PostgresReachStore, runner: ControlledAgentReach, *, poll_seconds: float,
                sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    while True:
        try:
            if await step(store, runner):
                continue
        except Exception:
            log.exception("agent_reach.step_failed")
        await sleep(poll_seconds)


async def main() -> None:
    import asyncpg

    from bot.facebook_collector.browser import BrowserSessionClient

    from .settings import AgentReachSettings

    settings = AgentReachSettings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is required for the Agent Reach worker")
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        runner = ControlledAgentReach(
            BrowserSessionClient(settings.browser_url, settings.browser_token), max_pages=settings.max_pages,
            max_execution_seconds=settings.max_execution_seconds, page_timeout_seconds=settings.page_timeout_seconds,
        )
        log.info("agent_reach.ready")
        await serve(PostgresReachStore(pool), runner, poll_seconds=settings.poll_seconds)
    finally:
        await pool.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
