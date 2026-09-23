"""Conservative runtime settings for the Facebook batch collector."""

from __future__ import annotations

from pydantic import Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict


class FacebookCollectorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(validation_alias="DATABASE_URL")
    browser_url: str = Field(default="http://browser:8090", validation_alias="BROWSER_SESSION_URL")
    browser_token: str = Field(min_length=24, validation_alias="BROWSER_SESSION_API_TOKEN")
    max_groups: PositiveInt = Field(default=20, ge=1, le=20)
    max_posts_per_group: PositiveInt = Field(default=15, ge=1, le=20)
    group_timeout_seconds: PositiveInt = Field(default=90, ge=10, le=600)
    pause_min_seconds: float = Field(default=4.0, ge=0, le=120)
    pause_max_seconds: float = Field(default=9.0, ge=0, le=120)

