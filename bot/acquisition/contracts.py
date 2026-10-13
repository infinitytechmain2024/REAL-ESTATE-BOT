"""One post as an acquisition provider hands it over (schema v1): the same shape from the local browser collector
and from Apify, so analysis, dedup and the report never learn where a post came from.

Not ``Evidence``: that name is the analysis worker's input (``bot.analysis_pipeline.models.Evidence``). An
``AcquiredPost`` becomes a ``collected_posts`` row (``collected_post_row``), and the analysis worker reads it from
there as it reads every other post.

Rules of the contract:

* unknown is ``None`` -- never ``""`` and never ``0``: an empty string or a zero price/area is rejected, so a mapper
  cannot hide a missing value behind a placeholder;
* a price needs its currency (``ValidationError`` otherwise); a rent price without its period is accepted with an
  issue (``issues()``), because some ads only say «900 €»;
* ``provider_facts`` are what the provider claims, not facts: the analysis worker still extracts and checks price,
  area and deal against quotes from the text (``verify_facts``);
* phones are kept as written, with an optional E.164 form; nothing here holds cookies, tokens or session data.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

log = logging.getLogger(__name__)

SCHEMA_VERSION = "1"
_E164 = re.compile(r"\+[1-9]\d{6,14}")
_POST_ID = re.compile(r"/(?:posts|permalink|story_fbid=|p)/?(\d{5,})|[?&](?:story_fbid|fbid|multi_permalinks)=(\d{5,})")


def _no_empty_strings(data: Any) -> Any:
    """``""`` (or only spaces) for a field is refused: unknown is ``None``."""
    if isinstance(data, dict):
        empty = sorted(k for k, v in data.items() if isinstance(v, str) and not v.strip())
        if empty:
            raise ValueError(f"empty string instead of null: {', '.join(empty)}")
    return data


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _nulls(cls, data: Any) -> Any:
        return _no_empty_strings(data)


class Phone(_Strict):
    raw: str = Field(max_length=40)          # as written in the post
    e164: str | None = None                  # +34600123456, when it could be normalised

    @field_validator("e164")
    @classmethod
    def _e164(cls, value: str | None) -> str | None:
        if value is not None and not _E164.fullmatch(value):
            raise ValueError("e164 must look like +34600123456")
        return value


class Provenance(_Strict):
    provider: Literal["local", "apify"]
    actor_id: str | None = Field(default=None, max_length=120)   # apify only: «owner/actor»
    run_id: str | None = Field(default=None, max_length=80)
    group_url: str = Field(max_length=2048)
    post_url: str = Field(max_length=2048)
    published_at: datetime | None = None
    collected_at: datetime

    @field_validator("group_url", "post_url")
    @classmethod
    def _http(cls, value: str) -> str:
        if urlsplit(value).scheme not in ("http", "https") or not urlsplit(value).hostname:
            raise ValueError("must be an http(s) URL")
        return value

    @model_validator(mode="after")
    def _apify_run(self) -> Provenance:
        if self.provider == "apify" and not (self.actor_id and self.run_id):
            raise ValueError("an apify post needs actor_id and run_id")
        return self


class ListingFacts(_Strict):
    listing_kind: Literal["offer", "wanted", "catalog", "other"] | None = None
    deal_type: Literal["rent", "sale"] | None = None
    property_type: Literal["apartment", "room", "house", "studio", "land", "commercial", "other"] | None = None
    price_amount: float | None = Field(default=None, gt=0)
    price_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    price_period: Literal["month", "week", "day", "year", "total"] | None = None
    area_m2: float | None = Field(default=None, gt=0, le=100_000_000)
    location_text: str | None = Field(default=None, max_length=300)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    phones: tuple[Phone, ...] = ()
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _price(self) -> ListingFacts:
        if self.price_amount is not None and self.price_currency is None:
            raise ValueError("price_amount without price_currency")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude come together")
        if self.issues():
            log.warning("acquisition.listing_issues %s", ",".join(self.issues()))
        return self

    def issues(self) -> list[str]:
        """What is accepted but incomplete (a warning, never an error)."""
        out = []
        if self.deal_type == "rent" and self.price_amount is not None and self.price_period is None:
            out.append("price_period_missing")
        return out


class AcquiredPost(_Strict):
    schema_version: Literal["1"] = SCHEMA_VERSION
    campaign_id: str = Field(min_length=1, max_length=64)
    source: Provenance
    raw_text: str = Field(min_length=1, max_length=50_000)
    provider_facts: ListingFacts = Field(default_factory=ListingFacts)
    quotes: dict[str, str] = Field(default_factory=dict)  # fact -> verbatim quote; «needs_split»: several objects

    @field_validator("quotes")
    @classmethod
    def _quotes(cls, value: dict[str, str]) -> dict[str, str]:
        _no_empty_strings(value)
        return {k[:40]: v[:300] for k, v in value.items()}

    def platform_post_id(self) -> str:
        """The post's own id from its URL (/posts/123…, story_fbid=123…), else a hash of the URL."""
        found = _POST_ID.search(self.source.post_url)
        if found:
            return found.group(1) or found.group(2)
        return "url:" + hashlib.sha256(self.source.post_url.encode()).hexdigest()[:32]

    def content_hash(self) -> str:
        """The same hash the browser collector stores (``PostgresCollectorStore.save_post``): a post read by both
        providers is one row."""
        return hashlib.sha256((self.source.post_url + "\n" + self.raw_text).encode()).hexdigest()

    def collected_post_row(self) -> dict[str, Any]:
        """The ``collected_posts`` columns this post fills (``source_id`` and ``acquisition_run_id`` are the store's)."""
        payload = {"schema_version": self.schema_version, "provider": self.source.provider,
                   "actor_id": self.source.actor_id, "run_id": self.source.run_id, "group_url": self.source.group_url,
                   "collected_at": self.source.collected_at.isoformat(),
                   "provider_facts": self.provider_facts.model_dump(mode="json", exclude_defaults=True),
                   "quotes": self.quotes}
        return {"platform_post_id": self.platform_post_id(), "canonical_url": self.source.post_url,
                "body_text": self.raw_text, "published_at": self.source.published_at,
                "content_hash": self.content_hash(), "raw_payload": payload, "state": "normalised"}
