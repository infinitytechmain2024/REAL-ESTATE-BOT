"""Environment settings for the campaign-runner service."""

from __future__ import annotations

from pydantic import Field, PositiveInt
from pydantic_settings import BaseSettings, SettingsConfigDict

from bot.orchestra.store import SafetyLimits

from .runner import RunnerConfig


class CampaignRunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(validation_alias="DATABASE_URL")
    telegram_token: str = Field(min_length=1, validation_alias="TELEGRAM_TOKEN")
    # Discovery searches Facebook through the Browser Session Manager; without a token it stays off.
    browser_url: str = Field(default="http://browser:8090", validation_alias="BROWSER_SESSION_URL")
    browser_token: str = Field(default="", validation_alias="BROWSER_SESSION_API_TOKEN")
    discovery_max_posts: PositiveInt = Field(default=15, ge=1, le=20, validation_alias="FACEBOOK_COLLECTOR_MAX_POSTS_PER_GROUP")
    discovery_group_timeout_seconds: PositiveInt = Field(default=90, ge=35, le=600, validation_alias="FACEBOOK_COLLECTOR_GROUP_TIMEOUT_SECONDS")

    # Owners see the technical campaign status; everyone else only user-safe labels.
    operator_ids_raw: str = Field(default="", validation_alias="TELEGRAM_OPERATOR_IDS")

    poll_seconds: PositiveInt = Field(default=10, ge=2, le=300, validation_alias="CAMPAIGN_POLL_SECONDS")
    window_cooldown_seconds: int = Field(default=120, ge=30, le=86_400, validation_alias="CAMPAIGN_WINDOW_COOLDOWN_SECONDS")
    analysis_grace_seconds: int = Field(default=600, ge=0, le=7_200, validation_alias="CAMPAIGN_ANALYSIS_GRACE_SECONDS")
    refusal_retry_seconds: int = Field(default=300, ge=30, le=86_400, validation_alias="CAMPAIGN_REFUSAL_RETRY_SECONDS")

    # The same safety limits (and variable names) as the Orchestra's /run.
    facebook_batches_per_day: int = Field(default=6, ge=1, le=48, validation_alias="SAFETY_MAX_FACEBOOK_BATCHES_PER_DAY")
    facebook_groups_per_day: int = Field(default=60, ge=1, le=500, validation_alias="SAFETY_MAX_FACEBOOK_GROUPS_PER_DAY")
    runs_per_day: int = Field(default=40, ge=1, le=500, validation_alias="SAFETY_MAX_RUNS_PER_DAY")
    breaker_failures: int = Field(default=3, ge=1, le=20, validation_alias="SAFETY_BREAKER_FAILURES")
    breaker_challenges: int = Field(default=2, ge=1, le=20, validation_alias="SAFETY_BREAKER_CHALLENGES")
    breaker_window_hours: int = Field(default=6, ge=1, le=72, validation_alias="SAFETY_BREAKER_WINDOW_HOURS")

    # Social network search (bot/social_search): off unless platforms are listed.
    social_platforms_raw: str = Field(default="", validation_alias="SOCIAL_SEARCH_PLATFORMS")
    social_queries_per_day: int = Field(default=20, ge=1, le=200, validation_alias="SOCIAL_SEARCH_QUERIES_PER_DAY")
    social_items_per_query: int = Field(default=12, ge=1, le=30, validation_alias="SOCIAL_SEARCH_ITEMS_PER_QUERY")
    social_queries_per_round: int = Field(default=4, ge=1, le=10, validation_alias="SOCIAL_SEARCH_QUERIES_PER_ROUND")
    social_max_rounds: int = Field(default=3, ge=1, le=20, validation_alias="SOCIAL_SEARCH_MAX_ROUNDS")
    social_pause_seconds: int = Field(default=120, ge=20, le=3600, validation_alias="SOCIAL_SEARCH_PAUSE_SECONDS")
    social_jitter_seconds: int = Field(default=90, ge=0, le=3600, validation_alias="SOCIAL_SEARCH_JITTER_SECONDS")
    social_detail_per_query: int = Field(default=4, ge=0, le=10, validation_alias="SOCIAL_SEARCH_OPEN_POSTS_PER_QUERY")
    social_scrolls: int = Field(default=2, ge=1, le=5, validation_alias="SOCIAL_SEARCH_SCROLLS")
    social_reuse_days: int = Field(default=7, ge=0, le=90, validation_alias="SOCIAL_SEARCH_QUERY_REUSE_DAYS")
    social_cooldown_hours: int = Field(default=6, ge=1, le=168, validation_alias="SOCIAL_SEARCH_RATE_LIMIT_COOLDOWN_HOURS")
    social_poll_seconds: int = Field(default=15, ge=5, le=600, validation_alias="SOCIAL_SEARCH_POLL_SECONDS")
    social_grace_seconds: int = Field(default=1800, ge=0, le=86_400, validation_alias="CAMPAIGN_SOCIAL_GRACE_SECONDS")
    # AI queries use the same OpenRouter key as the analysis; without it the deterministic fallback is used.
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY", repr=False)
    social_model: str = Field(default="openai/gpt-4o-mini", validation_alias="OPENROUTER_SOCIAL_MODEL")
    social_model_timeout_seconds: int = Field(default=20, ge=1, le=120, validation_alias="OPENROUTER_SOCIAL_TIMEOUT_SECONDS")

    # The AI relevance check of each campaign finding (bot/campaign/relevance.py); 0 calls: rules only.
    relevance_model: str = Field(default="openai/gpt-4o-mini", validation_alias="OPENROUTER_MATCH_MODEL")
    relevance_timeout_seconds: int = Field(default=15, ge=1, le=120, validation_alias="OPENROUTER_MATCH_TIMEOUT_SECONDS")
    relevance_max_calls: int = Field(default=200, ge=0, le=10_000, validation_alias="CAMPAIGN_RELEVANCE_MAX_CALLS")

    def social_config(self):  # -> bot.social_search.worker.SocialConfig (imported lazily)
        from bot.social_search.worker import SocialConfig, parse_platforms

        return SocialConfig(
            platforms=parse_platforms(self.social_platforms_raw), queries_per_day=self.social_queries_per_day,
            items_per_query=self.social_items_per_query, queries_per_round=self.social_queries_per_round,
            max_rounds=self.social_max_rounds, pause_seconds=float(self.social_pause_seconds),
            jitter_seconds=float(self.social_jitter_seconds), detail_per_query=self.social_detail_per_query,
            scrolls=self.social_scrolls, reuse_days=self.social_reuse_days,
            rate_limit_cooldown_seconds=self.social_cooldown_hours * 3600.0,
        )

    def safety_limits(self) -> SafetyLimits:
        return SafetyLimits(
            facebook_batches_per_day=self.facebook_batches_per_day, facebook_groups_per_day=self.facebook_groups_per_day,
            runs_per_day=self.runs_per_day, breaker_failures=self.breaker_failures,
            breaker_challenges=self.breaker_challenges, breaker_window_hours=self.breaker_window_hours,
        )

    def runner_config(self) -> RunnerConfig:
        return RunnerConfig(window_cooldown_seconds=self.window_cooldown_seconds,
                            analysis_grace_seconds=self.analysis_grace_seconds,
                            refusal_retry_seconds=self.refusal_retry_seconds,
                            social_grace_seconds=self.social_grace_seconds,
                            max_relevance_calls=self.relevance_max_calls)


    def owner_ids(self) -> frozenset[int]:
        """``123, 456`` -> owners; a typo stops startup rather than showing users technical text."""
        ids: set[int] = set()
        for part in self.operator_ids_raw.replace(",", " ").split():
            if not part.isdigit():
                raise ValueError(f"TELEGRAM_OPERATOR_IDS must be numeric Telegram user IDs, got {part!r}")
            ids.add(int(part))
        return frozenset(ids)
