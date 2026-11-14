from pydantic import Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


class AnalysisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = Field(validation_alias="DATABASE_URL")
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(
        default="anthropic/claude-sonnet-4.5", validation_alias="OPENROUTER_ANALYSIS_MODEL"
    )
    # One more try with the same request when the model answers outside the schema, then one call to the fallback
    # model (empty: none). Off by default: an invalid answer is booked as an error and the page is closed.
    invalid_retry: bool = Field(default=False, validation_alias="ANALYSIS_INVALID_RETRY")
    fallback_model: str = Field(default="", validation_alias="ANALYSIS_FALLBACK_MODEL")
    # Q4: true = of a portal's JSON-LD line only price, currency, area and rooms are authoritative; a place the title or
    # description contradict is taken from the text (openrouter.LOCATION_FROM_TEXT_RULE, source_place.py). Off: as before.
    location_from_text: bool = Field(default=False, validation_alias="ANALYSIS_LOCATION_FROM_TEXT")
    # Q13: true = a JSON-LD area more than twice apart from every area the text states is not confirmed: area_m2 is
    # None, neither number is taken (area_check.py, after the model's answer; the prompt is unchanged). Off: as before.
    area_text_check: bool = Field(default=False, validation_alias="ANALYSIS_AREA_TEXT_CHECK")
    # Q18, the same variable as bot/web_search/settings.py: portal descriptions come up to 8000 characters; the place
    # check of Q4 (source_place.conflict) still reads only their first 1500, as before (an agency's «oficina en
    # Madrid» at the end of a long text is no conflict). The model reads the whole text. Off: as before.
    portal_description_full: bool = Field(default=False, validation_alias="PORTAL_DESCRIPTION_FULL")
    # Q53: for an investor post a lead is an investor's own page or its own «we invest in …» (prompt «-isrc»); a news
    # or blog address (/noticias/, /news/, a date) is dropped before the model; a project that seeks money is no lead
    # when the requester raises money (task_hint.user_role). false (default): the prompt and the hint as before.
    investor_lead_source_check: bool = Field(default=False, validation_alias="INVESTOR_LEAD_SOURCE_CHECK")
    # Q56: true = when the title or description explicitly state another bedroom count, area of the same kind or price
    # (same currency) than the JSON-LD line, the prompt («-fc») makes that field null with both numbers in reason, so
    # the reviewer gets «unknown». Prompt only, unlike Q13 (deterministic, area only). false (default): byte for byte.
    fact_conflict_null: bool = Field(default=False, validation_alias="ANALYSIS_FACT_CONFLICT_NULL")
    batch_size: PositiveInt = Field(default=10, ge=1, le=50, validation_alias="ANALYSIS_BATCH_SIZE")
    timeout_seconds: PositiveInt = Field(
        default=60, ge=5, le=120, validation_alias="OPENROUTER_ANALYSIS_TIMEOUT_SECONDS"
    )
    # --serve: seconds between cycles; a claim older than claim_seconds is taken over.
    poll_seconds: PositiveInt = Field(default=15, ge=5, le=3600, validation_alias="ANALYSIS_POLL_SECONDS")
    claim_seconds: PositiveInt = Field(default=300, ge=60, le=3600, validation_alias="ANALYSIS_CLAIM_SECONDS")
    # Comma-separated source platforms this worker leaves alone. Set it to ``website`` when the reduction worker runs
    # in live mode (AGENT_REDUCTION_MODE=live, AGENT_REDUCTION_SOURCES=website): that one then owns the website
    # posts and this worker keeps Facebook and the social networks. Empty (default): every platform.
    exclude_platforms: str = Field(default="", validation_alias="ANALYSIS_EXCLUDE_PLATFORMS")
    # The most one campaign may spend in USD across every service (bot/utils/costs.py); 0: no limit. A campaign over
    # it gets no more model calls here: its posts are closed and counted in its report.
    budget_usd: float = Field(default=5.0, ge=0, le=10_000, validation_alias="CAMPAIGN_BUDGET_USD")

    @property
    def excluded_platforms(self) -> tuple[str, ...]:
        return tuple(p.strip().lower() for p in self.exclude_platforms.split(",") if p.strip())
    telegram_token: str | None = Field(default=None, validation_alias="TELEGRAM_TOKEN")
    telegram_chat_id: int | None = Field(default=None, validation_alias="ANALYSIS_TELEGRAM_CHAT_ID")
