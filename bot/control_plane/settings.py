"""Environment-only configuration for the isolated Telegram control plane."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ControlPlaneSettings:
    telegram_token: str
    database_url: str
    stt_model: str = "small"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    max_voice_mb: float = 20.0
    confirmation_ttl_seconds: int = 300
    orchestra_command_lease_seconds: int = 30
    orchestra_poll_seconds: float = 1.0
    orchestra_stale_batch_seconds: int = 900
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
            stt_model=os.environ.get("FASTER_WHISPER_MODEL", "small"),
            stt_device=os.environ.get("FASTER_WHISPER_DEVICE", "cpu"),
            stt_compute_type=os.environ.get("FASTER_WHISPER_COMPUTE_TYPE", "int8"),
            max_voice_mb=float(os.environ.get("TELEGRAM_MAX_VOICE_MB", "20")),
            confirmation_ttl_seconds=int(os.environ.get("TELEGRAM_CONFIRMATION_TTL_SECONDS", "300")),
            orchestra_command_lease_seconds=int(os.environ.get("ORCHESTRA_COMMAND_LEASE_SECONDS", "30")),
            orchestra_poll_seconds=float(os.environ.get("ORCHESTRA_POLL_SECONDS", "1")),
            orchestra_stale_batch_seconds=int(os.environ.get("ORCHESTRA_STALE_BATCH_SECONDS", "900")),
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
