"""Small, explicit command contracts for the Main Orchestra."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AcquisitionMethod(StrEnum):
    FACEBOOK_CONNECTOR = "facebook_connector"
    AGENT_REACH = "agent_ridge"
    SCRAPLING = "scrapling"


class CommandState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    NEEDS_VERIFICATION = "needs_verification"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ConfirmedCommand:
    command: str
    arguments: str
    chat_id: int
    user_id: int
    message_id: int
    confirmation_id: str | None = None
    auto: bool = False  # queued by auto mode, without a confirmation


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    command_id: str
    state: CommandState
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class RunRequest:
    platform: str
    source_kind: str
    targets: tuple[str, ...]
    vertical: str
    method: AcquisitionMethod


@dataclass(frozen=True, slots=True)
class ClaimedCommand:
    id: str
    command: str
    arguments: str
    chat_id: int
    user_id: int
    attempt: int = 1


class ClaimLost(RuntimeError):
    """The command was cancelled or reclaimed; its planned work was rolled back."""
