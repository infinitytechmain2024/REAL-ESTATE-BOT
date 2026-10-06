"""Environment settings of the web stage (read by the campaign-runner service)."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .worker import WebSearchConfig


class WebSearchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    enabled: bool = Field(default=True, validation_alias="WEB_SEARCH_ENABLED")
    searxng_url: str = Field(default="http://searxng:8080", validation_alias="WEB_SEARCH_SEARXNG_URL")
    searxng_timeout_seconds: float = Field(default=25, ge=3, le=120, validation_alias="WEB_SEARCH_SEARXNG_TIMEOUT_SECONDS")
    poll_seconds: float = Field(default=15, ge=2, le=600, validation_alias="WEB_SEARCH_POLL_SECONDS")

    # Query generation: OpenRouter with the analysis key; without it, deterministic templates.
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")
    query_model: str = Field(default="openai/gpt-4o-mini", validation_alias="OPENROUTER_WEB_QUERY_MODEL")
    query_timeout_seconds: float = Field(default=30, ge=3, le=120, validation_alias="OPENROUTER_WEB_QUERY_TIMEOUT_SECONDS")

    queries_per_round: int = Field(default=12, ge=1, le=30, validation_alias="WEB_SEARCH_QUERIES_PER_ROUND")
    max_queries_per_campaign: int = Field(default=80, ge=1, le=200, validation_alias="WEB_SEARCH_MAX_QUERIES_PER_CAMPAIGN")
    results_per_query: int = Field(default=30, ge=1, le=30, validation_alias="WEB_SEARCH_RESULTS_PER_QUERY")
    pages_per_query: int = Field(default=2, ge=1, le=5, validation_alias="WEB_SEARCH_PAGES_PER_QUERY")
    # Hours a query another campaign searched blocks the same query (0: never; a campaign never repeats its own).
    query_reuse_hours: int = Field(default=0, ge=0, le=720, validation_alias="WEB_SEARCH_QUERY_REUSE_HOURS")
    # Days after which an index (search/list) page may be read again; its listing links go through the usual dedup.
    index_ttl_days: int = Field(default=7, ge=0, le=365, validation_alias="WEB_SEARCH_INDEX_TTL_DAYS")
    max_pages_per_campaign: int = Field(default=400, ge=1, le=500, validation_alias="WEB_SEARCH_MAX_PAGES_PER_CAMPAIGN")
    max_pages_per_host: int = Field(default=100, ge=1, le=100, validation_alias="WEB_SEARCH_MAX_PAGES_PER_HOST")
    max_links_per_index: int = Field(default=40, ge=0, le=100, validation_alias="WEB_SEARCH_MAX_LINKS_PER_INDEX")
    max_pages_per_day: int = Field(default=3000, ge=1, le=10_000, validation_alias="WEB_SEARCH_MAX_PAGES_PER_DAY")
    max_queries_per_day: int = Field(default=300, ge=1, le=5_000, validation_alias="WEB_SEARCH_MAX_QUERIES_PER_DAY")
    max_minutes_per_campaign: int = Field(default=240, ge=5, le=10_080, validation_alias="WEB_SEARCH_MAX_MINUTES_PER_CAMPAIGN")
    blocked_hosts_raw: str = Field(default="", validation_alias="WEB_SEARCH_BLOCKED_HOSTS")
    # Search every known portal of the country (Idealista, Fotocasa first), not only those the model picks.
    cover_portals: bool = Field(default=True, validation_alias="WEB_SEARCH_COVER_PORTALS")

    # Fetching public pages.
    user_agent: str = Field(default="RealEstateResearchBot/0.2 (+https://github.com/infinitytechmain2024/REAL-ESTATE-BOT)",
                            min_length=3, max_length=300, validation_alias="WEB_SEARCH_USER_AGENT")
    request_timeout_seconds: float = Field(default=20, ge=3, le=120, validation_alias="WEB_SEARCH_REQUEST_TIMEOUT_SECONDS")
    max_content_bytes: int = Field(default=2_000_000, ge=10_000, le=10_000_000, validation_alias="WEB_SEARCH_MAX_CONTENT_BYTES")
    host_interval_seconds: float = Field(default=5, ge=1, le=300, validation_alias="WEB_SEARCH_HOST_INTERVAL_SECONDS")
    # Pages drawn by JavaScript (HTTP 200 but empty): read once more in the browser (the Agent Reach path).
    render_enabled: bool = Field(default=True, validation_alias="WEB_SEARCH_RENDER_ENABLED")
    render_timeout_seconds: float = Field(default=30, ge=5, le=60, validation_alias="WEB_SEARCH_RENDER_TIMEOUT_SECONDS")
    max_renders_per_campaign: int = Field(default=15, ge=0, le=200, validation_alias="WEB_SEARCH_MAX_RENDERS_PER_CAMPAIGN")
    # Optional outbound proxy/VPN for page fetches (http://, https://, socks5://). Never logged.
    # Several proxies may be given comma-separated (one sticky proxy per host).
    proxy_url: str = Field(default="", validation_alias="WEB_SEARCH_PROXY_URL")
    # Browser-impersonating fetch via curl_cffi: off | chrome | safari | firefox | a profile name (chrome124).
    impersonate: str = Field(default="chrome", max_length=40, validation_alias="WEB_SEARCH_IMPERSONATE")
    # UA sent on page requests when impersonating; empty: the Chrome 124 / Windows default for the profile.
    browser_user_agent: str = Field(default="", max_length=300, validation_alias="WEB_SEARCH_BROWSER_USER_AGENT")

    def blocked_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower().removeprefix("www.") for h in self.blocked_hosts_raw.replace(",", " ").split()
                         if h.strip())

    def config(self) -> WebSearchConfig:
        return WebSearchConfig(
            queries_per_round=self.queries_per_round, max_queries_per_campaign=self.max_queries_per_campaign,
            results_per_query=self.results_per_query, pages_per_query=self.pages_per_query,
            query_reuse_hours=self.query_reuse_hours, index_ttl_days=self.index_ttl_days, max_pages_per_campaign=self.max_pages_per_campaign,
            max_pages_per_host=self.max_pages_per_host, max_links_per_index=self.max_links_per_index,
            max_pages_per_day=self.max_pages_per_day, max_queries_per_day=self.max_queries_per_day,
            max_minutes_per_campaign=self.max_minutes_per_campaign, blocked_hosts=self.blocked_hosts(),
            page_runtime_seconds=int(min(600, self.request_timeout_seconds * 3)),
            max_renders_per_campaign=self.max_renders_per_campaign if self.render_enabled else 0,
            cover_portals=self.cover_portals,
        )
