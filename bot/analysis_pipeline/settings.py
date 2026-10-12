from pydantic import Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


class AnalysisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = Field(validation_alias="DATABASE_URL")
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(
        default="anthropic/claude-sonnet-4.5", validation_alias="OPENROUTER_ANALYSIS_MODEL"
    )
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
