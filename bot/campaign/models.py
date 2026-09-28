"""The campaign plan contract: a bounded, JSON-serialisable description of a goal."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PLAN_VERSION = "campaign-plan/1"
WINDOW_SIZE = 20
MAX_GROUPS = 200
MAX_WINDOWS = 10
MAX_SEEDS_PER_LANGUAGE = 6
MAX_SEED_CHARS = 80
CONSTRAINT_KEYS = frozenset({"deal", "max_price", "rooms"})

Language = Literal["es", "en", "ru", "uk"]
LANGUAGES: tuple[Language, ...] = ("es", "en", "ru", "uk")
Vertical = Literal["real_estate", "investors", "both"]
CampaignState = Literal[
    "planned", "discovering", "running", "paused_verification", "completed", "cancelled", "failed",
]

TERMINAL_STATES: frozenset[str] = frozenset({"completed", "cancelled", "failed"})
# Mirrors public.enforce_campaign_transition() in migration 014.
TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"discovering", "running", "cancelled", "failed"}),
    "discovering": frozenset({"running", "paused_verification", "completed", "cancelled", "failed"}),
    "running": frozenset({"paused_verification", "completed", "cancelled", "failed"}),
    "paused_verification": frozenset({"discovering", "running", "cancelled", "failed"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
    "failed": frozenset(),
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


class CampaignLimits(BaseModel):
    """Hard work bounds: groups are processed in windows of at most 20."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    max_groups: int = Field(default=40, ge=1, le=MAX_GROUPS)
    max_windows: int = Field(default=0, ge=1, le=MAX_WINDOWS, validate_default=True)
    window_size: int = Field(default=WINDOW_SIZE, ge=1, le=WINDOW_SIZE)

    @model_validator(mode="before")
    @classmethod
    def _default_windows(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("max_windows") is None:
            groups = data.get("max_groups", 40)
            size = data.get("window_size", WINDOW_SIZE)
            if isinstance(groups, int) and isinstance(size, int) and groups > 0 and size > 0:
                data = {**data, "max_windows": min(MAX_WINDOWS, math.ceil(groups / size))}
        return data


class CampaignPlan(BaseModel):
    """What a campaign will look for; produced by ``plan_campaign``, never by a model."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    plan_version: str = PLAN_VERSION
    goal: str = Field(min_length=1, max_length=200)
    location: str = Field(min_length=1, max_length=80)
    location_aliases: dict[Language, str]
    # ISO-2 country of the place, when known (any place in the world; None for plans stored before it).
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    vertical: Vertical
    languages: list[Language] = Field(default_factory=lambda: list(LANGUAGES), min_length=1)
    platforms_order: list[str] = Field(default_factory=lambda: ["facebook_groups", "websites"], min_length=1)
    query_seeds: dict[Language, list[str]]
    constraints: dict[str, str | int | None] = Field(default_factory=dict)
    limits: CampaignLimits = Field(default_factory=CampaignLimits)

    @field_validator("languages")
    @classmethod
    def _unique_languages(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("languages must be unique")
        return value

    @field_validator("query_seeds")
    @classmethod
    def _bounded_seeds(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        cleaned: dict[str, list[str]] = {}
        for language, seeds in value.items():
            seen: set[str] = set()
            unique: list[str] = []
            for seed in seeds:
                seed = " ".join(seed.split())
                if not seed or len(seed) > MAX_SEED_CHARS:
                    raise ValueError(f"query seed must be 1..{MAX_SEED_CHARS} characters")
                if seed.casefold() not in seen:
                    seen.add(seed.casefold())
                    unique.append(seed)
            if len(unique) > MAX_SEEDS_PER_LANGUAGE:
                raise ValueError(f"at most {MAX_SEEDS_PER_LANGUAGE} query seeds per language")
            cleaned[language] = unique
        return cleaned

    @field_validator("constraints")
    @classmethod
    def _known_constraints(cls, value: dict[str, Any]) -> dict[str, Any]:
        unknown = set(value) - CONSTRAINT_KEYS
        if unknown:
            raise ValueError(f"unknown constraints: {sorted(unknown)}")
        if value.get("deal") not in (None, "rent", "sale"):
            raise ValueError("deal must be rent, sale or null")
        for key in ("max_price", "rooms"):
            number = value.get(key)
            if number is not None and (not isinstance(number, int) or number <= 0):
                raise ValueError(f"{key} must be a positive integer")
        return value

    @model_validator(mode="after")
    def _seeds_match_languages(self) -> CampaignPlan:
        if set(self.query_seeds) - set(self.languages):
            raise ValueError("query_seeds has a language outside languages")
        return self


@dataclass(frozen=True, slots=True)
class Campaign:
    id: str
    plan: CampaignPlan
    state: str
    chat_id: int
    requested_by: int
    source_text: str
    status_message_id: int | None
    stop_reason: str | None
    created_at: datetime
    finished_at: datetime | None = None
