"""Environment of the ``reduction-worker`` service (SA-2 Reduction agents, shadow mode)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .reduction import ReductionConfig


class ReductionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    enabled: bool = Field(default=False, validation_alias="AGENT_REDUCTION_ENABLED")
    database_url: str = Field(default="", validation_alias="DATABASE_URL")
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    # OpenRouter model ids, e.g. an anthropic/… id for Claude and the Jev id: check /api/v1/models.
    claude_model: str = Field(default="", validation_alias="OPENROUTER_CLAUDE_MODEL")
    jev_model: str = Field(default="", validation_alias="OPENROUTER_JEV_MODEL")
    timeout_seconds: float = Field(default=45, ge=5, le=180, validation_alias="AGENT_REDUCTION_TIMEOUT_SECONDS")
    poll_seconds: float = Field(default=15, ge=2, le=600, validation_alias="AGENT_REDUCTION_POLL_SECONDS")
    batch: int = Field(default=5, ge=1, le=50, validation_alias="AGENT_REDUCTION_BATCH")
    concurrency: int = Field(default=4, ge=1, le=16, validation_alias="AGENT_REDUCTION_CONCURRENCY")
    lease_seconds: int = Field(default=300, ge=60, le=3600, validation_alias="AGENT_REDUCTION_LEASE_SECONDS")
    max_calls_per_day: int = Field(default=1000, ge=0, le=100_000, validation_alias="AGENT_REDUCTION_MAX_CALLS_PER_DAY")

    def missing(self) -> list[str]:
        """Why the service must stay idle: switched off, or a required setting is empty."""
        if not self.enabled:
            return ["AGENT_REDUCTION_ENABLED"]
        return [name for name, value in (("DATABASE_URL", self.database_url), ("OPENROUTER_API_KEY", self.openrouter_api_key),
                                         ("OPENROUTER_CLAUDE_MODEL", self.claude_model),
                                         ("OPENROUTER_JEV_MODEL", self.jev_model)) if not value]

    def config(self) -> ReductionConfig:
        return ReductionConfig(batch=self.batch, concurrency=self.concurrency, lease_seconds=self.lease_seconds,
                               max_calls_per_day=self.max_calls_per_day)
