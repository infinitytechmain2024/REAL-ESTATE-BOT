"""Decision and safety tests for the bounded Main Orchestra dispatcher."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from bot.orchestra.dispatcher import OrchestraDispatcher
from bot.orchestra.models import (
    ClaimedCommand,
    ClaimLost,
    CommandReceipt,
    CommandState,
    ConfirmedCommand,
)
from bot.orchestra.parser import CommandValidationError, parse_run


@dataclass
class FakeStore:
    items: list[ClaimedCommand]

    def __post_init__(self) -> None:
        self.completed: list[tuple[str, CommandState, dict[str, object], str | None]] = []
        self.requests: list[object] = []
        self.lifecycle: list[tuple[str, str, str]] = []
        self.reclaimed = 0

    async def enqueue(self, command: ConfirmedCommand) -> CommandReceipt:
        return CommandReceipt("command-1", CommandState.QUEUED)

    async def reclaim_expired(self) -> int:
        self.reclaimed += 1
        return 0

    async def claim_next(self, *, lease_seconds: int) -> ClaimedCommand | None:
        return self.items.pop(0) if self.items else None

    async def plan_run(self, request: object, item: ClaimedCommand, *, actor: str) -> dict[str, object]:
        self.requests.append((request, actor))
        result = {"status": "queued", "method": request.method.value}  # type: ignore[attr-defined]
        self.completed.append((item.id, CommandState.FINISHED, result, None))
        return result

    async def apply_lifecycle(self, item: ClaimedCommand, scope_kind: str, identifier: str, *, actor: str) -> dict[str, object]:
        self.lifecycle.append((item.command, scope_kind, identifier))
        result = {"status": "cancelled", "affected": 1}
        self.completed.append((item.id, CommandState.FINISHED, result, None))
        return result

    async def complete(self, item: ClaimedCommand, state: CommandState, result: dict[str, object], *, error_code: str | None = None, error_detail: str | None = None) -> None:
        self.completed.append((item.id, state, result, error_code))


OPERATORS = frozenset({20})


def claimed(command: str, arguments: str) -> ClaimedCommand:
    return ClaimedCommand("cmd-1", command, arguments, 10, 20)


def test_dedicated_collector_is_selected_only_for_facebook_groups() -> None:
    facebook = parse_run("facebook-groups https://www.facebook.com/groups/one https://www.facebook.com/groups/two")
    website = parse_run("website https://example.org/listing")
    fallback = parse_run("facebook https://www.facebook.com/example")
    assert facebook.method.value == "facebook_connector"
    assert facebook.source_kind == "group"
    assert len(facebook.targets) == 2
    assert website.method.value == "agent_ridge"
    assert fallback.method.value == "agent_ridge"


def test_unsafe_or_oversized_targets_are_rejected_before_planning() -> None:
    with pytest.raises(CommandValidationError):
        parse_run("website http://example.org")
    with pytest.raises(CommandValidationError):
        parse_run("website http://127.0.0.1/private")
    with pytest.raises(CommandValidationError):
        parse_run("facebook https://www.facebook.com/one https://www.facebook.com/two")
    many = " ".join(f"https://www.facebook.com/groups/{number}" for number in range(21))
    with pytest.raises(CommandValidationError):
        parse_run(f"facebook-groups {many}")


@pytest.mark.asyncio
async def test_confirmed_run_creates_a_bounded_queued_plan() -> None:
    store = FakeStore([claimed("run", "instagram https://www.instagram.com/example")])
    notices: list[str] = []

    async def notify(_chat_id: int, text: str) -> None:
        notices.append(text)

    dispatcher = OrchestraDispatcher(store, operator_ids=OPERATORS, notifier=notify)  # type: ignore[arg-type]
    assert await dispatcher.process_once()
    request, actor = store.requests[0]
    assert actor == "telegram:20"
    assert request.platform == "instagram"
    assert request.method.value == "agent_ridge"
    assert store.completed == [("cmd-1", CommandState.FINISHED, {"status": "queued", "method": "agent_ridge"}, None)]
    assert any("processing" in notice for notice in notices)
    assert any("queued" in notice for notice in notices)


@pytest.mark.asyncio
@pytest.mark.parametrize("command,arguments,expected", [
    ("pause", "source:source-id", ("pause", "source", "source-id")),
    ("resume", "source:source-id", ("resume", "source", "source-id")),
    ("cancel", "batch:batch-id", ("cancel", "batch", "batch-id")),
])
async def test_lifecycle_commands_are_dispatched(command: str, arguments: str, expected: tuple[str, str, str]) -> None:
    store = FakeStore([claimed(command, arguments)])
    dispatcher = OrchestraDispatcher(store, operator_ids=OPERATORS)  # type: ignore[arg-type]
    await dispatcher.process_once()
    assert store.lifecycle == [expected]
    assert store.completed[0][1] is CommandState.FINISHED


@pytest.mark.asyncio
async def test_invalid_command_is_auditable_failure_not_a_browser_action() -> None:
    store = FakeStore([claimed("run", "website http://bad.example")])
    dispatcher = OrchestraDispatcher(store, operator_ids=OPERATORS)  # type: ignore[arg-type]
    await dispatcher.process_once()
    assert not store.requests
    assert store.completed[0][1] is CommandState.FAILED
    assert store.completed[0][3] == "invalid_command"


@pytest.mark.asyncio
async def test_expired_leases_are_reclaimed_before_the_next_claim() -> None:
    store = FakeStore([])
    dispatcher = OrchestraDispatcher(store, operator_ids=OPERATORS)  # type: ignore[arg-type]
    assert not await dispatcher.process_once()
    assert store.reclaimed == 1


@pytest.mark.asyncio
async def test_a_lost_claim_is_reported_without_recording_a_failure() -> None:
    store = FakeStore([claimed("run", "website https://example.org")])
    notices: list[str] = []

    async def plan_run(*_: object, **__: object) -> dict[str, object]:
        raise ClaimLost("cmd-1")

    async def notify(_chat_id: int, text: str) -> None:
        notices.append(text)

    store.plan_run = plan_run  # type: ignore[method-assign]
    assert await OrchestraDispatcher(store, operator_ids=OPERATORS, notifier=notify).process_once()  # type: ignore[arg-type]
    assert store.completed == []
    assert "nothing was planned" in notices[-1]


@pytest.mark.asyncio
async def test_the_dispatch_loop_survives_database_errors() -> None:
    store = FakeStore([claimed("pause", "source:source-id")])
    failures = 2
    dispatcher = OrchestraDispatcher(store, operator_ids=OPERATORS, poll_seconds=0.1)  # type: ignore[arg-type]

    async def flaky_reclaim() -> int:
        nonlocal failures
        if failures:
            failures -= 1
            raise ConnectionError("database restarting")
        if not store.items:
            dispatcher.stop()
        return 0

    store.reclaim_expired = flaky_reclaim  # type: ignore[method-assign]
    await asyncio.wait_for(dispatcher.run_forever(), timeout=5)
    assert store.lifecycle == [("pause", "source", "source-id")]


@pytest.mark.asyncio
async def test_commands_from_non_operators_are_rejected_before_dispatch() -> None:
    store = FakeStore([ClaimedCommand("cmd-1", "cancel", "all", 10, 999)])
    notices: list[str] = []

    async def notify(_chat_id: int, text: str) -> None:
        notices.append(text)

    assert await OrchestraDispatcher(store, operator_ids=OPERATORS, notifier=notify).process_once()  # type: ignore[arg-type]
    assert store.lifecycle == [] and store.requests == []
    assert store.completed[0][1] is CommandState.FAILED and store.completed[0][3] == "not_operator"
    assert "only operators" in notices[-1]
