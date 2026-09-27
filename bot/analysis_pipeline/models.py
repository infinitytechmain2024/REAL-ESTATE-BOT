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
    # analysis-v3: what a Russian finding card and the budget rule need, from the same call.
    summary_ru: str | None = Field(default=None, max_length=1500)
    source_language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}$")
    price_amount: float | None = Field(default=None, ge=0)
    price_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    deal_type: Literal["rent", "sale"] | None = None
    property_type: Literal["apartment", "room", "house", "studio", "land", "commercial", "other"] | None = None
    rooms: int | None = Field(default=None, ge=0, le=50)
    who: str | None = Field(default=None, max_length=200)
    # analysis-v4: only a concrete single offer may reach users; where it is and how big.
    listing_kind: Literal["offer", "catalog", "wanted", "other"] | None = None
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    area_m2: float | None = Field(default=None, gt=0, le=100_000_000)


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
