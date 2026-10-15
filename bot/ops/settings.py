"""Environment of the ``watchdog`` service. Off by default; secrets are never logged."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WatchdogSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    enabled: bool = Field(default=False, validation_alias="WATCHDOG_ENABLED")
    # Telegram chat that receives alerts and the daily summary; 0: nowhere to send, the service idles.
    owner_chat_id: int = Field(default=0, validation_alias="OWNER_ALERT_CHAT_ID", repr=False)
    poll_seconds: int = Field(default=60, ge=5, le=3600, validation_alias="WATCHDOG_POLL_SECONDS")
    cooldown_minutes: int = Field(default=30, ge=1, le=1440, validation_alias="WATCHDOG_ALERT_COOLDOWN_MINUTES")
    summary_hour: int = Field(default=9, ge=0, le=23, validation_alias="WATCHDOG_DAILY_SUMMARY_HOUR")
    # Path of the Docker Engine socket inside the container; empty: containers are not checked.
    docker_socket: str = Field(default="", validation_alias="WATCHDOG_DOCKER_SOCKET")
    # Shared with the other services.
    database_url: str = Field(default="", validation_alias="DATABASE_URL", repr=False)
    telegram_token: str = Field(default="", validation_alias="TELEGRAM_TOKEN", repr=False)
    budget_usd: float = Field(default=5.0, ge=0, le=10_000, validation_alias="CAMPAIGN_BUDGET_USD")

    def missing(self) -> list[str]:
        """Why the service must stay idle: switched off, or a required setting is empty (names only)."""
        if not self.enabled:
            return ["WATCHDOG_ENABLED"]
        return [name for name, ok in (("OWNER_ALERT_CHAT_ID", self.owner_chat_id != 0),
                                      ("TELEGRAM_TOKEN", bool(self.telegram_token)),
                                      ("DATABASE_URL", bool(self.database_url))) if not ok]
