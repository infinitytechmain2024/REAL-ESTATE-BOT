"""Environment settings for the campaign-runner service."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, PositiveInt, model_validator
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

    # apify: the browser social search is off (portals via Apify first); legacy: as before.
    acquisition_mode: Literal["legacy", "apify"] = Field(default="legacy", validation_alias="ACQUISITION_MODE")

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
    # A TOTAL deadline for the planner call (both posts); it runs inside a web tick, so it must stay well under the
    # 300 s web lease: hence le=90.
    plan_timeout_seconds: int = Field(default=45, ge=5, le=90, validation_alias="OPENROUTER_PLAN_TIMEOUT_SECONDS")
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
    # H11: a finding whose must-have criterion is not mentioned is held and never offered as «similar».
    mandatory_unknown_hold: bool = Field(default=False, validation_alias="CAMPAIGN_MANDATORY_UNKNOWN_HOLD")
    # A failed AI call is retried on later steps; after this many misses the finding is held as unverified.
    relevance_retry_limit: int = Field(default=5, ge=1, le=100, validation_alias="CAMPAIGN_RELEVANCE_RETRY_LIMIT")
    # Auction / nuda propiedad / usufructo in a purchase listing: never exact, held as «similar» («≈ Отличается: аукцион»).
    auction_deviation: bool = Field(default=False, validation_alias="CAMPAIGN_AUCTION_DEVIATION")
    # Who judges a finding against the task: the reviewer (a strong model, a hard-criteria matrix with a quote each,
    # bot/agents/reviewer.py) or the legacy one-word judge above. The caps and fail-closed rules apply to both.
    judge: str = Field(default="reviewer", pattern="^(reviewer|legacy)$", validation_alias="CAMPAIGN_JUDGE")
    review_model: str = Field(default="anthropic/claude-sonnet-4.5", validation_alias="OPENROUTER_REVIEW_MODEL")
    review_timeout_seconds: int = Field(default=45, ge=5, le=300, validation_alias="OPENROUTER_REVIEW_TIMEOUT_SECONDS")
    # Reviewer attempts per campaign (failed calls count): past it the finding is held as unverified.
    review_max_calls: int = Field(default=300, ge=1, le=10_000, validation_alias="CAMPAIGN_REVIEW_MAX_CALLS")
    # The most one campaign may spend in USD (LLM, scrape API, paid search, listing APIs), summed over every service
    # from the campaign_costs ledger (bot/utils/costs.py). Reached: the web stage stops (budget_cap), the analysis
    # worker and the AI check skip the campaign's findings, the report says so. 0: no limit.
    budget_usd: float = Field(default=5.0, ge=0, le=10_000, validation_alias="CAMPAIGN_BUDGET_USD")
    # The user's final report when a search ends (bot/campaign/final_report.py); the recommendations come from this model.
    final_report_enabled: bool = Field(default=True, validation_alias="CAMPAIGN_FINAL_REPORT")
    final_model: str = Field(default="anthropic/claude-sonnet-4.5", validation_alias="OPENROUTER_FINAL_MODEL")
    final_timeout_seconds: int = Field(default=60, ge=5, le=300, validation_alias="OPENROUTER_FINAL_TIMEOUT_SECONDS")

    def relevance_judge(self):  # -> bot.campaign.relevance.RelevanceJudge | None
        """The campaign judge: the reviewer (``CAMPAIGN_JUDGE=reviewer``, investors go to the legacy judge) or the legacy
        judge; None without a key or with ``CAMPAIGN_RELEVANCE_MAX_CALLS=0`` (the rules decide alone)."""
        from .relevance import OpenRouterRelevanceJudge, ReviewerJudge

        if not self.openrouter_api_key or self.relevance_max_calls <= 0:
            return None
        legacy = OpenRouterRelevanceJudge(api_key=self.openrouter_api_key, model=self.relevance_model,
                                          timeout_seconds=self.relevance_timeout_seconds)
        if self.judge == "legacy":
            return legacy
        from bot.agents.reviewer import OpenRouterReviewer

        return ReviewerJudge(OpenRouterReviewer(api_key=self.openrouter_api_key, model=self.review_model,
                                                timeout_seconds=float(self.review_timeout_seconds)), legacy,
                             max_calls=self.review_max_calls)

    def final_reporter(self):  # -> bot.campaign.final_report.FinalReporter | None
        from .final_report import FinalReporter, OpenRouterRecommender

        if not self.final_report_enabled:
            return None
        recommender = (OpenRouterRecommender(api_key=self.openrouter_api_key, model=self.final_model,
                                             timeout_seconds=float(self.final_timeout_seconds))
                       if self.openrouter_api_key else None)
        return FinalReporter(recommender)

    # Tell the chat about closed Facebook groups (Apify group_private) and read them after «Вступил».
    join_notices: bool = Field(default=False, validation_alias="CAMPAIGN_JOIN_NOTICES")
    # One card for one object listed without a street address (same type, deal, district, price ±1 %, area ±2 %).
    dedup_without_address: bool = Field(default=False, validation_alias="DEDUP_WITHOUT_ADDRESS")
    # Investor leads from the comments under sent Facebook posts (bot/campaign/leads.py).
    # all: every campaign; investors (default): investor campaigns only; off: never read comments.
    comment_leads: str = Field(default="investors", pattern="^(all|investors|off)$", validation_alias="CAMPAIGN_COMMENT_LEADS")
    comment_max_posts: int = Field(default=15, ge=0, le=100, validation_alias="CAMPAIGN_COMMENT_MAX_POSTS")
    # An investor search sends the people stored from comments in its city (at most, seen within days).
    lead_people_max: int = Field(default=60, ge=0, le=500, validation_alias="CAMPAIGN_LEAD_PEOPLE_MAX")
    lead_days: int = Field(default=90, ge=1, le=3650, validation_alias="CAMPAIGN_LEAD_DAYS")
    comment_reads_per_day: int = Field(default=40, ge=0, le=500, validation_alias="SAFETY_MAX_FACEBOOK_COMMENT_READS_PER_DAY")
    comment_reads_per_round: int = Field(default=3, ge=1, le=10, validation_alias="CAMPAIGN_COMMENT_READS_PER_ROUND")
    comment_poll_seconds: int = Field(default=30, ge=5, le=600, validation_alias="CAMPAIGN_COMMENT_POLL_SECONDS")
    leads_model: str = Field(default="openai/gpt-4o-mini", validation_alias="OPENROUTER_LEADS_MODEL")
    leads_timeout_seconds: int = Field(default=30, ge=5, le=120, validation_alias="OPENROUTER_LEADS_TIMEOUT_SECONDS")

    # Comments under ONE Facebook post sent by link, through Apify (bot/campaign/post_leads.py, v2 B1). Off by default;
    # on, it needs APIFY_TOKEN and a pinned actor build or the service does not start. The same flag turns on the link
    # intake in the telegram service.
    apify_comments_enabled: bool = Field(default=False, validation_alias="APIFY_FACEBOOK_COMMENTS_ENABLED")
    apify_token: str = Field(default="", repr=False, validation_alias="APIFY_TOKEN")
    apify_comments_actor: str = Field(default="apify/facebook-comments-scraper", validation_alias="APIFY_FACEBOOK_COMMENTS_ACTOR")
    apify_comments_build: str = Field(default="", max_length=40, validation_alias="APIFY_FACEBOOK_COMMENTS_ACTOR_BUILD")
    apify_posts_actor: str = Field(default="", validation_alias="APIFY_FACEBOOK_POSTS_ACTOR")
    apify_comments_max: int = Field(default=200, ge=1, le=1000, validation_alias="APIFY_FACEBOOK_COMMENTS_MAX_PER_POST")
    apify_comments_max_usd: float = Field(default=1.0, gt=0, le=100, validation_alias="APIFY_FACEBOOK_COMMENTS_MAX_USD_PER_POST")
    apify_comments_timeout_seconds: int = Field(default=300, ge=30, le=3600, validation_alias="APIFY_FACEBOOK_COMMENTS_TIMEOUT_SECONDS")
    # Who reads the comments under sent objects (v2 B2): browser (default, unchanged) or apify (needs the flag above,
    # else a warning and browser). Its day quota is its own: SAFETY_MAX_FACEBOOK_COMMENT_READS_PER_DAY is the browser's.
    leads_provider: str = Field(default="browser", pattern="^(browser|apify)$", validation_alias="LEADS_PROVIDER")
    apify_comments_max_per_day: int = Field(default=40, ge=0, le=100_000, validation_alias="APIFY_FACEBOOK_COMMENTS_MAX_PER_DAY")

    # Instagram through Apify (v3 P1.2, bot/acquisition/apify_instagram.py): posts and the people under them, no
    # account. Off by default; on, it needs APIFY_TOKEN and a pinned build or the service does not start. Runs in
    # the campaign only with ACQUISITION_MODE=apify (P1.8, bot/campaign/social_apify.py).
    apify_instagram_enabled: bool = Field(default=False, validation_alias="APIFY_INSTAGRAM_ENABLED")
    apify_instagram_posts_actor: str = Field(default="apify/instagram-scraper", validation_alias="APIFY_INSTAGRAM_POSTS_ACTOR")
    apify_instagram_comments_actor: str = Field(default="apify/instagram-comment-scraper", validation_alias="APIFY_INSTAGRAM_COMMENTS_ACTOR")
    apify_instagram_build: str = Field(default="", max_length=40, validation_alias="APIFY_INSTAGRAM_ACTOR_BUILD")
    apify_instagram_max_results: int = Field(default=30, ge=1, le=50, validation_alias="APIFY_INSTAGRAM_MAX_RESULTS")
    apify_instagram_max_charge_usd: float = Field(default=0.20, gt=0, le=5, validation_alias="APIFY_INSTAGRAM_MAX_CHARGE_USD")
    # TikTok through Apify (v3 P1.3, bot/acquisition/apify_tiktok.py): the same rules as Instagram above.
    apify_tiktok_enabled: bool = Field(default=False, validation_alias="APIFY_TIKTOK_ENABLED")
    apify_tiktok_posts_actor: str = Field(default="clockworks/tiktok-scraper", validation_alias="APIFY_TIKTOK_POSTS_ACTOR")
    apify_tiktok_comments_actor: str = Field(default="clockworks/tiktok-comments-scraper", validation_alias="APIFY_TIKTOK_COMMENTS_ACTOR")
    apify_tiktok_build: str = Field(default="", max_length=40, validation_alias="APIFY_TIKTOK_ACTOR_BUILD")
    apify_tiktok_max_results: int = Field(default=30, ge=1, le=50, validation_alias="APIFY_TIKTOK_MAX_RESULTS")
    # clockworks/tiktok-scraper refuses a cap below $0.50 (apify_tiktok.TIKTOK_MIN_CHARGE_USD, live sample 2026-11-11):
    # the default is that minimum; MAX_RESULTS, not the cap, bounds what a run spends.
    apify_tiktok_max_charge_usd: float = Field(default=0.50, gt=0, le=5, validation_alias="APIFY_TIKTOK_MAX_CHARGE_USD")
    # The comments actor's own cap: its minimum is not measured; on a 4xx refusal or apify_cap_too_low, raise it.
    apify_tiktok_comments_max_charge_usd: float = Field(default=0.15, gt=0, le=5,
                                                        validation_alias="APIFY_TIKTOK_COMMENTS_MAX_CHARGE_USD")
    # Instagram and TikTok targets (hashtags or words, one per paid run) per campaign in all, with ACQUISITION_MODE=apify
    # and a flag above (v3 P1.8, bot/campaign/social_apify.py); fewer when the run caps do not fit CAMPAIGN_BUDGET_USD.
    apify_social_max_targets: int = Field(default=3, ge=1, le=10, validation_alias="APIFY_SOCIAL_MAX_TARGETS")

    @model_validator(mode="after")
    def _comments_ready(self) -> CampaignRunnerSettings:
        """APIFY_FACEBOOK_COMMENTS_ENABLED=true without what it needs is a start-up error, never a silent fallback."""
        if self.apify_comments_enabled:
            missing = [name for name, value in (("APIFY_TOKEN", self.apify_token),
                                                ("APIFY_FACEBOOK_COMMENTS_ACTOR_BUILD", self.apify_comments_build))
                       if not value.strip()]
            if missing:
                raise ValueError(f"APIFY_FACEBOOK_COMMENTS_ENABLED=true needs {', '.join(missing)}")
            self.post_reads_config()  # an unpinned build or unsafe limits fail here too
        return self

    @model_validator(mode="after")
    def _social_ready(self) -> CampaignRunnerSettings:
        """APIFY_INSTAGRAM_ENABLED / APIFY_TIKTOK_ENABLED=true without a token or a pinned build is a start-up error,
        never a silent fallback."""
        for flag, build_name, enabled, build in self._social_flags():
            if not enabled:
                continue
            missing = [name for name, value in (("APIFY_TOKEN", self.apify_token), (build_name, build))
                       if not value.strip()]
            if missing:
                raise ValueError(f"{flag}=true needs {', '.join(missing)}")
        self._social_caps_fit()
        self.instagram_config()  # an unpinned build («latest») or unsafe limits fail here too
        self.tiktok_config()
        return self

    def _social_caps_fit(self) -> None:
        """A run of MAX_RESULTS must fit in its cap at the actor's price (else the cap, not the limit, ends it)."""
        from bot.acquisition import apify_instagram, apify_tiktok

        for prefix, enabled, results, cap, prices in (
                ("APIFY_INSTAGRAM", self.apify_instagram_enabled, self.apify_instagram_max_results,
                 self.apify_instagram_max_charge_usd, apify_instagram),
                ("APIFY_TIKTOK", self.apify_tiktok_enabled, self.apify_tiktok_max_results,
                 self.apify_tiktok_max_charge_usd, apify_tiktok)):
            need = results * prices.USD_PER_RESULT + prices.START_USD
            if enabled and need > cap + 1e-9:
                raise ValueError(f"{prefix}_MAX_RESULTS={results} costs up to ${need:.4f} per run, above "
                                 f"{prefix}_MAX_CHARGE_USD={cap}: lower the results or raise the cap")
        if self.apify_tiktok_enabled and self.apify_tiktok_max_charge_usd < apify_tiktok.TIKTOK_MIN_CHARGE_USD - 1e-9:
            raise ValueError(f"APIFY_TIKTOK_MAX_CHARGE_USD={self.apify_tiktok_max_charge_usd} is below the "
                             f"${apify_tiktok.TIKTOK_MIN_CHARGE_USD:.2f} minimum of {apify_tiktok.POSTS_ACTOR}: "
                             "every launch would be refused")
        need = self.apify_tiktok_max_results * apify_tiktok.COMMENTS_USD_PER_RESULT
        if self.apify_tiktok_enabled and need > self.apify_tiktok_comments_max_charge_usd + 1e-9:
            raise ValueError(f"APIFY_TIKTOK_MAX_RESULTS={self.apify_tiktok_max_results} comments cost up to "
                             f"${need:.4f} per run, above APIFY_TIKTOK_COMMENTS_MAX_CHARGE_USD="
                             f"{self.apify_tiktok_comments_max_charge_usd}: lower the results or raise the cap")

    def _social_flags(self) -> list[tuple[str, str, bool, str]]:
        return [("APIFY_INSTAGRAM_ENABLED", "APIFY_INSTAGRAM_ACTOR_BUILD", self.apify_instagram_enabled,
                 self.apify_instagram_build),
                ("APIFY_TIKTOK_ENABLED", "APIFY_TIKTOK_ACTOR_BUILD", self.apify_tiktok_enabled, self.apify_tiktok_build)]

    def instagram_config(self):  # -> bot.acquisition.apify_social.SocialSourceConfig | None
        """The Instagram sources' configuration, or None while APIFY_INSTAGRAM_ENABLED is off."""
        if not self.apify_instagram_enabled:
            return None
        from bot.acquisition.apify_social import SocialSourceConfig

        return SocialSourceConfig(platform="instagram", posts_actor=self.apify_instagram_posts_actor.strip(),
                                  comments_actor=self.apify_instagram_comments_actor.strip(),
                                  build=self.apify_instagram_build.strip(),
                                  max_results=self.apify_instagram_max_results,
                                  max_charge_usd=self.apify_instagram_max_charge_usd,
                                  build_setting="APIFY_INSTAGRAM_ACTOR_BUILD")

    def instagram_sources(self, store, http, **kw):  # -> bot.acquisition.apify_instagram.InstagramSources | None
        """The Instagram posts and comments sources on ``store`` and ``http``; None while the flag is off."""
        config = self.instagram_config()
        if config is None:
            return None
        from bot.acquisition.apify_instagram import instagram_sources

        return instagram_sources(config, store, http, **kw)

    def tiktok_config(self):  # -> bot.acquisition.apify_social.SocialSourceConfig | None
        """The TikTok sources' configuration, or None while APIFY_TIKTOK_ENABLED is off."""
        if not self.apify_tiktok_enabled:
            return None
        from bot.acquisition.apify_social import SocialSourceConfig

        return SocialSourceConfig(platform="tiktok", posts_actor=self.apify_tiktok_posts_actor.strip(),
                                  comments_actor=self.apify_tiktok_comments_actor.strip(),
                                  build=self.apify_tiktok_build.strip(), max_results=self.apify_tiktok_max_results,
                                  max_charge_usd=self.apify_tiktok_max_charge_usd,
                                  comments_max_charge_usd=self.apify_tiktok_comments_max_charge_usd,
                                  build_setting="APIFY_TIKTOK_ACTOR_BUILD")

    def tiktok_sources(self, store, http, **kw):  # -> bot.acquisition.apify_tiktok.TikTokSources | None
        """The TikTok posts and comments sources on ``store`` and ``http``; None while the flag is off."""
        config = self.tiktok_config()
        if config is None:
            return None
        from bot.acquisition.apify_tiktok import tiktok_sources

        return tiktok_sources(config, store, http, **kw)

    def apify_social_on(self) -> bool:
        """Instagram / TikTok posts through Apify in the campaign (P1.8): ACQUISITION_MODE=apify and a flag."""
        return self.acquisition_mode == "apify" and (self.apify_instagram_enabled or self.apify_tiktok_enabled)

    def linkedin_apify_on(self) -> bool:
        """LinkedIn people and companies through Apify in the reach (P1.8): ACQUISITION_MODE=apify and the flag."""
        return self.acquisition_mode == "apify" and self.apify_linkedin_enabled

    def post_reads_config(self):  # -> bot.acquisition.apify_facebook_comments.CommentsConfig | None
        """The post comment reader's configuration, or None while APIFY_FACEBOOK_COMMENTS_ENABLED is off."""
        if not self.apify_comments_enabled:
            return None
        from bot.acquisition.apify_facebook_comments import CommentsConfig

        return CommentsConfig(actor_id=self.apify_comments_actor, build=self.apify_comments_build.strip(),
                              posts_actor_id=self.apify_posts_actor, max_comments=self.apify_comments_max,
                              max_usd_per_post=self.apify_comments_max_usd,
                              timeout_seconds=float(self.apify_comments_timeout_seconds))

    # People who reacted under public Facebook posts, through Apify (bot/acquisition/apify_facebook_reactions.py,
    # v3 P1.6). Off by default and not wired yet (P4); on, it needs APIFY_TOKEN and a pinned build or the service
    # does not start. The cap of one run must cover MAX_RESULTS x $0.0062.
    apify_reactions_enabled: bool = Field(default=False, validation_alias="APIFY_FACEBOOK_REACTIONS_ENABLED")
    apify_reactions_actor: str = Field(default="apify/facebook-likes-scraper",
                                       validation_alias="APIFY_FACEBOOK_REACTIONS_ACTOR")
    apify_reactions_build: str = Field(default="", max_length=40, validation_alias="APIFY_FACEBOOK_REACTIONS_ACTOR_BUILD")
    apify_reactions_max_results: int = Field(default=100, ge=1, le=500, validation_alias="APIFY_FACEBOOK_REACTIONS_MAX_RESULTS")
    apify_reactions_max_charge_usd: float = Field(default=0.70, gt=0, le=5,
                                                  validation_alias="APIFY_FACEBOOK_REACTIONS_MAX_CHARGE_USD")

    @model_validator(mode="after")
    def _reactions_ready(self) -> CampaignRunnerSettings:
        """With the flag on: no token, no pinned build or a cap below MAX_RESULTS x the result price is a start-up
        error. With it off nothing is checked, so ENABLED=false alone always rolls it back."""
        if self.apify_reactions_enabled:
            from bot.acquisition.apify_facebook_reactions import USD_PER_RESULT, min_charge_usd

            if self.apify_reactions_max_charge_usd < min_charge_usd(self.apify_reactions_max_results):
                raise ValueError("APIFY_FACEBOOK_REACTIONS_MAX_CHARGE_USD is below "
                                 f"APIFY_FACEBOOK_REACTIONS_MAX_RESULTS x {USD_PER_RESULT}")
            missing = [name for name, value in (("APIFY_TOKEN", self.apify_token),
                                                ("APIFY_FACEBOOK_REACTIONS_ACTOR_BUILD", self.apify_reactions_build))
                       if not value.strip()]
            if missing:
                raise ValueError(f"APIFY_FACEBOOK_REACTIONS_ENABLED=true needs {', '.join(missing)}")
            self.reactions_config()  # an unpinned build or a bad actor id fails here too
        return self

    def reactions_config(self):  # -> bot.acquisition.apify_facebook_reactions.ReactionsConfig | None
        """The Facebook reactions source's configuration, or None while APIFY_FACEBOOK_REACTIONS_ENABLED is off."""
        if not self.apify_reactions_enabled:
            return None
        from bot.acquisition.apify_facebook_reactions import ReactionsConfig

        return ReactionsConfig(actor_id=self.apify_reactions_actor, build=self.apify_reactions_build.strip(),
                               max_results=self.apify_reactions_max_results,
                               max_charge_usd=self.apify_reactions_max_charge_usd)

    # LinkedIn people and companies through Apify without cookies (v3 P1.4, bot/acquisition/apify_linkedin.py). Off by
    # default; on, it needs APIFY_TOKEN and a pinned build of each actor (APIFY_LINKEDIN_PEOPLE_ACTOR_BUILD,
    # APIFY_LINKEDIN_COMPANIES_ACTOR_BUILD; never «latest») or the service does not start. One run's cap:
    # MAX_PAGES x the actor's price per page + 0.05, never above MAX_CHARGE_USD.
    apify_linkedin_enabled: bool = Field(default=False, validation_alias="APIFY_LINKEDIN_ENABLED")
    apify_linkedin_people_actor: str = Field(default="harvestapi/linkedin-profile-search",
                                             validation_alias="APIFY_LINKEDIN_PEOPLE_ACTOR")
    apify_linkedin_companies_actor: str = Field(default="harvestapi/linkedin-company-search",
                                                validation_alias="APIFY_LINKEDIN_COMPANIES_ACTOR")
    apify_linkedin_people_build: str = Field(default="", max_length=40,
                                             validation_alias="APIFY_LINKEDIN_PEOPLE_ACTOR_BUILD")
    apify_linkedin_companies_build: str = Field(default="", max_length=40,
                                                validation_alias="APIFY_LINKEDIN_COMPANIES_ACTOR_BUILD")
    apify_linkedin_max_pages: int = Field(default=1, ge=1, le=4, validation_alias="APIFY_LINKEDIN_MAX_PAGES")
    apify_linkedin_usd_per_page: float = Field(default=0.10, gt=0, le=1, validation_alias="APIFY_LINKEDIN_USD_PER_PAGE")
    apify_linkedin_companies_usd_per_page: float = Field(default=0.101, gt=0, le=1,
                                                         validation_alias="APIFY_LINKEDIN_COMPANIES_USD_PER_PAGE")
    apify_linkedin_max_charge_usd: float = Field(default=0.45, gt=0, le=5, validation_alias="APIFY_LINKEDIN_MAX_CHARGE_USD")

    @model_validator(mode="after")
    def _linkedin_ready(self) -> CampaignRunnerSettings:
        """APIFY_LINKEDIN_ENABLED=true without a token or a pinned build is a start-up error."""
        if self.apify_linkedin_enabled:
            if not self.apify_token.strip():
                raise ValueError("APIFY_LINKEDIN_ENABLED=true needs APIFY_TOKEN")
            self.linkedin_config()  # an empty or «latest» build, a bad actor id or unsafe limits fail here
        return self

    def linkedin_config(self):  # -> bot.acquisition.apify_linkedin.LinkedInConfig | None
        """Both LinkedIn sources' configuration, or None while APIFY_LINKEDIN_ENABLED is off."""
        if not self.apify_linkedin_enabled:
            return None
        from bot.acquisition.apify_linkedin import LinkedInConfig

        return LinkedInConfig(people_build=self.apify_linkedin_people_build.strip(),
                              companies_build=self.apify_linkedin_companies_build.strip(),
                              people_actor=self.apify_linkedin_people_actor,
                              companies_actor=self.apify_linkedin_companies_actor,
                              max_pages=self.apify_linkedin_max_pages, usd_per_page=self.apify_linkedin_usd_per_page,
                              companies_usd_per_page=self.apify_linkedin_companies_usd_per_page,
                              max_charge_usd=self.apify_linkedin_max_charge_usd)

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
                            mandatory_unknown_hold=self.mandatory_unknown_hold,
                            relevance_retry_limit=self.relevance_retry_limit,
                            auction_deviation=self.auction_deviation,
                            comment_leads=self.comment_leads, comment_max_posts=self.comment_max_posts,
                            max_people=self.lead_people_max, lead_days=self.lead_days,
                            investor_report=self.investor_report,
                            join_notices=self.join_notices,
                            dedup_without_address=self.dedup_without_address)

    # The final report gets the «💼 Инвесторы» section (v2 B4); off: the report is as before B4.
    investor_report: bool = Field(default=False, validation_alias="CAMPAIGN_INVESTOR_REPORT")

    # Investor reach across platforms through the search engines (bot/campaign/reach.py).
    reach_enabled: bool = Field(default=True, validation_alias="INVESTOR_REACH_ENABLED")
    reach_queries_per_campaign: int = Field(default=16, ge=1, le=100, validation_alias="INVESTOR_REACH_QUERIES_PER_CAMPAIGN")
    reach_queries_per_tick: int = Field(default=2, ge=1, le=10, validation_alias="INVESTOR_REACH_QUERIES_PER_TICK")
    reach_queries_per_day: int = Field(default=80, ge=0, le=2000, validation_alias="INVESTOR_REACH_QUERIES_PER_DAY")
    reach_poll_seconds: int = Field(default=20, ge=5, le=600, validation_alias="INVESTOR_REACH_POLL_SECONDS")
    reach_model: str = Field(default="openai/gpt-4o-mini", validation_alias="OPENROUTER_REACH_MODEL")
    reach_model_queries: int = Field(default=10, ge=0, le=30, validation_alias="INVESTOR_REACH_MODEL_QUERIES")
    # Relevant reach results whose public page is opened for contacts and a description (per campaign; 0: never).
    reach_enrich_per_campaign: int = Field(default=30, ge=0, le=500, validation_alias="INVESTOR_REACH_ENRICH_PER_CAMPAIGN")

    def reach_config(self):  # -> bot.campaign.reach.ReachConfig
        from .reach import ReachConfig

        return ReachConfig(queries_per_campaign=self.reach_queries_per_campaign,
                           queries_per_tick=self.reach_queries_per_tick, queries_per_day=self.reach_queries_per_day,
                           model_queries=self.reach_model_queries,
                           enrich_per_campaign=self.reach_enrich_per_campaign,
                           acquisition_mode=self.acquisition_mode, linkedin_apify=self.linkedin_apify_on())

    def comment_config(self):  # -> bot.campaign.leads.CommentConfig
        from .leads import CommentConfig

        return CommentConfig(reads_per_day=self.comment_reads_per_day, reads_per_round=self.comment_reads_per_round,
                             apify_reads_per_day=self.apify_comments_max_per_day)


    def owner_ids(self) -> frozenset[int]:
        """``123, 456`` -> owners; a typo stops startup rather than showing users technical text."""
        ids: set[int] = set()
        for part in self.operator_ids_raw.replace(",", " ").split():
            if not part.isdigit():
                raise ValueError(f"TELEGRAM_OPERATOR_IDS must be numeric Telegram user IDs, got {part!r}")
            ids.add(int(part))
        return frozenset(ids)
