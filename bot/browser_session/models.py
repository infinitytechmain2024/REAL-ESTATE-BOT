"""Public types for the browser-session boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class BrowserProfileStatus(StrEnum):
    """Persisted profile states from migration 003 plus transient lease states."""

    PROVISIONED = "PROVISIONED"
    READY = "READY"
    IN_USE = "IN_USE"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    QUARANTINED = "QUARANTINED"
    DISABLED = "DISABLED"
    RETIRED = "RETIRED"
    LOCKED = "LOCKED"  # another live lease owns the profile; never persisted
    COOLDOWN = "COOLDOWN"  # optional local safety delay; never persisted


@dataclass(frozen=True)
class ProfileRequest:
    profile_id: str
    profile_name: str
    platform: str


@dataclass(frozen=True)
class SessionHandle:
    profile_id: str
    token: str
    profile_dir: Path
    expires_at: float


@dataclass(frozen=True)
class ProfileStatus:
    profile_id: str
    state: BrowserProfileStatus
    lease_expires_at: float | None = None
    detail: str | None = None
