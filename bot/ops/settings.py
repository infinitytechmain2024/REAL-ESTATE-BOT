"""Environment of the ``watchdog`` service. Off by default; secrets are never logged."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class WatchdogSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    enabled: bool = Field(default=False, validation_alias="WATCHDOG_ENABLED")
    # Optional override: one Telegram chat (e.g. a group) that receives alerts and the daily summary instead of the
    # bot owners. 0 (default): alerts go to every id of TELEGRAM_OPERATOR_IDS.
    owner_chat_id: int = Field(default=0, validation_alias="OWNER_ALERT_CHAT_ID", repr=False)
    operator_ids: Annotated[tuple[int, ...], NoDecode] = Field(default=(), validation_alias="TELEGRAM_OPERATOR_IDS",
                                                               repr=False)
    poll_seconds: int = Field(default=60, ge=5, le=3600, validation_alias="WATCHDOG_POLL_SECONDS")
    cooldown_minutes: int = Field(default=30, ge=1, le=1440, validation_alias="WATCHDOG_ALERT_COOLDOWN_MINUTES")
    summary_hour: int = Field(default=9, ge=0, le=23, validation_alias="WATCHDOG_DAILY_SUMMARY_HOUR")
    # Path of the Docker Engine socket inside the container; empty: containers are not checked.
    docker_socket: str = Field(default="", validation_alias="WATCHDOG_DOCKER_SOCKET")
    # Shared with the other services.
    database_url: str = Field(default="", validation_alias="DATABASE_URL", repr=False)
    telegram_token: str = Field(default="", validation_alias="TELEGRAM_TOKEN", repr=False)
    budget_usd: float = Field(default=5.0, ge=0, le=10_000, validation_alias="CAMPAIGN_BUDGET_USD")

    @field_validator("operator_ids", mode="before")
    @classmethod
    def _parse_operator_ids(cls, value: object) -> object:
        """Strict ``123, 456`` as in control_plane: a typo must stop startup, not silence the alerts."""
        if not isinstance(value, str):
            return value
        ids: list[int] = []
        for part in value.replace(",", " ").split():
            if not part.isdigit():
                raise ValueError(f"TELEGRAM_OPERATOR_IDS must be numeric Telegram user IDs, got {part!r}")
            if int(part) not in ids:
                ids.append(int(part))
        return tuple(ids)

    def recipients(self) -> tuple[int, ...]:
        """OWNER_ALERT_CHAT_ID when set (manual override), else every id of TELEGRAM_OPERATOR_IDS."""
        return (self.owner_chat_id,) if self.owner_chat_id != 0 else self.operator_ids

    def missing(self) -> list[str]:
        """Why the service must stay idle: switched off, or a required setting is empty (names only)."""
        if not self.enabled:
            return ["WATCHDOG_ENABLED"]
        return [name for name, ok in (("OWNER_ALERT_CHAT_ID|TELEGRAM_OPERATOR_IDS", bool(self.recipients())),
                                      ("TELEGRAM_TOKEN", bool(self.telegram_token)),
                                      ("DATABASE_URL", bool(self.database_url))) if not ok]
