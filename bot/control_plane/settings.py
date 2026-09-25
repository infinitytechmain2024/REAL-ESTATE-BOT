"""Environment-only configuration for the isolated Telegram control plane."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from bot.orchestra.store import SafetyLimits

# Telegram's Bot API will not hand a bot a file larger than 20 MB.
TELEGRAM_DOWNLOAD_LIMIT_BYTES = 20 * 1_048_576
SUPPORTED_STT_PROVIDERS = frozenset({"openrouter"})


@dataclass(frozen=True, slots=True)
class ControlPlaneSettings:
    telegram_token: str
    database_url: str
    stt_provider: str = "openrouter"
    stt_model: str = "openai/whisper-large-v3-turbo"
    stt_max_audio_bytes: int = TELEGRAM_DOWNLOAD_LIMIT_BYTES
    stt_max_audio_seconds: int = 300
    stt_timeout_seconds: float = 60.0
    # Reused from the analysis pipeline; never logged or echoed.
    openrouter_api_key: str = field(default="", repr=False)
    # Live browser for logins and checkpoints. Empty public URL disables it.
    live_view_public_url: str = ""
    live_view_open_minutes: int = 20
    live_view_request_minutes: int = 60
    live_view_poll_seconds: int = 60
    live_view_port: int = 8081
    browser_session_url: str = "http://browser:8090"
    browser_session_api_token: str = field(default="", repr=False)
    novnc_url: str = "http://browser:6080"
    confirmation_ttl_seconds: int = 300
    orchestra_command_lease_seconds: int = 30
    orchestra_poll_seconds: float = 1.0
    orchestra_stale_batch_seconds: int = 900
    safety_limits: SafetyLimits = field(default_factory=SafetyLimits)
    # Telegram user IDs allowed to run, pause, resume, or cancel acquisition.
    # Empty means nobody: state changes fail closed until an operator is named.
    operator_user_ids: frozenset[int] = frozenset()

    @classmethod
    def from_env(cls) -> ControlPlaneSettings:
        token = os.environ.get("TELEGRAM_TOKEN", "").strip()
        if not token:
            raise ValueError("TELEGRAM_TOKEN is required")
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise ValueError("DATABASE_URL is required")
        return cls(
            telegram_token=token,
            database_url=database_url,
            stt_provider=_stt_provider(os.environ.get("STT_PROVIDER", "openrouter")),
            stt_model=os.environ.get("STT_MODEL", "").strip() or "openai/whisper-large-v3-turbo",
            stt_max_audio_bytes=_bounded_int("STT_MAX_AUDIO_BYTES", TELEGRAM_DOWNLOAD_LIMIT_BYTES, 1, TELEGRAM_DOWNLOAD_LIMIT_BYTES),
            stt_max_audio_seconds=_bounded_int("STT_MAX_AUDIO_SECONDS", 300, 1, 1800),
            stt_timeout_seconds=float(_bounded_int("STT_TIMEOUT_SECONDS", 60, 1, 300)),
            openrouter_api_key=os.environ.get("OPENROUTER_API_KEY", "").strip(),
            live_view_public_url=_public_url(os.environ.get("LIVE_VIEW_PUBLIC_URL", "")),
            live_view_open_minutes=_bounded_int("LIVE_VIEW_OPEN_MINUTES", 20, 5, 60),
            live_view_request_minutes=_bounded_int("LIVE_VIEW_REQUEST_MINUTES", 60, 10, 1440),
            live_view_poll_seconds=_bounded_int("LIVE_VIEW_POLL_SECONDS", 60, 10, 3600),
            browser_session_url=os.environ.get("BROWSER_SESSION_URL", "").strip() or "http://browser:8090",
            browser_session_api_token=os.environ.get("BROWSER_SESSION_API_TOKEN", "").strip(),
            confirmation_ttl_seconds=int(os.environ.get("TELEGRAM_CONFIRMATION_TTL_SECONDS", "300")),
            orchestra_command_lease_seconds=int(os.environ.get("ORCHESTRA_COMMAND_LEASE_SECONDS", "30")),
            orchestra_poll_seconds=float(os.environ.get("ORCHESTRA_POLL_SECONDS", "1")),
            orchestra_stale_batch_seconds=int(os.environ.get("ORCHESTRA_STALE_BATCH_SECONDS", "900")),
            safety_limits=SafetyLimits(
                facebook_batches_per_day=_bounded_int("SAFETY_MAX_FACEBOOK_BATCHES_PER_DAY", 6, 1, 48),
                facebook_groups_per_day=_bounded_int("SAFETY_MAX_FACEBOOK_GROUPS_PER_DAY", 60, 1, 500),
                runs_per_day=_bounded_int("SAFETY_MAX_RUNS_PER_DAY", 40, 1, 500),
                breaker_failures=_bounded_int("SAFETY_BREAKER_FAILURES", 3, 1, 20),
                breaker_challenges=_bounded_int("SAFETY_BREAKER_CHALLENGES", 2, 1, 20),
                breaker_window_hours=_bounded_int("SAFETY_BREAKER_WINDOW_HOURS", 6, 1, 72),
            ),
            operator_user_ids=parse_user_ids(os.environ.get("TELEGRAM_OPERATOR_IDS", "")),
        )


def parse_user_ids(raw: str) -> frozenset[int]:
    """Parse ``123, 456`` strictly: a typo must stop startup, not lock operators out."""
    ids: set[int] = set()
    for part in raw.replace(",", " ").split():
        if not part.isdigit():
            raise ValueError(f"TELEGRAM_OPERATOR_IDS must be numeric Telegram user IDs, got {part!r}")
        ids.add(int(part))
    return frozenset(ids)


def _stt_provider(raw: str) -> str:
    provider = raw.strip().lower() or "openrouter"
    if provider not in SUPPORTED_STT_PROVIDERS:
        raise ValueError(f"STT_PROVIDER must be one of {sorted(SUPPORTED_STT_PROVIDERS)}, got {raw!r}")
    return provider


def _bounded_int(name: str, default: int, low: int, high: int) -> int:
    """Read an integer limit; out-of-range values stop startup instead of being clamped."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _public_url(raw: str) -> str:
    """HTTPS origin only: Telegram opens Mini Apps over HTTPS and nothing else."""
    url = raw.strip().rstrip("/")
    if not url:
        return ""
    if not url.startswith("https://") or "/" in url[len("https://"):] or "@" in url:
        raise ValueError(f"LIVE_VIEW_PUBLIC_URL must look like https://host, got {raw!r}")
    return url
