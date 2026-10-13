"""Versioned, provider-neutral evidence collected for one campaign."""

from __future__ import annotations

import warnings
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def reject_blank_strings(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("unknown string fields must be null, not empty")
        return value


class EvidenceSource(ContractModel):
    provider: str
    actor_id: str | None = None
    run_id: str | None = None
    group_url: str | None = None
    post_url: str | None = None
    published_at: datetime | None = None
    collected_at: datetime


class PhoneNumber(ContractModel):
    original: str
    e164: str | None = Field(default=None, pattern=r"^\+[1-9]\d{7,14}$")


class ListingFacts(ContractModel):
    listing_kind: Literal["offer", "catalog", "wanted", "other"] | None = None
    deal_type: Literal["rent", "sale"] | None = None
    property_type: Literal[
        "apartment", "room", "house", "studio", "land", "commercial", "other"
    ] | None = None
    price_amount: float | None = Field(default=None, gt=0)
    price_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    price_period: str | None = None
    area_m2: float | None = Field(default=None, gt=0)
    location_text: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    phones: list[PhoneNumber] = Field(default_factory=list)
    confidence: float | None = Field(default=None, gt=0, le=1)

    @field_validator("phones", mode="before")
    @classmethod
    def accept_source_phone_strings(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [{"original": phone} if isinstance(phone, str) else phone for phone in value]
        return value

    @model_validator(mode="after")
    def validate_price(self) -> ListingFacts:
        if self.price_amount is not None and self.price_currency is None:
            raise ValueError("price_currency is required when price_amount is set")
        if (
            self.deal_type == "rent"
            and self.price_amount is not None
            and self.price_period is None
        ):
            warnings.warn(
                "rental price has no price_period",
                UserWarning,
                stacklevel=2,
            )
        return self


class Evidence(ContractModel):
    schema_version: Literal["1"] = "1"
    campaign_id: str
    source: EvidenceSource
    raw_text: str
    listing: ListingFacts = Field(default_factory=ListingFacts)
    evidence: dict[str, str] = Field(default_factory=dict)

    @field_validator("evidence")
    @classmethod
    def reject_blank_quotes(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not key.strip() or not quote.strip() for key, quote in value.items()):
            raise ValueError("evidence keys and quotes must not be empty")
        return value
