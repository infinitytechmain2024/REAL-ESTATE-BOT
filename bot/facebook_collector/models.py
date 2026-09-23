"""Types crossing the Facebook collector boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GroupState(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    INACCESSIBLE = "INACCESSIBLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class BatchItem:
    id: str
    source_id: str
    canonical_url: str
    sequence_no: int


@dataclass(frozen=True)
class BatchPlan:
    id: str
    browser_profile_id: str
    browser_profile_name: str
    browser_profile_state: str
    max_items: int
    items: tuple[BatchItem, ...]


@dataclass(frozen=True)
class CollectedPost:
    platform_post_id: str
    canonical_url: str
    body_text: str
    published_at: str | None = None
    author_handle: str | None = None


@dataclass(frozen=True)
class GroupRead:
    state: GroupState
    posts: tuple[CollectedPost, ...]
    evidence: dict[str, object]


class ChallengeDetected(RuntimeError):
    """Facebook presented a challenge and all automation must stop."""

    def __init__(self, reason: str, evidence: dict[str, object]) -> None:
        super().__init__(reason)
        self.reason, self.evidence = reason, evidence

