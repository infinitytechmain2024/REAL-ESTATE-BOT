"""Environment-only configuration. Anything unsafe stops startup."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class VerificationSettings:
    database_url: str = field(repr=False)
    telegram_token: str = field(repr=False)
    operator_ids: frozenset[int]
    owner_id: int
    # https://<host> that Caddy serves /verify/* on; empty keeps the flow off.
    public_url: str
    browser_session_url: str = "http://browser:8090"
    browser_session_api_token: str = field(default="", repr=False)
    novnc_url: str = "http://browser:6080"
    # Reached only by Caddy on the private Docker network; no host port.
    listen_host: str = "0.0.0.0"
    listen_port: int = 8095
    token_minutes: int = 15
    session_minutes: int = 30
    job_hours: int = 24
    renotify_minutes: int = 30
    live_minutes: int = 20
    poll_seconds: int = 30

    @classmethod
    def from_env(cls) -> VerificationSettings:
        env = os.environ
        operators = _ids(env.get("TELEGRAM_OPERATOR_IDS", ""), "TELEGRAM_OPERATOR_IDS")
        if not operators:
            raise ValueError("TELEGRAM_OPERATOR_IDS must name at least one operator")
        owner_raw = env.get("VERIFICATION_OWNER_TELEGRAM_ID", "").strip()
        owner = int(owner_raw) if owner_raw.isdigit() else min(operators)
        if owner not in operators:
            raise ValueError("VERIFICATION_OWNER_TELEGRAM_ID must also be in TELEGRAM_OPERATOR_IDS")
        public = env.get("VERIFICATION_PUBLIC_URL", "").strip() or env.get("LIVE_VIEW_PUBLIC_URL", "").strip()
        return cls(
            database_url=_required(env, "DATABASE_URL"),
            telegram_token=_required(env, "TELEGRAM_TOKEN"),
            operator_ids=operators,
            owner_id=owner,
            public_url=https_origin(public) if public else "",
            browser_session_url=env.get("BROWSER_SESSION_URL", "").strip() or "http://browser:8090",
            browser_session_api_token=_required(env, "BROWSER_SESSION_API_TOKEN"),
            novnc_url=env.get("VERIFICATION_NOVNC_URL", "").strip() or "http://browser:6080",
            token_minutes=_bounded(env, "VERIFICATION_TOKEN_MINUTES", 15, 2, 60),
            session_minutes=_bounded(env, "VERIFICATION_SESSION_MINUTES", 30, 5, 120),
            job_hours=_bounded(env, "VERIFICATION_JOB_HOURS", 24, 1, 168),
            renotify_minutes=_bounded(env, "VERIFICATION_RENOTIFY_MINUTES", 30, 5, 1440),
            live_minutes=_bounded(env, "VERIFICATION_LIVE_MINUTES", 20, 5, 60),
            poll_seconds=_bounded(env, "VERIFICATION_POLL_SECONDS", 30, 5, 600),
        )


def https_origin(raw: str) -> str:
    """Telegram opens Mini Apps over HTTPS only, and the page lives at the origin's /verify."""
    url = raw.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.path or parts.query or parts.username:
        raise ValueError(f"VERIFICATION_PUBLIC_URL must look like https://host, got {raw!r}")
    return url


def _required(env: os._Environ[str] | dict[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _ids(raw: str, name: str) -> frozenset[int]:
    parts = raw.replace(",", " ").split()
    if not all(p.isdigit() for p in parts):
        raise ValueError(f"{name} must be numeric Telegram user IDs")
    return frozenset(int(p) for p in parts)


def _bounded(env: os._Environ[str] | dict[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}, got {raw!r}")
    return int(raw)
