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
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from bot.exceptions import ConfigurationError

# pydantic-settings JSON-decodes complex types straight out of the environment,
# which makes a plain `FOO=a,b` fail before any validator runs. NoDecode hands
# the raw string to our `mode="before"` validators instead, so both
# `FOO=a,b` and `FOO=["a","b"]` are accepted.
CsvList = Annotated[list[str], NoDecode]
CsvIntList = Annotated[list[int], NoDecode]


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


def _parse_int_list(value: object) -> object:
    """Same as :func:`_parse_str_list`, but for a list of ids."""
    parsed = _parse_str_list(value)
    if not isinstance(parsed, list):
        return parsed
    out: list[int] = []
    for item in parsed:
        try:
            out.append(int(str(item).strip()))
        except ValueError:
            raise ValueError(f"{item!r} is not a valid Telegram id") from None
    return out


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
    max_searches_per_user: int = Field(
        default=1,
        ge=1,
        le=10,
        description="Pipelines one user may have in flight at once",
    )
    request_cooldown_seconds: float = Field(
        default=3.0, ge=0.0, description="Minimum gap between two requests from the same user"
    )
    # 'Подробнее' is the one button that costs an LLM call and a page fetch, so
    # unlike the feedback buttons it gets a cooldown of its own.
    details_cooldown_seconds: float = Field(
        default=15.0, ge=0.0, description="Minimum gap between two 'Подробнее' presses"
    )
    admin_ids: CsvIntList = Field(
        default_factory=list,
        description="Telegram ids notified when the daily LLM budget is spent",
    )

    @field_validator("admin_ids", mode="before")
    @classmethod
    def _parse_admin_ids(cls, value: object) -> object:
        return _parse_int_list(value)

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

    # Prices are per million tokens and differ per model, so they are config
    # rather than a hard-coded table that would silently go stale. The defaults
    # are gpt-4o-mini's list price; set them to whatever your model costs.
    price_prompt_usd_per_1m: float = Field(
        default=0.15, ge=0.0, description="USD per 1M prompt tokens, for cost accounting"
    )
    price_completion_usd_per_1m: float = Field(
        default=0.60, ge=0.0, description="USD per 1M completion tokens, for cost accounting"
    )

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> float:
        """Estimated USD cost of one call. Zero when the provider reports no usage."""
        return (
            prompt_tokens * self.price_prompt_usd_per_1m
            + completion_tokens * self.price_completion_usd_per_1m
        ) / 1_000_000

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
    max_redirects: int = Field(
        default=5, ge=0, le=20, description="Redirect hops followed; each one is re-screened"
    )
    max_chars: int = Field(
        default=6_000, gt=0, description="Characters of page text handed to the LLM"
    )
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (compatible; RealEstateResearchBot/0.1; "
            "+https://github.com/infinitytechmain2024/real-estate-bot)"
        )
    )


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
    min_score: int = Field(default=45, ge=0, le=100, description="Drop results the LLM scored lower")
    send_delay_seconds: float = Field(
        default=0.4, ge=0.0, description="Pause between result messages to stay under Telegram limits"
    )
    skip_seen_results: bool = Field(
        default=True, description="Never show a user the same url_hash twice"
    )
    # Without this a stalled provider leaves the user staring at a status
    # message forever and holds a concurrency slot the whole time.
    timeout_seconds: float = Field(
        default=90.0, gt=0, description="Hard ceiling on one full pipeline run"
    )
    details_timeout_seconds: float = Field(
        default=60.0, gt=0, description="Hard ceiling on one 'Подробнее' briefing"
    )


class LimitsSettings(_Base):
    """Spending limits: how much one user, and the deployment, may use per day.

    Counters roll over on the UTC calendar date rather than on a sliding
    window -- a user should be able to look at the number and predict when it
    resets, and "midnight UTC" is the only answer that does not depend on
    where they are.
    """

    model_config = SettingsConfigDict(**{**_Base.model_config, "env_prefix": "LIMITS_"})

    # Sized for a small internal team: generous enough that nobody hits it in a
    # normal day's work, low enough that a stuck client cannot spend a month's
    # budget overnight.
    daily_searches: int = Field(
        default=50, ge=0, description="Searches per user per UTC day; 0 disables searching"
    )
    daily_details: int = Field(
        default=100, ge=0, description="'Подробнее' presses per user per UTC day"
    )
    daily_cost_usd: float = Field(
        default=5.0,
        ge=0.0,
        description="Estimated LLM spend per UTC day across all users; 0 means no limit",
    )

    @property
    def cost_limit_enabled(self) -> bool:
        return self.daily_cost_usd > 0


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
    limits: LimitsSettings = Field(default_factory=LimitsSettings)

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, value: str) -> str:
        level = value.upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if level not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {value!r}")
        return level


# Every nested settings group, so a validation error can be reported as the
# environment variable name the operator actually has to set.
_GROUPS: tuple[type[_Base], ...] = (
    TelegramSettings,
    LLMSettings,
    STTSettings,
    SearxngSettings,
    ParserSettings,
    SupabaseSettings,
    PipelineSettings,
    LimitsSettings,
)


def _env_var_name(model_title: str, field: str) -> str:
    """Best guess at the environment variable behind a validation error.

    pydantic reports the failing model by class name and the field by its
    Python name; the operator only knows the variable. An explicit alias
    (``SUPABASE_SCHEMA``) wins over the prefix rule, since that is exactly what
    aliases are for.
    """
    for group in (*_GROUPS, Settings):
        if group.__name__ != model_title:
            continue
        info = group.model_fields.get(field)
        alias = getattr(info, "alias", None) or getattr(info, "validation_alias", None)
        if isinstance(alias, str):
            return alias.upper()
        prefix = group.model_config.get("env_prefix", "")
        return f"{prefix}{field}".upper()
    return field.upper()


def _describe_validation_error(exc: ValidationError) -> str:
    """Turn a pydantic ValidationError into something an operator can act on."""
    missing: list[str] = []
    invalid: list[str] = []
    for error in exc.errors():
        field = str(error["loc"][-1]) if error["loc"] else "?"
        name = _env_var_name(exc.title, field)
        if error["type"] == "missing":
            missing.append(name)
        else:
            invalid.append(f"{name}: {error['msg']}")

    lines: list[str] = []
    if missing:
        lines.append("Не заданы обязательные переменные окружения:")
        lines.extend(f"  - {name}" for name in missing)
    if invalid:
        lines.append("Некорректные значения переменных окружения:")
        lines.extend(f"  - {item}" for item in invalid)
    lines.append("Проверьте файл .env (образец — .env.example) или переменные окружения сервиса.")
    return "\n".join(lines)


@functools.lru_cache(maxsize=1)
def get_settings(env_file: str | None = ".env") -> Settings:
    """Load and cache the settings.

    *env_file* is loaded into ``os.environ`` if it exists, without overriding
    variables that are already set -- so a container's real environment always
    beats a stray ``.env`` baked into an image. Pass ``None`` to skip the file
    entirely.

    A missing or malformed variable is raised as a
    :class:`~bot.exceptions.ConfigurationError` naming the variable, which
    ``bot.main`` prints and exits on. A pydantic traceback tells the operator
    the same thing in a form they have to decode first.

    Cached because building this validates every subsystem; call
    :meth:`get_settings.cache_clear` in tests to pick up a changed environment.
    """
    if env_file:
        load_dotenv(env_file, override=False)
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigurationError(_describe_validation_error(exc)) from exc
