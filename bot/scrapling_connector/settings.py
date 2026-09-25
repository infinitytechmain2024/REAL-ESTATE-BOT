"""Conservative configuration for the HTTP-only Scrapling connector."""

from __future__ import annotations

from pydantic import Field, PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ScraplingSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(validation_alias="DATABASE_URL")
    run_id: str = Field(default="", validation_alias="SCRAPLING_RUN_ID")
    # Worker mode only (bot.scrapling_connector.worker): seconds between queue checks.
    poll_seconds: PositiveInt = Field(default=15, ge=5, le=300, validation_alias="SCRAPLING_POLL_SECONDS")
    max_runtime_seconds: PositiveInt = Field(default=45, ge=10, le=300, validation_alias="SCRAPLING_CONNECTOR_MAX_RUNTIME_SECONDS")
    request_timeout_seconds: PositiveInt = Field(default=20, ge=3, le=60, validation_alias="SCRAPLING_CONNECTOR_REQUEST_TIMEOUT_SECONDS")
    max_content_bytes: PositiveInt = Field(default=1_500_000, ge=10_000, le=5_000_000, validation_alias="SCRAPLING_CONNECTOR_MAX_CONTENT_BYTES")
    max_content_chars: PositiveInt = Field(default=120_000, ge=1_000, le=200_000, validation_alias="SCRAPLING_CONNECTOR_MAX_CONTENT_CHARS")
    user_agent: str = Field(default="RealEstateResearchBot/0.1 (+https://github.com/infinitytechmain2024/REAL-ESTATE-BOT)", validation_alias="SCRAPLING_CONNECTOR_USER_AGENT")

    @model_validator(mode="after")
    def check_request_fits_total_budget(self) -> ScraplingSettings:
        if self.request_timeout_seconds > self.max_runtime_seconds:
            raise ValueError("SCRAPLING_CONNECTOR_REQUEST_TIMEOUT_SECONDS cannot exceed total runtime")
        return self
