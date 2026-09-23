"""The bounded Main Orchestra command dispatcher."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from .models import ClaimedCommand, CommandReceipt, CommandState, ConfirmedCommand
from .parser import CommandValidationError, parse_run, parse_scope
from .store import PostgresOrchestraStore

log = logging.getLogger(__name__)
Notifier = Callable[[int, str], Awaitable[None]]


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
            await self._dispatch(item)
            await self._notify(item.chat_id, f"Orchestra: finished {item.command}; request {item.id} is recorded with its bounded plan/status.")
        except CommandValidationError as exc:
            await self.store.complete(item.id, CommandState.FAILED, {"status": "failed"}, error_code="invalid_command", error_detail=str(exc))
            await self._notify(item.chat_id, f"Orchestra: request {item.id} failed validation: {exc}")
        except ValueError as exc:
            await self.store.complete(item.id, CommandState.FAILED, {"status": "failed"}, error_code="precondition_failed", error_detail=str(exc))
            await self._notify(item.chat_id, f"Orchestra: request {item.id} cannot run: {exc}")
        except Exception as exc:
            log.exception("orchestra.dispatch_failed", extra={"command_id": item.id})
            await self.store.complete(item.id, CommandState.FAILED, {"status": "failed"}, error_code=type(exc).__name__, error_detail=str(exc)[:500])
            await self._notify(item.chat_id, f"Orchestra: request {item.id} failed safely; inspect its audit record.")
        return True

    async def _dispatch(self, item: ClaimedCommand) -> None:
        actor = f"telegram:{item.user_id}"
        if item.command == "run":
            result = await self.store.plan_run(parse_run(item.arguments), actor=actor)
        else:
            scope_kind, identifier = parse_scope(item.arguments)
            result = await self.store.apply_lifecycle(item.command, scope_kind, identifier)
        await self.store.complete(item.id, CommandState.FINISHED, result)

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            worked = await self.process_once()
            if not worked:
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    async def _notify(self, chat_id: int, text: str) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier(chat_id, text)
        except Exception:  # noqa: BLE001 - Telegram delivery must not undo durable orchestration.
            log.warning("orchestra.status_notification_failed", extra={"chat_id": chat_id})
