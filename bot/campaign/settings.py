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
    # A group whose newest post is this many days older than its last read is dead and skipped (re-read after 30 days).
    facebook_group_dead_days: int = Field(default=7, ge=1, le=365, validation_alias="FACEBOOK_GROUP_DEAD_DAYS")
    breaker_window_hours: int = Field(default=6, ge=1, le=72, validation_alias="SAFETY_BREAKER_WINDOW_HOURS")

    # Social network search (bot/social_search): off unless platforms are listed.
    social_platforms_raw: str = Field(default="instagram,tiktok,linkedin", validation_alias="SOCIAL_SEARCH_PLATFORMS")
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

    # The LLM-written search plan of a campaign (bot/campaign/search_plan.py); no key: the old behaviour.
    plan_model: str = Field(default="anthropic/claude-sonnet-4.5", validation_alias="OPENROUTER_PLAN_MODEL")
    plan_timeout_seconds: int = Field(default=45, ge=5, le=300, validation_alias="OPENROUTER_PLAN_TIMEOUT_SECONDS")
    plan_enabled: bool = Field(default=True, validation_alias="CAMPAIGN_SEARCH_PLAN_ENABLED")

    def search_planner(self):  # -> bot.campaign.search_plan.OpenRouterSearchPlanner | None
        from .search_plan import OpenRouterSearchPlanner

        if not self.plan_enabled or not self.openrouter_api_key:
            return None
        return OpenRouterSearchPlanner(api_key=self.openrouter_api_key, model=self.plan_model,
                                       timeout_seconds=float(self.plan_timeout_seconds))

    # The AI relevance check of each campaign finding (bot/campaign/relevance.py); 0 calls: rules only.
    relevance_model: str = Field(default="openai/gpt-4o-mini", validation_alias="OPENROUTER_MATCH_MODEL")
    relevance_timeout_seconds: int = Field(default=15, ge=1, le=120, validation_alias="OPENROUTER_MATCH_TIMEOUT_SECONDS")
    relevance_max_calls: int = Field(default=2000, ge=0, le=10_000, validation_alias="CAMPAIGN_RELEVANCE_MAX_CALLS")
    # No AI verdict (cap reached, model down, no key): an exact finding is held as similar instead of sent.
    relevance_fail_closed: bool = Field(default=True, validation_alias="CAMPAIGN_RELEVANCE_FAIL_CLOSED")
    # A failed AI call is retried on later steps; after this many misses the finding is held as unverified.
    relevance_retry_limit: int = Field(default=5, ge=1, le=100, validation_alias="CAMPAIGN_RELEVANCE_RETRY_LIMIT")

    # Investor leads from the comments under sent Facebook posts (bot/campaign/leads.py).
    # all: every campaign; investors: investor campaigns only; off: never read comments.
    comment_leads: str = Field(default="all", pattern="^(all|investors|off)$", validation_alias="CAMPAIGN_COMMENT_LEADS")
    comment_max_posts: int = Field(default=15, ge=0, le=100, validation_alias="CAMPAIGN_COMMENT_MAX_POSTS")
    # An investor search sends the people stored from comments in its city (at most, seen within days).
    lead_people_max: int = Field(default=60, ge=0, le=500, validation_alias="CAMPAIGN_LEAD_PEOPLE_MAX")
    lead_days: int = Field(default=90, ge=1, le=3650, validation_alias="CAMPAIGN_LEAD_DAYS")
    comment_reads_per_day: int = Field(default=40, ge=0, le=500, validation_alias="SAFETY_MAX_FACEBOOK_COMMENT_READS_PER_DAY")
    comment_reads_per_round: int = Field(default=3, ge=1, le=10, validation_alias="CAMPAIGN_COMMENT_READS_PER_ROUND")
    comment_poll_seconds: int = Field(default=30, ge=5, le=600, validation_alias="CAMPAIGN_COMMENT_POLL_SECONDS")
    leads_model: str = Field(default="openai/gpt-4o-mini", validation_alias="OPENROUTER_LEADS_MODEL")
    leads_timeout_seconds: int = Field(default=30, ge=5, le=120, validation_alias="OPENROUTER_LEADS_TIMEOUT_SECONDS")

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
                            max_relevance_calls=self.relevance_max_calls,
                            relevance_fail_closed=self.relevance_fail_closed,
                            relevance_retry_limit=self.relevance_retry_limit,
                            comment_leads=self.comment_leads, comment_max_posts=self.comment_max_posts,
                            max_people=self.lead_people_max, lead_days=self.lead_days)

    # Investor reach across platforms through the search engines (bot/campaign/reach.py).
    reach_enabled: bool = Field(default=True, validation_alias="INVESTOR_REACH_ENABLED")
    reach_queries_per_campaign: int = Field(default=16, ge=1, le=100, validation_alias="INVESTOR_REACH_QUERIES_PER_CAMPAIGN")
    reach_queries_per_tick: int = Field(default=2, ge=1, le=10, validation_alias="INVESTOR_REACH_QUERIES_PER_TICK")
    reach_queries_per_day: int = Field(default=80, ge=0, le=2000, validation_alias="INVESTOR_REACH_QUERIES_PER_DAY")
    reach_poll_seconds: int = Field(default=20, ge=5, le=600, validation_alias="INVESTOR_REACH_POLL_SECONDS")
    reach_model: str = Field(default="openai/gpt-4o-mini", validation_alias="OPENROUTER_REACH_MODEL")
    reach_model_queries: int = Field(default=10, ge=0, le=30, validation_alias="INVESTOR_REACH_MODEL_QUERIES")

    def reach_config(self):  # -> bot.campaign.reach.ReachConfig
        from .reach import ReachConfig

        return ReachConfig(queries_per_campaign=self.reach_queries_per_campaign,
                           queries_per_tick=self.reach_queries_per_tick, queries_per_day=self.reach_queries_per_day,
                           model_queries=self.reach_model_queries)

    def comment_config(self):  # -> bot.campaign.leads.CommentConfig
        from .leads import CommentConfig

        return CommentConfig(reads_per_day=self.comment_reads_per_day, reads_per_round=self.comment_reads_per_round)


    def owner_ids(self) -> frozenset[int]:
        """``123, 456`` -> owners; a typo stops startup rather than showing users technical text."""
        ids: set[int] = set()
        for part in self.operator_ids_raw.replace(",", " ").split():
            if not part.isdigit():
                raise ValueError(f"TELEGRAM_OPERATOR_IDS must be numeric Telegram user IDs, got {part!r}")
            ids.add(int(part))
        return frozenset(ids)
