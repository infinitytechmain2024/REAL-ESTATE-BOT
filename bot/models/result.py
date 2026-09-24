"""Models for a result as it travels through the pipeline.

The flow is::

    SearchHit        raw entry from SearXNG (title, url, snippet)
      -> PageContent extracted article text for the promising ones
      -> StructuredResult  what the LLM made of it (score, summary, facts)
      -> StoredResult      the row that lives in Supabase and is sent to Telegram
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bot.models.enums import BudgetFit, Mode, ResultStatus
from bot.utils.urls import url_hash


class SearchHit(BaseModel):
    """One entry of SearXNG's ``results`` array."""

    model_config = ConfigDict(extra="ignore")

    url: str
    title: str = ""
    snippet: str = ""
    engines: list[str] = Field(default_factory=list)
    score: float = 0.0
    published_at: dt.datetime | None = None
    query: str | None = Field(default=None, description="Which of our queries produced this hit")

    content: str | None = Field(
        default=None,
        description="Pre-extracted page text, when the source already read it (e.g. a Facebook "
        "group post). When set, the pipeline skips fetching this URL and ranks on this text "
        "directly -- the normal HTTP fetcher cannot reach an authenticated Facebook page anyway.",
    )
    author: str | None = Field(
        default=None, description="Displayed author name, for sources that have one (e.g. a comment)"
    )

    @property
    def url_hash(self) -> str:
        return url_hash(self.url)

    @field_validator("title", "snippet", mode="before")
    @classmethod
    def _default_empty(cls, value: object) -> object:
        return value or ""


class PageContent(BaseModel):
    """Text extracted from a fetched page."""

    model_config = ConfigDict(extra="ignore")

    url: str
    final_url: str | None = Field(default=None, description="After redirects, if it differs")
    title: str | None = None
    text: str = ""
    lang: str | None = None
    fetched_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    error: str | None = Field(default=None, description="Why extraction failed, if it did")
    status: int | None = Field(
        default=None, description="HTTP status of the response, when there was one"
    )

    @property
    def blocked(self) -> bool:
        """Whether the failure looks like bot protection rather than a dead page.

        403 and 429 are what Cloudflare-fronted listing sites answer a plain
        HTTP client with; 404 or a genuine timeout are not worth a second,
        much more expensive, attempt through a real browser.
        """
        return self.status in (401, 403, 429, 503)

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


class StructuredResult(BaseModel):
    """A candidate after the LLM has read it.

    This is the LLM's output schema, so field descriptions are prompt text.
    """

    model_config = ConfigDict(extra="ignore")

    url: str
    title: str = Field(description="Short factual title, max ~90 characters")
    summary: str = Field(description="2-4 sentences: what this is and why it matches the request")
    why_relevant: str | None = Field(default=None, description="The single strongest match reason")
    score: int = Field(default=0, ge=0, le=100, description="Relevance 0-100 against the request")

    location: str | None = Field(default=None, description="Location mentioned by the source")
    price: str | None = Field(default=None, description="Price as written, with currency")
    price_value: float | None = Field(
        default=None,
        description="The price as a plain number, no separators or currency symbol. "
        "For a range, the lower end. Null if the page states no price.",
    )
    price_currency: str | None = Field(
        default=None, description="ISO-4217 code of `price_value`, e.g. EUR"
    )
    area: str | None = Field(default=None, description="Area as written, with units")
    contacts: list[str] = Field(
        default_factory=list, description="Phone numbers, e-mails or contact page URLs found"
    )
    seller: str | None = Field(default=None, description="Seller or author name shown by the source")
    criteria_match: bool = Field(default=False, description="Whether all hard request criteria are evidenced")
    missing_criteria: list[str] = Field(
        default_factory=list, description="Hard criteria not evidenced by this source"
    )
    language: str | None = Field(default=None, description="ISO-639-1 code of the source page")
    sources: list[str] = Field(
        default_factory=list,
        description="Source engines or adapters that found this same result",
    )

    @field_validator("score", mode="before")
    @classmethod
    def _clamp_score(cls, value: object) -> object:
        """Models occasionally answer 0-1, '85%' or out-of-range integers."""
        if isinstance(value, str):
            value = value.strip().rstrip("%")
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0
        if 0.0 < number <= 1.0:
            number *= 100
        return int(max(0, min(100, round(number))))

    @field_validator("price_value", mode="before")
    @classmethod
    def _parse_price(cls, value: object) -> object:
        """Tolerate "285 000", "€285,000" and "285000 EUR" as well as a number."""
        if value is None or isinstance(value, (int, float)):
            return value
        if not isinstance(value, str):
            return None
        cleaned = re.sub(r"[^\d.,]", "", value).replace(" ", "")
        if not cleaned:
            return None
        # Thousands separators vary by locale; the last separator is decimal
        # only when it is followed by exactly two digits.
        if re.search(r"[.,]\d{2}$", cleaned):
            cleaned = cleaned[:-3].replace(",", "").replace(".", "") + "." + cleaned[-2:]
        else:
            cleaned = cleaned.replace(",", "").replace(".", "")
        try:
            return float(cleaned)
        except ValueError:
            return None

    @field_validator("price_currency")
    @classmethod
    def _upper_currency(cls, value: str | None) -> str | None:
        return value.upper()[:3] if value else None

    @field_validator("contacts", mode="before")
    @classmethod
    def _coerce_contacts(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value or []

    @property
    def url_hash(self) -> str:
        return url_hash(self.url)


class StoredResult(BaseModel):
    """A ``results`` row. Mirrors services/db/migrations/001_init.sql."""

    model_config = ConfigDict(extra="ignore")

    id: UUID | None = None
    search_id: UUID | None = None
    user_id: int
    mode: Mode
    url: str
    url_hash: str
    title: str = ""
    summary: str = ""
    score: int = 0
    status: ResultStatus = ResultStatus.NEW
    raw: dict[str, Any] = Field(default_factory=dict)
    content: str | None = None
    created_at: dt.datetime | None = None

    # Derived at request time from the user's budget, not persisted as columns
    # (they live inside `raw`): the same listing is a match for one user and a
    # near miss for another, so the verdict belongs to the answer, not the row.
    budget_fit: BudgetFit = BudgetFit.UNKNOWN
    budget_delta: float | None = None
    budget_currency: str | None = None

    @property
    def is_alternative(self) -> bool:
        """Whether this was offered as a near miss rather than a match."""
        return self.budget_fit.is_alternative

    @classmethod
    def from_structured(
        cls,
        result: StructuredResult,
        *,
        user_id: int,
        mode: Mode,
        search_id: UUID | None,
        content: str | None = None,
        budget_fit: BudgetFit = BudgetFit.UNKNOWN,
        budget_delta: float | None = None,
        budget_currency: str | None = None,
    ) -> StoredResult:
        return cls(
            search_id=search_id,
            user_id=user_id,
            mode=mode,
            url=result.url,
            url_hash=result.url_hash,
            title=result.title,
            summary=result.summary,
            score=result.score,
            status=ResultStatus.NEW,
            raw=result.model_dump(mode="json"),
            content=content,
            budget_fit=budget_fit,
            budget_delta=budget_delta,
            budget_currency=budget_currency,
        )
