"""Stable, deliberately small contract between Orchestra and Agent Reach."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class ReachPlatform(StrEnum):
    FACEBOOK = "facebook"
    INSTAGRAM = "instagram"
    TIKTOK = "tiktok"
    WEBSITE = "website"


class ReachSkill(StrEnum):
    READ_PUBLIC_PAGE = "read_public_page"
    EXTRACT_PUBLIC_TEXT = "extract_public_text"


class ReachOutcome(StrEnum):
    COMPLETED = "completed"
    STOPPED_CHALLENGE = "stopped_challenge"
    STOPPED_LIMIT = "stopped_limit"
    FAILED = "failed"


@dataclass(frozen=True)
class ReachTask:
    """An explicit, read-only request. URLs are not discovered or followed."""

    task_id: str
    platform: ReachPlatform
    targets: tuple[str, ...]
    browser_profile_id: str
    browser_profile_name: str
    browser_profile_state: str = "ready"
    allowed_skills: tuple[ReachSkill, ...] = (ReachSkill.READ_PUBLIC_PAGE, ReachSkill.EXTRACT_PUBLIC_TEXT)


@dataclass(frozen=True)
class NormalizedPage:
    canonical_url: str
    title: str
    text: str
    platform: str
    source_type: str = "public_page"

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ReachResult:
    task_id: str
    outcome: ReachOutcome
    pages_visited: int
    pages: tuple[NormalizedPage, ...]
    stop_reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "outcome": self.outcome.value,
            "pages_visited": self.pages_visited,
            "pages": [page.as_dict() for page in self.pages],
            "stop_reason": self.stop_reason,
        }
