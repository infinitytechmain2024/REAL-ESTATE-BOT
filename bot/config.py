"""Application configuration.

Everything is read from the environment (or a local ``.env``) and validated at
start-up, so a misconfigured deployment fails immediately with a readable error
instead of at the first user message. See ``.env.example`` for the full list.

The settings are split into nested models per subsystem; each nested model has
its own ``env_prefix``, which keeps the variable names self-describing
(``LLM_MODEL``, ``STT_PROVIDER``, ``SEARXNG_URL``, ...).
"""

from __future__ import annotations

import functools
import json
from typing import Annotated, Literal

from dotenv import load_dotenv
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# pydantic-settings JSON-decodes complex types straight out of the environment,
# which makes a plain `FOO=a,b` fail before any validator runs. NoDecode hands
# the raw string to our `mode="before"` validators instead, so both
# `FOO=a,b` and `FOO=["a","b"]` are accepted.
CsvList = Annotated[list[str], NoDecode]


def _parse_str_list(value: object) -> object:
    """Accept ``a, b``, ``["a", "b"]`` or an actual list for a :data:`CsvList`."""
    if value is None:
        return []
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            pass  # fall through to comma splitting
        else:
            if isinstance(decoded, list):
                return [str(item).strip() for item in decoded if str(item).strip()]
    return [part.strip() for part in text.split(",") if part.strip()]

BotMode = Literal["polling", "webhook"]
LogFormat = Literal["console", "json"]


class _Base(BaseSettings):
    """Common config for every settings group.

    Deliberately no ``env_file``: pydantic-settings applies it per model, and a
    nested model built through ``default_factory`` would read its own copy,
    ignoring whatever file the root was given. Instead :func:`get_settings`
    loads the dotenv file into the process environment once, so every group --
    nested or not -- sees the same values, and a real environment variable
    always wins over the file, which is what containers need.
    """

    model_config = SettingsConfigDict(
        extra="ignore",
        case_sensitive=False,
    )


class TelegramSettings(_Base):
    """Bot token and how updates are delivered."""

    model_config = SettingsConfigDict(**{**_Base.model_config, "env_prefix": "TELEGRAM_"})

    token: SecretStr = Field(description="Bot token from @BotFather")
    mode: BotMode = Field(default="polling", description="'polling' (default) or 'webhook'")

    webhook_url: str | None = Field(default=None, description="Public https URL, webhook mode only")
    webhook_path: str = Field(default="/webhook", description="Path the webhook is served on")
    webhook_secret: SecretStr | None = Field(
        default=None, description="Value Telegram echoes in X-Telegram-Bot-Api-Secret-Token"
    )
    webhook_host: str = Field(default="0.0.0.0", description="Bind address for the webhook server")
    webhook_port: int = Field(default=8080, ge=1, le=65535)

    # A single user's request fans out to several LLM calls and page fetches;
    # without a cap one person can monopolise the worker.
    max_concurrent_searches: int = Field(default=3, ge=1, le=50)
    request_cooldown_seconds: float = Field(
        default=3.0, ge=0.0, description="Minimum gap between two requests from the same user"
    )

    @model_validator(mode="after")
    def _webhook_needs_url(self) -> TelegramSettings:
        if self.mode == "webhook" and not self.webhook_url:
            raise ValueError("TELEGRAM_WEBHOOK_URL is required when TELEGRAM_MODE=webhook")
        return self

    @field_validator("webhook_path")
    @classmethod
    def _leading_slash(cls, value: str) -> str:
        return value if value.startswith("/") else f"/{value}"


class LLMSettings(_Base):
    """Which LLM provider to use, and with what models.

    ``provider`` is a key in ``bot.services.llm.registry``; adding a provider
    never requires touching this class.
    """

    model_config = SettingsConfigDict(**{**_Base.model_config, "env_prefix": "LLM_"})

    provider: str = Field(
        default="openai_compatible",
        description="Registered provider name: openai_compatible | anthropic | openai | nvidia",
    )
    fallback_providers: CsvList = Field(
        default_factory=list,
        description="Tried in order when the primary provider errors out",
    )

    model: str = Field(default="openai/gpt-4o-mini", description="Default model id")
    model_extract: str | None = Field(
        default=None, description="Cheaper model for query parsing; defaults to `model`"
    )
    model_rank: str | None = Field(
        default=None, description="Stronger model for ranking/structuring; defaults to `model`"
    )

    base_url: str | None = Field(
        default=None,
        description="OpenAI-compatible endpoint, e.g. https://openrouter.ai/api/v1. "
        "Falls back to the provider's own default.",
    )
    api_key: SecretStr | None = Field(default=None, description="Key for the selected provider")

    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=64, le=200_000)
    timeout_seconds: float = Field(default=90.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)

    @field_validator("fallback_providers", mode="before")
    @classmethod
    def _parse_fallbacks(cls, value: object) -> object:
        return _parse_str_list(value)

    @property
    def extract_model(self) -> str:
        return self.model_extract or self.model

    @property
    def rank_model(self) -> str:
        return self.model_rank or self.model


class STTSettings(_Base):
    """Speech-to-text for voice messages."""

    model_config = SettingsConfigDict(**{**_Base.model_config, "env_prefix": "STT_"})

    enabled: bool = Field(default=True, description="Set false to reject voice messages politely")
    provider: str = Field(
        default="openai_whisper",
        description="Registered provider name: openai_whisper | groq_whisper | nvidia",
    )
    fallback_providers: CsvList = Field(default_factory=list)

    model: str = Field(default="whisper-1")
    base_url: str | None = Field(default=None, description="OpenAI-compatible audio endpoint")
    api_key: SecretStr | None = Field(default=None)
    language: str | None = Field(
        default=None, description="ISO-639-1 hint; leave empty to auto-detect"
    )
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_audio_mb: float = Field(
        default=20.0, gt=0, le=50, description="Refuse longer voice notes rather than pay for them"
    )

    @field_validator("fallback_providers", mode="before")
    @classmethod
    def _parse_fallbacks(cls, value: object) -> object:
        return _parse_str_list(value)


class SearxngSettings(_Base):
    """Connection to the SearXNG JSON API."""

    model_config = SettingsConfigDict(**{**_Base.model_config, "env_prefix": "SEARXNG_"})

    url: str = Field(default="http://127.0.0.1:8888", description="Base URL of the instance")
    timeout_seconds: float = Field(default=25.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)

    engines: CsvList = Field(
        default_factory=lambda: ["google", "bing", "duckduckgo", "brave", "startpage"],
        description="Engines requested per query; must be enabled in settings.yml",
    )
    max_queries: int = Field(default=6, ge=1, le=20, description="Search strings per user request")
    results_per_query: int = Field(default=15, ge=1, le=50)
    max_hits: int = Field(default=40, ge=1, le=200, description="Cap after merging and de-duping")
    concurrency: int = Field(default=4, ge=1, le=20, description="Parallel SearXNG requests")

    blocked_domains: CsvList = Field(
        default_factory=lambda: [
            "facebook.com",
            "instagram.com",
            "x.com",
            "twitter.com",
            "tiktok.com",
            "pinterest.com",
            "youtube.com",
            "reddit.com",
        ],
        description="Hosts dropped before ranking; matched on suffix",
    )

    @field_validator("url")
    @classmethod
    def _no_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("engines", "blocked_domains", mode="before")
    @classmethod
    def _parse_lists(cls, value: object) -> object:
        return _parse_str_list(value)


class ParserSettings(_Base):
    """Fetching and extracting the pages behind the search hits."""

    model_config = SettingsConfigDict(**{**_Base.model_config, "env_prefix": "PARSER_"})

    enabled: bool = Field(default=True, description="Set false to rank on snippets alone")
    max_pages: int = Field(default=8, ge=1, le=50, description="Hits to actually fetch")
    concurrency: int = Field(default=5, ge=1, le=20)
    timeout_seconds: float = Field(default=15.0, gt=0)
    max_bytes: int = Field(default=1_500_000, gt=0, description="Abort downloads larger than this")
    max_chars: int = Field(
        default=6_000, gt=0, description="Characters of page text handed to the LLM"
    )
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (compatible; RealEstateResearchBot/0.1; "
            "+https://github.com/infinitytechmain2024/real-estate-bot)"
        )
    )

    # -- browser fallback --------------------------------------------------
    # Large listing portals answer a plain HTTP client with 403 however polite
    # its headers are. These control the Playwright fetcher that gets past
    # that; it is off by default because it needs Chromium in the image.

    browser_enabled: bool = Field(
        default=False, description="Use Playwright for blocked pages (needs `playwright install`)"
    )
    browser_domains: CsvList = Field(
        default_factory=list,
        description="Domains always fetched in a browser, skipping the HTTP attempt",
    )
    browser_proxy_url: str | None = Field(
        default=None,
        description="Egress proxy for the browser, e.g. http://user:pass@host:port. "
        "A residential/mobile endpoint is strongly recommended: datacenter IPs "
        "are the main thing these sites' protections key on.",
    )
    browser_headless: bool = Field(default=True)
    browser_concurrency: int = Field(
        default=2, ge=1, le=10, description="Tabs at once; each is a real page render"
    )
    browser_timeout_seconds: float = Field(default=30.0, gt=0, description="Navigation timeout")
    browser_wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = Field(
        default="domcontentloaded",
        description="Playwright navigation milestone to wait for before reading the DOM",
    )
    browser_user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        ),
        description="Sent by the browser fetcher; the bot-identifying default above "
        "defeats the point of using a browser at all",
    )
    browser_locale: str = Field(default="es-ES", description="Browser locale, e.g. es-ES")

    @field_validator("browser_domains", mode="before")
    @classmethod
    def _split_browser_domains(cls, value: object) -> object:
        return _parse_str_list(value)


class SupabaseSettings(_Base):
    """Supabase / PostgREST credentials."""

    model_config = SettingsConfigDict(**{**_Base.model_config, "env_prefix": "SUPABASE_"})

    url: str | None = Field(default=None, description="https://<project>.supabase.co")
    key: SecretStr | None = Field(
        default=None, description="service_role key (server side) or anon key"
    )
    schema_name: str = Field(default="public", alias="SUPABASE_SCHEMA")
    timeout_seconds: float = Field(default=20.0, gt=0)

    @property
    def configured(self) -> bool:
        """Whether persistence is available.

        The bot deliberately still runs without Supabase -- results are simply
        not stored or de-duplicated across sessions -- so a missing key is a
        warning at start-up rather than a crash.
        """
        return bool(self.url and self.key)


class PipelineSettings(_Base):
    """Knobs for the research pipeline itself."""

    model_config = SettingsConfigDict(**{**_Base.model_config, "env_prefix": "PIPELINE_"})

    max_results_to_user: int = Field(default=8, ge=1, le=30)
    min_score: int = Field(
        default=45, ge=0, le=100, description="Drop results the LLM scored lower"
    )
    send_delay_seconds: float = Field(
        default=0.4,
        ge=0.0,
        description="Pause between result messages to stay under Telegram limits",
    )
    skip_seen_results: bool = Field(
        default=True, description="Never show a user the same url_hash twice"
    )

    include_alternatives: bool = Field(
        default=True,
        description="When a result matches everything except the budget, offer it as a "
        "near miss ('nothing in your range, but here is one 45 000 more') instead of "
        "dropping it",
    )
    max_alternatives: int = Field(
        default=4,
        ge=1,
        le=20,
        description="Cap on near misses per answer, so alternatives never crowd out matches",
    )


class FacebookSettings(_Base):
    """Facebook group scraping: the shared, operator-controlled browser session.

    End users never touch Facebook directly -- this is a single persistent,
    visible browser profile that the bot drives to read group posts/comments,
    and that a human takes over when login or verification is required. See
    ``bot/services/facebook/`` for the session and group-reading logic.
    """

    model_config = SettingsConfigDict(**{**_Base.model_config, "env_prefix": "FACEBOOK_"})

    enabled: bool = Field(default=False, description="Set true once a profile/groups are configured")
    search_enabled: bool = Field(
        default=False,
        description="Feed group posts into the research pipeline as a second source of hits. "
        "Deliberately separate from `enabled`: the session and the admin takeover flow are worth "
        "having on their own, and group reading should only be switched on once "
        "scripts/facebook_probe.py has proven the selectors against a real, accessible group.",
    )

    profile_dir: str = Field(
        default="./data/facebook_profile",
        description="Persistent Playwright user-data-dir; holds the login session on disk",
    )
    headless: bool = Field(
        default=False,
        description="Keep false: a visible window is what the operator takes over during recovery",
    )
    nav_timeout_seconds: float = Field(default=30.0, gt=0)
    cdp_url: str | None = Field(
        default=None,
        description="Attach to an already-running Chrome via CDP (e.g. http://127.0.0.1:9222) "
        "instead of launching one. Set this on the VM, where a supervised system Chrome is the "
        "long-lived process; leave unset for local dev, where launch_persistent_context is used.",
    )
    action_delay_ms: tuple[int, int] = Field(
        default=(600, 1800),
        description="Random pause range between actions (scroll/click), min/max ms",
    )

    group_urls: CsvList = Field(
        default_factory=list,
        description="Facebook group URLs to read, comma-separated. Supplied by the operator, "
        "never discovered automatically in v1.",
    )
    max_posts_per_group: int = Field(default=20, ge=1, le=200)
    max_comments_per_post: int = Field(default=15, ge=0, le=200)
    min_group_recheck_minutes: int = Field(
        default=60, ge=1, description="Do not re-open a group more often than this"
    )
    search_timeout_seconds: float = Field(
        default=90.0,
        gt=0,
        description="Give up on reading groups after this long and answer from the web alone. "
        "A browser walking several groups is slower than every other source here by an order of "
        "magnitude, and the person who sent the message is waiting.",
    )

    login_email: str | None = Field(default=None, description="Only used for the one automatic attempt")
    login_password: SecretStr | None = Field(
        default=None, description="Never logged; used once per incident, then the operator takes over"
    )

    admin_telegram_ids: CsvList = Field(
        default_factory=list,
        description="Numeric Telegram IDs to alert on login/verification failures",
    )

    # --- Remote live-view gate (Telegram button -> token-gated noVNC) ---
    desktop_public_base: str | None = Field(
        default=None,
        description="Public HTTPS base the gate is reachable at (e.g. a Tailscale Funnel "
        "address), used to build the Telegram button URL. Unset means the login/checkpoint "
        "alert falls back to plain text with no button.",
    )
    desktop_token_ttl_seconds: int = Field(
        default=1800, ge=60, description="How long a login/checkpoint link stays valid before "
        "it must be reissued"
    )
    desktop_pin: str | None = Field(
        default=None, description="Optional PIN required before the live browser view is shown"
    )
    gate_bind_address: str = Field(default="127.0.0.1")
    gate_port: int = Field(default=8090)
    novnc_internal_url: str = Field(
        default="http://127.0.0.1:6080",
        description="Where noVNC/websockify actually listens; reached only through this gate, "
        "never exposed directly",
    )
    token_store_path: str = Field(default="./data/facebook_gate_token.json")

    @field_validator("group_urls", "admin_telegram_ids", mode="before")
    @classmethod
    def _parse_lists(cls, value: object) -> object:
        return _parse_str_list(value)

    @field_validator("action_delay_ms", mode="before")
    @classmethod
    def _parse_delay(cls, value: object) -> object:
        if isinstance(value, str):
            parts = [int(p.strip()) for p in value.split(",")]
            return tuple(parts[:2])
        return value


class Settings(_Base):
    """Root settings object; build it with :func:`get_settings`."""

    environment: Literal["local", "render", "docker"] = Field(default="local")
    log_level: str = Field(default="INFO")
    log_format: LogFormat = Field(default="console")

    telegram: TelegramSettings = Field(default_factory=TelegramSettings)  # type: ignore[arg-type]
    llm: LLMSettings = Field(default_factory=LLMSettings)
    stt: STTSettings = Field(default_factory=STTSettings)
    searxng: SearxngSettings = Field(default_factory=SearxngSettings)
    parser: ParserSettings = Field(default_factory=ParserSettings)
    supabase: SupabaseSettings = Field(default_factory=SupabaseSettings)
    pipeline: PipelineSettings = Field(default_factory=PipelineSettings)
    facebook: FacebookSettings = Field(default_factory=FacebookSettings)

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}")
        return level


@functools.lru_cache(maxsize=1)
def get_settings(env_file: str | None = ".env") -> Settings:
    """Load and cache the settings.

    *env_file* is loaded into ``os.environ`` if it exists, without overriding
    variables that are already set -- so a container's real environment always
    beats a stray ``.env`` baked into an image. Pass ``None`` to skip the file
    entirely.

    Cached because building this validates every subsystem; call
    :meth:`get_settings.cache_clear` in tests to pick up a changed environment.
    """
    if env_file:
        load_dotenv(env_file, override=False)
    return Settings()
