"""The bounded Main Orchestra command dispatcher."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any

from .models import ClaimedCommand, ClaimLost, CommandReceipt, CommandState, ConfirmedCommand
from .parser import CommandValidationError, parse_run, parse_scope
from .store import PostgresOrchestraStore

log = logging.getLogger(__name__)
Notifier = Callable[[int, str], Awaitable[None]]
MAX_BACKOFF_SECONDS = 30.0


class OrchestraDispatcher:
    def __init__(self, store: PostgresOrchestraStore, *, lease_seconds: int = 30, poll_seconds: float = 1.0, notifier: Notifier | None = None) -> None:
        if not 10 <= lease_seconds <= 300 or not 0.1 <= poll_seconds <= 30:
            raise ValueError("unsafe dispatcher settings")
        self.store, self.lease_seconds, self.poll_seconds = store, lease_seconds, poll_seconds
        self.notifier = notifier
        self._stop = asyncio.Event()

    async def enqueue(self, command: ConfirmedCommand) -> CommandReceipt:
        return await self.store.enqueue(command)

    async def process_once(self) -> bool:
        await self.store.reclaim_expired()
        item = await self.store.claim_next(lease_seconds=self.lease_seconds)
        if item is None:
            return False
        await self._notify(item.chat_id, f"Orchestra: processing {item.command} request {item.id}.")
        try:
            result = await self._dispatch(item)
            await self._notify(item.chat_id, f"Orchestra: {item.command} request {item.id} {_summary(result)}.")
        except ClaimLost:
            log.warning("orchestra.claim_lost", extra={"command_id": item.id})
            await self._notify(item.chat_id, f"Orchestra: request {item.id} was cancelled before it completed; nothing was planned.")
        except CommandValidationError as exc:
            await self._fail(item, "invalid_command", str(exc))
            await self._notify(item.chat_id, f"Orchestra: request {item.id} failed validation: {exc}")
        except ValueError as exc:
            await self._fail(item, "precondition_failed", str(exc))
            await self._notify(item.chat_id, f"Orchestra: request {item.id} cannot run: {exc}")
        except Exception as exc:
            log.exception("orchestra.dispatch_failed", extra={"command_id": item.id})
            await self._fail(item, type(exc).__name__, str(exc)[:500])
            await self._notify(item.chat_id, f"Orchestra: request {item.id} failed safely; inspect its audit record.")
        return True

    async def _dispatch(self, item: ClaimedCommand) -> dict[str, Any]:
        """Plan or apply the command; the store finishes it in the same transaction."""
        actor = f"telegram:{item.user_id}"
        if item.command == "run":
            return await self.store.plan_run(parse_run(item.arguments), item, actor=actor)
        scope_kind, identifier = parse_scope(item.arguments)
        return await self.store.apply_lifecycle(item, scope_kind, identifier, actor=actor)

    async def _fail(self, item: ClaimedCommand, error_code: str, error_detail: str) -> None:
        with suppress(ClaimLost):
            await self.store.complete(item, CommandState.FAILED, {"status": "failed"}, error_code=error_code, error_detail=error_detail)

    async def run_forever(self) -> None:
        """Keep draining the inbox; a database outage delays work but never ends the loop."""
        failures = 0
        while not self._stop.is_set():
            try:
                worked = await self.process_once()
                failures, delay = 0, self.poll_seconds
            except Exception:
                failures += 1
                worked, delay = False, min(MAX_BACKOFF_SECONDS, self.poll_seconds * 2 ** min(failures, 6))
                log.exception("orchestra.loop_failed", extra={"consecutive_failures": failures, "retry_in_seconds": delay})
            if not worked:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)

    def stop(self) -> None:
        self._stop.set()

    async def _notify(self, chat_id: int, text: str) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier(chat_id, text)
        except Exception:  # noqa: BLE001 - Telegram delivery must not undo durable orchestration.
            log.warning("orchestra.status_notification_failed", extra={"chat_id": chat_id})


def _summary(result: dict[str, Any]) -> str:
    if result.get("batch_id"):
        return f"queued Facebook batch {result['batch_id']} ({result.get('max_groups')} groups); start it with the collector"
    if result.get("run_id"):
        return f"queued run {result['run_id']}"
    return f"{result.get('status', 'finished')} ({result.get('affected', 0)} affected)"
