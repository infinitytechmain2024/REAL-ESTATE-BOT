from pydantic import Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


class AnalysisSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = Field(validation_alias="DATABASE_URL")
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(
        default="openai/gpt-4o-mini", validation_alias="OPENROUTER_ANALYSIS_MODEL"
    )
    batch_size: PositiveInt = Field(default=10, ge=1, le=50, validation_alias="ANALYSIS_BATCH_SIZE")
    timeout_seconds: PositiveInt = Field(
        default=30, ge=5, le=90, validation_alias="OPENROUTER_ANALYSIS_TIMEOUT_SECONDS"
    )
    # --serve: seconds between cycles; a claim older than claim_seconds is taken over.
    poll_seconds: PositiveInt = Field(default=60, ge=10, le=3600, validation_alias="ANALYSIS_POLL_SECONDS")
    claim_seconds: PositiveInt = Field(default=300, ge=60, le=3600, validation_alias="ANALYSIS_CLAIM_SECONDS")
    telegram_token: str | None = Field(default=None, validation_alias="TELEGRAM_TOKEN")
    telegram_chat_id: int | None = Field(default=None, validation_alias="ANALYSIS_TELEGRAM_CHAT_ID")
