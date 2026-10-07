"""Types crossing the verification flow's boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

OPEN_STATES = frozenset({"requested", "active"})


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    state: str
    job_type: str
    source_id: str
    source_url: str
    platform: str
    resolution_note: str | None
    profile_id: str | None
    profile_name: str | None
    profile_state: str | None
    batch_id: str | None
    challenge_kind: str | None = None
    sensitive: bool = False
    claimed_by: int | None = None
    notified_at: datetime | None = None
    solved_at: datetime | None = None
    recovered_at: datetime | None = None
    resumed_at: datetime | None = None
    expires_at: datetime | None = None
    # The page to open in the live browser and reload in the watchdog (web_challenge jobs); None: the source URL.
    target_url: str | None = None

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def page_url(self) -> str:
        """What a person and the watchdog open: the challenged page when known, else the source's own URL."""
        return self.target_url or self.source_url

    @property
    def host(self) -> str:
        """The website of the source (platform ``website``)."""
        return urlsplit(self.source_url).hostname or self.source_url


@dataclass(frozen=True, slots=True)
class AccessToken:
    id: str
    job_id: str
    user_id: int
    profile_id: str


@dataclass(frozen=True, slots=True)
class PageSession:
    id: str
    job_id: str
    user_id: int
    profile_id: str
    identity: str  # "telegram:<id>", from the Mini App signature
    csrf_token: str


@dataclass(frozen=True, slots=True)
class Event:
    event: str
    actor: str
    detail: dict[str, object]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class Recovery:
    """What the watchdog saw on the profile's own browser."""

    clear: bool
    kind: str | None = None
    sensitive: bool = False
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Launch:
    """Automatic collector restart after a resume (migration 012)."""

    id: str
    batch_id: str
    state: str
    notify_user_id: int | None
    result: str | None = None
    error: str | None = None
    job_id: str | None = None
    # Still pending after the grace time: the runner may not be running.
    stale: bool = False
