"""Configuration with deliberately conservative browser defaults."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrowserSessionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BROWSER_", extra="ignore")

    # Redis is shared with the rest of the stack, so retain its existing name.
    redis_url: str = Field(validation_alias="REDIS_URL")
    profile_root: Path = Path("/profiles")
    screenshot_root: Path = Path("/screenshots")
    lease_seconds: PositiveInt = Field(default=120, ge=30, le=3600)
    lease_renew_seconds: PositiveInt = Field(default=30, ge=5, le=900)
    # Release a session whose caller has made no request for this long. It must
    # exceed the longest pause a live collector takes between requests.
    idle_seconds: PositiveInt = Field(default=300, ge=60, le=3600)
    shutdown_timeout_seconds: PositiveInt = Field(default=20, ge=5, le=120)
    api_host: str = "0.0.0.0"
    api_port: PositiveInt = 8090
    # Required for all mutation endpoints. The service is internal-only, but
    # an explicit shared secret prevents accidental control by another container.
    api_token: str = Field(min_length=24, validation_alias="BROWSER_SESSION_API_TOKEN")
