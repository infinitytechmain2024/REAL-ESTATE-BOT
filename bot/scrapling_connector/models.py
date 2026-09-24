"""Explicit contracts for the one-page Scrapling worker."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from bot.acquisition.models import NormalizedPage


class ScraplingOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED_LIMIT = "stopped_limit"


@dataclass(frozen=True)
class ScraplingTask:
    run_id: str
    source_id: str
    target: str
    max_runtime_seconds: int
    max_pages: int


@dataclass(frozen=True)
class ScraplingResult:
    run_id: str
    outcome: ScraplingOutcome
    page: NormalizedPage | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "outcome": self.outcome.value,
            "page": self.page.as_dict() if self.page else None,
            "reason": self.reason,
        }
