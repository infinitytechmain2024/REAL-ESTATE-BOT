from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AnalysisResult(BaseModel):
    """The sole accepted model output; extra keys make the response invalid."""

    model_config = ConfigDict(extra="forbid", strict=True)
    relevant: bool
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(max_length=1000)
    location: str | None = Field(default=None, max_length=200)
    price_signals: list[str] = Field(default_factory=list, max_length=10)
    related_links: list[str] = Field(default_factory=list, max_length=10)
    category: Literal["real_estate", "investors", "other"]
    reason: str = Field(max_length=300)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    post_id: str
    source_id: str
    canonical_url: str
    text: str
    title: str = ""
    published_at: datetime | None = None
    comments: list[str] = Field(default_factory=list)
    profile_extract: dict[str, object] | None = None


class FilterDecision(BaseModel):
    accepted: bool
    reason: str
    language: Literal["es", "ru", "en", "unknown"]
