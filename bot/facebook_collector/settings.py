"""Conservative runtime settings for the Facebook batch collector."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import Field, PositiveInt, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from .apify.provider import ApifyFacebookConfig


class FacebookCollectorSettings(BaseSettings):
    # Tunables read FACEBOOK_COLLECTOR_*; the shared values keep explicit aliases.
    model_config = SettingsConfigDict(env_file=".env", env_prefix="FACEBOOK_COLLECTOR_", extra="ignore")

    database_url: str = Field(validation_alias="DATABASE_URL")
    browser_url: str = Field(default="http://browser:8090", validation_alias="BROWSER_SESSION_URL")
    browser_token: str = Field(min_length=24, validation_alias="BROWSER_SESSION_API_TOKEN")
    max_groups: PositiveInt = Field(default=20, ge=1, le=20)
    max_posts_per_group: PositiveInt = Field(default=15, ge=1, le=20)
    # At least navigation (>=5 s) plus snapshot overhead (25 s) plus slack.
    group_timeout_seconds: PositiveInt = Field(default=90, ge=35, le=600)
    pause_min_seconds: float = Field(default=4.0, ge=0, le=120)
    pause_max_seconds: float = Field(default=9.0, ge=0, le=120)
    # facebook-runner: how often it looks for batches resumed after verification.
    runner_poll_seconds: PositiveInt = Field(default=15, ge=5, le=300)

    # Apify provider of Facebook batches (bot/facebook_collector/apify): public groups only, no cookies, no account,
    # never joins a group. Off by default; on, it needs a token and a pinned actor build or the service does not start.
    apify_enabled: bool = Field(default=False, validation_alias="FEATURE_APIFY_FACEBOOK")
    apify_token: str = Field(default="", repr=False, validation_alias="APIFY_TOKEN")
    apify_actor_id: str = Field(default="apify/facebook-groups-scraper", validation_alias="APIFY_FACEBOOK_ACTOR_ID")
    apify_actor_build: str = Field(default="", max_length=40, validation_alias="APIFY_FACEBOOK_ACTOR_BUILD")
    apify_max_posts_per_run: int = Field(default=500, ge=1, le=5000, validation_alias="APIFY_FACEBOOK_MAX_POSTS_PER_RUN")
    apify_max_usd_per_campaign: float = Field(default=10.0, gt=0, le=1000, validation_alias="APIFY_FACEBOOK_MAX_USD_PER_CAMPAIGN")
    apify_max_usd_per_run: float = Field(default=2.0, gt=0, le=1000, validation_alias="APIFY_FACEBOOK_MAX_USD_PER_RUN")
    apify_timeout_seconds: int = Field(default=600, ge=30, le=3600, validation_alias="APIFY_FACEBOOK_TIMEOUT_SECONDS")
    # campaign: only campaigns with acquisition_provider='apify' (migration 046); all: every campaign's batches.
    apify_scope: Literal["campaign", "all"] = Field(default="campaign", validation_alias="APIFY_FACEBOOK_SCOPE")
    apify_raw_retention_days: int = Field(default=7, ge=1, le=365, validation_alias="APIFY_FACEBOOK_RAW_RETENTION_DAYS")
    # The campaign budget every service books against (bot/utils/costs.py); 0: no limit.
    campaign_budget_usd: float = Field(default=5.0, ge=0, le=10_000, validation_alias="CAMPAIGN_BUDGET_USD")

    @model_validator(mode="after")
    def _apify_ready(self) -> FacebookCollectorSettings:
        """FEATURE_APIFY_FACEBOOK=true without what it needs is a start-up error, never a silent fallback."""
        if self.apify_enabled:
            missing = [name for name, value in (("APIFY_TOKEN", self.apify_token),
                                                ("APIFY_FACEBOOK_ACTOR_BUILD", self.apify_actor_build)) if not value.strip()]
            if missing:
                raise ValueError(f"FEATURE_APIFY_FACEBOOK=true needs {', '.join(missing)}")
            self.apify_config()  # an unpinned build or unsafe limits fail here too
        return self

    def apify_config(self) -> ApifyFacebookConfig | None:
        """The Apify provider's configuration, or None while FEATURE_APIFY_FACEBOOK is off."""
        if not self.apify_enabled:
            return None
        from .apify.provider import ApifyFacebookConfig

        return ApifyFacebookConfig(actor_id=self.apify_actor_id, build=self.apify_actor_build.strip(),
                                   max_posts_per_run=self.apify_max_posts_per_run,
                                   max_usd_per_campaign=self.apify_max_usd_per_campaign,
                                   max_usd_per_run=self.apify_max_usd_per_run, timeout_seconds=self.apify_timeout_seconds)

