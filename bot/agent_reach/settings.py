"""Configuration for the controlled Agent Reach adapter only."""

from __future__ import annotations

from pydantic import Field, PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentReachSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    browser_url: str = Field(default="http://browser:8090", validation_alias="BROWSER_SESSION_URL")
    browser_token: str = Field(min_length=24, validation_alias="BROWSER_SESSION_API_TOKEN")
    max_pages: PositiveInt = Field(default=5, ge=1, le=20, validation_alias="AGENT_REACH_MAX_PAGES")
    max_execution_seconds: PositiveInt = Field(default=120, ge=10, le=600, validation_alias="AGENT_REACH_MAX_EXECUTION_SECONDS")
    page_timeout_seconds: PositiveInt = Field(default=30, ge=5, le=60, validation_alias="AGENT_REACH_PAGE_TIMEOUT_SECONDS")
    # Worker mode only (bot.agent_reach.worker): runs /run-queued tasks from PostgreSQL.
    database_url: str = Field(default="", validation_alias="DATABASE_URL")
    poll_seconds: PositiveInt = Field(default=15, ge=5, le=300, validation_alias="AGENT_REACH_POLL_SECONDS")
    # Documented only: this service never enables or runs upstream Agent Reach.
    upstream_enabled: bool = Field(default=False, validation_alias="AGENT_REACH_UPSTREAM_ENABLED")

    @model_validator(mode="after")
    def reject_upstream_execution(self) -> AgentReachSettings:
        if self.upstream_enabled:
            raise ValueError("upstream Agent Reach execution is intentionally unsupported")
        return self
