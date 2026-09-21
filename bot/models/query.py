"""Everything that describes *what the user is looking for*.

:class:`ParsedQuery` is the structured form an LLM extracts from the raw text
(or from a voice transcript). It is deliberately tolerant: every field is
optional, because a user is allowed to say "land in Cyprus" and nothing else.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bot.models.enums import Mode


class Location(BaseModel):
    """Where the user is looking. All parts optional -- users are vague."""

    model_config = ConfigDict(extra="ignore")

    country: str | None = Field(default=None, description="Country name in English, e.g. 'Cyprus'")
    region: str | None = Field(default=None, description="Region/state/province")
    city: str | None = Field(default=None, description="City or district")
    raw: str | None = Field(default=None, description="Location exactly as the user phrased it")

    def as_text(self) -> str:
        """Human-readable location, most specific part first."""
        parts = [p for p in (self.city, self.region, self.country) if p]
        return ", ".join(parts) or (self.raw or "")

    def is_empty(self) -> bool:
        return not any((self.country, self.region, self.city, self.raw))


class ParsedQuery(BaseModel):
    """Structured intent extracted from the user's message.

    This is the schema handed to the LLM, so the field descriptions double as
    prompt instructions -- keep them explicit.
    """

    model_config = ConfigDict(extra="ignore")

    mode: Mode = Field(description="'land' for property/plots, 'investors' for investors/companies")
    location: Location = Field(default_factory=Location)

    object_type: str | None = Field(
        default=None,
        description="Type of object or counterparty: 'land plot', 'villa', 'warehouse', "
        "'private equity fund', 'developer', ...",
    )
    area_min: float | None = Field(default=None, description="Minimum area, square metres")
    area_max: float | None = Field(default=None, description="Maximum area, square metres")
    building_required: bool | None = Field(
        default=None,
        description="True only when a building is required; false means land without one; null means either",
    )
    metro_drive_minutes: int | None = Field(
        default=None, description="Maximum driving minutes to the nearest metro station"
    )
    buildable_required: bool = Field(
        default=False, description="The land must be suitable or permitted for construction"
    )
    budget_min: float | None = Field(default=None, description="Minimum budget, in `currency`")
    budget_max: float | None = Field(default=None, description="Maximum budget, in `currency`")
    currency: str | None = Field(default=None, description="ISO-4217 code of the budget, e.g. EUR")

    languages: list[str] = Field(
        default_factory=list,
        description="ISO-639-1 codes worth searching in, most useful first. Always include the "
        "local language of the target country plus 'en'.",
    )
    keywords: list[str] = Field(
        default_factory=list, description="Salient terms to keep in the search queries"
    )
    exclude: list[str] = Field(
        default_factory=list, description="Terms the user explicitly does not want"
    )
    timeframe: str | None = Field(default=None, description="Recency constraint, if the user gave one")
    notes: str | None = Field(default=None, description="Anything else worth carrying into ranking")

    @field_validator("languages", "keywords", "exclude", mode="before")
    @classmethod
    def _coerce_list(cls, value: object) -> object:
        """LLMs sometimes answer with a comma-joined string instead of a list."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if value is None:
            return []
        return value

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str | None) -> str | None:
        return value.upper() if value else None

    def human_summary(self) -> str:
        """Short description shown to the user in progress messages.

        Unlike :meth:`summary` this omits the internal mode token and reads as
        a phrase rather than a debug line.
        """
        bits: list[str] = []
        if self.object_type:
            bits.append(self.object_type)
        if not self.location.is_empty():
            bits.append(self.location.as_text())
        if self.budget_max:
            currency = f" {self.currency}" if self.currency else ""
            bits.append(f"до {self.budget_max:,.0f}".replace(",", " ") + currency)
        if self.area_max:
            bits.append(f"до {self.area_max:,.0f} m²".replace(",", " "))
        return ", ".join(bits)

    def summary(self) -> str:
        """One-line description used in log lines."""
        bits: list[str] = [self.mode.value]
        if not self.location.is_empty():
            bits.append(self.location.as_text())
        if self.object_type:
            bits.append(self.object_type)
        if self.budget_max:
            bits.append(f"<= {self.budget_max:g} {self.currency or ''}".strip())
        return " | ".join(bits)


class SearchQuery(BaseModel):
    """A single string to send to SearXNG, plus the knobs SearXNG understands."""

    model_config = ConfigDict(extra="ignore")

    query: str
    language: str = "all"
    """SearXNG language filter; 'all' lets every engine answer."""
    pages: int = 1
    weight: float = 1.0
    """Relative trust in this phrasing; used to order merged hits."""

    @field_validator("query")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("search query must not be empty")
        return cleaned
