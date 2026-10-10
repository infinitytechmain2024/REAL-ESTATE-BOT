"""Environment settings of the web stage (read by the campaign-runner service)."""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .fetcher import checked_impersonation
from .scrape_api import ScrapeApiClient
from .search_backends import (
    BACKEND_NAMES,
    GoogleCseClient,
    MergedSearcher,
    SearchBackend,
    SerpApiClient,
)
from .searxng import SearxngClient
from .sources import ListingSource
from .worker import WebSearchConfig

log = logging.getLogger(__name__)


class WebSearchSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    enabled: bool = Field(default=True, validation_alias="WEB_SEARCH_ENABLED")
    searxng_url: str = Field(default="http://searxng:8080", validation_alias="WEB_SEARCH_SEARXNG_URL")
    searxng_timeout_seconds: float = Field(default=25, ge=3, le=120, validation_alias="WEB_SEARCH_SEARXNG_TIMEOUT_SECONDS")
    # Search backends: comma list of searxng | google_cse | serpapi. A named backend without its key is skipped.
    backends_raw: str = Field(default="searxng", validation_alias="WEB_SEARCH_BACKENDS")
    google_cse_api_key: str = Field(default="", repr=False, validation_alias="GOOGLE_CSE_API_KEY")
    google_cse_cx: str = Field(default="", validation_alias="GOOGLE_CSE_CX")
    serpapi_api_key: str = Field(default="", repr=False, validation_alias="SERPAPI_API_KEY")
    google_cse_daily_cap: int = Field(default=90, ge=0, le=100_000, validation_alias="WEB_SEARCH_GOOGLE_CSE_DAILY_CAP")
    serpapi_daily_cap: int = Field(default=90, ge=0, le=100_000, validation_alias="WEB_SEARCH_SERPAPI_DAILY_CAP")
    poll_seconds: float = Field(default=15, ge=2, le=600, validation_alias="WEB_SEARCH_POLL_SECONDS")

    apify_idealista_enabled: bool = Field(default=False, validation_alias="APIFY_IDEALISTA_ENABLED")
    apify_token: str = Field(default="", repr=False, validation_alias="APIFY_TOKEN")
    apify_idealista_actor: str = Field(default="axlymxp/idealista-scraper", validation_alias="APIFY_IDEALISTA_ACTOR")
    apify_idealista_max_results: int = Field(default=20, ge=1, le=50, validation_alias="APIFY_IDEALISTA_MAX_RESULTS")
    apify_idealista_location_name: str = Field(default="Madrid", validation_alias="APIFY_IDEALISTA_LOCATION_NAME")
    apify_idealista_location_id: str = Field(default="", validation_alias="APIFY_IDEALISTA_LOCATION_ID")
    apify_idealista_timeout_seconds: float = Field(default=120, ge=1, le=120, validation_alias="APIFY_IDEALISTA_TIMEOUT_SECONDS")
    apify_idealista_max_charge_usd: float = Field(default=0.10, gt=0, le=10, validation_alias="APIFY_IDEALISTA_MAX_CHARGE_USD")

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
    # Pages per campaign for a host that is no known portal, until it produced a listing post.
    max_pages_per_unknown_host: int = Field(default=5, ge=1, le=100, validation_alias="WEB_SEARCH_MAX_PAGES_PER_UNKNOWN_HOST")
    max_links_per_index: int = Field(default=40, ge=0, le=100, validation_alias="WEB_SEARCH_MAX_LINKS_PER_INDEX")
    max_pages_per_day: int = Field(default=3000, ge=1, le=10_000, validation_alias="WEB_SEARCH_MAX_PAGES_PER_DAY")
    max_queries_per_day: int = Field(default=300, ge=1, le=5_000, validation_alias="WEB_SEARCH_MAX_QUERIES_PER_DAY")
    max_minutes_per_campaign: int = Field(default=240, ge=5, le=10_080, validation_alias="WEB_SEARCH_MAX_MINUTES_PER_CAMPAIGN")
    blocked_hosts_raw: str = Field(default="", validation_alias="WEB_SEARCH_BLOCKED_HOSTS")
    # Search every known portal of the country (Idealista, Fotocasa first), not only those the model picks.
    cover_portals: bool = Field(default=True, validation_alias="WEB_SEARCH_COVER_PORTALS")
    # Which sites a search hit may come from: strict (the country's known portals -- for Spain the 20 of
    # urls.SPAIN_PORTALS -- plus the sites the plan or the person named), soft (also sites whose hit shows a price or
    # an area), off (the old rule: a property word and a deal word). A country without a portal list is soft.
    domain_policy: Literal["strict", "soft", "off"] = Field(default="strict", validation_alias="WEB_SEARCH_DOMAIN_POLICY")
    # A site refused (403/429/captcha on every layer tried) this many pages in a row is skipped for the rest of the
    # campaign; its search results stay as cards. 0: off.
    host_breaker_refusals: int = Field(default=3, ge=0, le=100, validation_alias="WEB_SEARCH_HOST_BREAKER_REFUSALS")
    # USD one paid search query costs (Google CSE / SerpAPI; SearXNG is free), for CAMPAIGN_BUDGET_USD.
    paid_query_cost_usd: float = Field(default=0.0, ge=0, le=10, validation_alias="WEB_SEARCH_PAID_QUERY_COST_USD")

    # Fetching public pages.
    user_agent: str = Field(default="RealEstateResearchBot/0.2 (+https://github.com/infinitytechmain2024/REAL-ESTATE-BOT)",
                            min_length=3, max_length=300, validation_alias="WEB_SEARCH_USER_AGENT")
    request_timeout_seconds: float = Field(default=20, ge=3, le=120, validation_alias="WEB_SEARCH_REQUEST_TIMEOUT_SECONDS")
    max_content_bytes: int = Field(default=2_000_000, ge=10_000, le=10_000_000, validation_alias="WEB_SEARCH_MAX_CONTENT_BYTES")
    host_interval_seconds: float = Field(default=5, ge=1, le=300, validation_alias="WEB_SEARCH_HOST_INTERVAL_SECONDS")
    # Pages drawn by JavaScript (HTTP 200 but empty): read once more in the browser (the Agent Reach path).
    render_enabled: bool = Field(default=True, validation_alias="WEB_SEARCH_RENDER_ENABLED")
    render_timeout_seconds: float = Field(default=30, ge=5, le=60, validation_alias="WEB_SEARCH_RENDER_TIMEOUT_SECONDS")
    max_renders_per_campaign: int = Field(default=60, ge=0, le=200, validation_alias="WEB_SEARCH_MAX_RENDERS_PER_CAMPAIGN")
    # A page the plain fetch was refused (403/429/503, a captcha page) is tried once in the browser
    # (robots.txt still decides first); then the search-result card. Off: only empty JS pages are rendered.
    render_on_refusal: bool = Field(default=True, validation_alias="WEB_SEARCH_RENDER_ON_REFUSAL")
    # A refused depth-0 index (search/list) page may use the browser; the scrape API is never used for index pages.
    render_index_on_refusal: bool = Field(default=True, validation_alias="WEB_SEARCH_RENDER_INDEX_ON_REFUSAL")
    # Human verification: a CAPTCHA / anti-bot page met in the browser becomes a verification job (Telegram button,
    # live browser, a person passes the check by hand and presses «Готово»); the site is skipped meanwhile and then
    # read through the same browser profile. Nothing is solved or worked around automatically. Off by default:
    # a site's terms may forbid automated access, enable it only on the owner's decision.
    human_verification: bool = Field(default=False, validation_alias="WEB_SEARCH_HUMAN_VERIFICATION")
    verified_host_interval_seconds: float = Field(default=8, ge=0, le=300, validation_alias="WEB_SEARCH_VERIFIED_HOST_INTERVAL_SECONDS")
    pages_per_verification: int = Field(default=40, ge=1, le=200, validation_alias="WEB_SEARCH_PAGES_PER_VERIFICATION")
    # Optional last layer for pages both HTTP and the browser were refused: GET {url}?url=<page> with
    # "Authorization: Bearer <key>" (a Zyte / ScraperAPI / Bright Data style unlocker). Empty: off. Never logged.
    scrape_api_url: str = Field(default="", validation_alias="WEB_SEARCH_SCRAPE_API_URL")
    scrape_api_key: str = Field(default="", repr=False, validation_alias="WEB_SEARCH_SCRAPE_API_KEY")
    scrape_api_timeout_seconds: float = Field(default=60, ge=5, le=180, validation_alias="WEB_SEARCH_SCRAPE_API_TIMEOUT_SECONDS")
    max_scrape_api_per_campaign: int = Field(default=40, ge=0, le=500, validation_alias="WEB_SEARCH_MAX_SCRAPE_API_PER_CAMPAIGN")
    # USD one scrape-API read costs (booked per call, refused ones too), for CAMPAIGN_BUDGET_USD.
    scrape_api_cost_usd: float = Field(default=0.0, ge=0, le=10, validation_alias="WEB_SEARCH_SCRAPE_API_COST_USD")
    # Optional outbound proxy/VPN for page fetches (http://, https://, socks5://). Never logged.
    # Several proxies may be given comma-separated (one sticky proxy per host).
    proxy_url: str = Field(default="", repr=False, validation_alias="WEB_SEARCH_PROXY_URL")
    # Browser-impersonating fetch via curl_cffi: off | chrome | safari | firefox | a profile name (chrome124).
    impersonate: str = Field(default="chrome", max_length=40, validation_alias="WEB_SEARCH_IMPERSONATE")
    # UA sent on page requests when impersonating; empty: the Chrome 124 / Windows default for the profile.
    browser_user_agent: str = Field(default="", max_length=300, validation_alias="WEB_SEARCH_BROWSER_USER_AGENT")

    @field_validator("impersonate")
    @classmethod
    def _known_profile(cls, value: str) -> str:
        return checked_impersonation(value)

    def blocked_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower().removeprefix("www.") for h in self.blocked_hosts_raw.replace(",", " ").split()
                         if h.strip())

    def config(self) -> WebSearchConfig:
        return WebSearchConfig(
            queries_per_round=self.queries_per_round, max_queries_per_campaign=self.max_queries_per_campaign,
            results_per_query=self.results_per_query, pages_per_query=self.pages_per_query,
            query_reuse_hours=self.query_reuse_hours, index_ttl_days=self.index_ttl_days, max_pages_per_campaign=self.max_pages_per_campaign,
            max_pages_per_host=self.max_pages_per_host,
            max_pages_per_unknown_host=self.max_pages_per_unknown_host, max_links_per_index=self.max_links_per_index,
            max_pages_per_day=self.max_pages_per_day, max_queries_per_day=self.max_queries_per_day,
            max_minutes_per_campaign=self.max_minutes_per_campaign, blocked_hosts=self.blocked_hosts(),
            page_runtime_seconds=int(min(600, self.request_timeout_seconds * 3)),
            max_renders_per_campaign=self.max_renders_per_campaign if self.render_enabled else 0,
            cover_portals=self.cover_portals, render_on_refusal=self.render_on_refusal,
            render_index_on_refusal=self.render_index_on_refusal,
            human_verification=self.human_verification and self.render_enabled,
            verified_host_interval_seconds=self.verified_host_interval_seconds,
            pages_per_verification=self.pages_per_verification,
            max_scrape_api_per_campaign=self.max_scrape_api_per_campaign if self.scrape_api_url else 0,
            domain_policy=self.domain_policy, host_breaker_refusals=self.host_breaker_refusals,
            scrape_cost_usd=self.scrape_api_cost_usd,
            query_cost_usd=self.paid_query_cost_usd * sum(
                1 for n in self.backend_names() if (n == "google_cse" and self.google_cse_api_key and self.google_cse_cx)
                or (n == "serpapi" and self.serpapi_api_key)),
        )

    def scraper(self) -> ScrapeApiClient | None:
        """The scrape-API layer, or None when ``WEB_SEARCH_SCRAPE_API_URL`` is empty."""
        if not self.scrape_api_url:
            return None
        return ScrapeApiClient(self.scrape_api_url, self.scrape_api_key, timeout_seconds=self.scrape_api_timeout_seconds,
                               max_bytes=self.max_content_bytes)

    def sources(self) -> tuple[ListingSource, ...]:
        """Default-off providers: disabling the source does not construct an HTTP client."""
        if not self.apify_idealista_enabled:
            return ()
        if not self.apify_token:
            log.warning("web_search.apify_disabled", extra={"code": "apify_token_missing"})
            return ()
        from .sources.apify import ApifyIdealistaSource

        return (ApifyIdealistaSource(token=self.apify_token, actor_id=self.apify_idealista_actor,
                                    location_name=self.apify_idealista_location_name,
                                    location_id=self.apify_idealista_location_id,
                                    max_items=self.apify_idealista_max_results,
                                    timeout_seconds=self.apify_idealista_timeout_seconds,
                                    max_charge_usd=self.apify_idealista_max_charge_usd),)

    def backend_names(self) -> list[str]:
        names: list[str] = []
        for raw in self.backends_raw.replace(";", ",").split(","):
            name = raw.strip().lower()
            if not name:
                continue
            if name not in BACKEND_NAMES:
                log.warning("web_search.unknown_backend %s", name)
            elif name not in names:
                names.append(name)
        return names or ["searxng"]

    def searcher(self, searxng_client: SearxngClient) -> SearchBackend:
        """The SearXNG client alone when it is the only usable backend, else a ``MergedSearcher``."""
        config = self.config()
        timeout = self.searxng_timeout_seconds
        backends: list[SearchBackend] = []
        caps: dict[str, int] = {}
        for name in self.backend_names():
            if name == "searxng":
                backends.append(searxng_client)
            elif name == "google_cse":
                if not (self.google_cse_api_key and self.google_cse_cx):
                    log.warning("web_search.backend_skipped google_cse: GOOGLE_CSE_API_KEY/GOOGLE_CSE_CX missing")
                    continue
                backends.append(GoogleCseClient(self.google_cse_api_key, self.google_cse_cx,
                                                max_results=config.results_per_query, timeout=timeout))
                caps[name] = self.google_cse_daily_cap
            elif name == "serpapi":
                if not self.serpapi_api_key:
                    log.warning("web_search.backend_skipped serpapi: SERPAPI_API_KEY missing")
                    continue
                backends.append(SerpApiClient(self.serpapi_api_key, max_results=config.results_per_query, timeout=timeout))
                caps[name] = self.serpapi_daily_cap
        if not backends:
            return searxng_client
        if backends == [searxng_client]:
            return searxng_client
        return MergedSearcher(backends, max_results=config.results_per_query, daily_caps=caps)
