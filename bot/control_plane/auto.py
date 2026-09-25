"""Auto mode: trusted operators' /campaign, /run, /pause and /resume skip confirmation.

Off by default (AUTO_MODE in .env). Owners switch it at runtime with /auto;
the switch lives in PostgreSQL (migration 017, audited) so it survives
restarts, and .env only supplies the value used while no row exists.
Eligible: owners plus TELEGRAM_AUTO_OPERATOR_IDS, and only while they can
control collection (never a helper or a stranger). /cancel, /login, the
verification Mini App and owner administration stay manual, and the
Orchestra applies its quotas and breakers to auto-queued work as to any other.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from bot.control_plane.models import Reply
from bot.operators import OperatorSet

log = logging.getLogger(__name__)
AUTO_MODE_KEY = "auto_mode"
AUTO_COMMANDS = frozenset({"campaign", "run", "pause", "resume"})


class SettingsStore(Protocol):
    async def get(self, key: str) -> tuple[str, int | None, datetime | None] | None: ...
    async def put(self, key: str, value: str, user_id: int) -> None: ...


class MemorySettingsStore:
    """Test/local fallback; production uses PostgresSettingsStore."""

    def __init__(self) -> None:
        self.values: dict[str, tuple[str, int | None, datetime | None]] = {}

    async def get(self, key: str) -> tuple[str, int | None, datetime | None] | None:
        return self.values.get(key)

    async def put(self, key: str, value: str, user_id: int) -> None:
        self.values[key] = (value, user_id, datetime.now(UTC))


class PostgresSettingsStore:
    """Shares the control plane's pool; every change is audited as telegram:<id>."""

    def __init__(self, pool_owner: Any) -> None:
        self._owner = pool_owner

    async def get(self, key: str) -> tuple[str, int | None, datetime | None] | None:
        row = await self._owner._pool().fetchrow(
            "select value #>> '{}', updated_by, updated_at from public.control_settings where key = $1", key)
        return (row[0], row[1], row[2]) if row else None

    async def put(self, key: str, value: str, user_id: int) -> None:
        async with self._owner._pool().acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", f"telegram:{user_id}")
            await conn.execute(
                """insert into public.control_settings (key, value, updated_by) values ($1, to_jsonb($2::text), $3)
                   on conflict (key) do update set value = excluded.value, updated_by = excluded.updated_by, updated_at = now()""",
                key, value, user_id,
            )


class AutoMode:
    def __init__(self, store: SettingsStore, *, default: bool, operators: OperatorSet, auto_operator_ids: frozenset[int]) -> None:
        self.store, self.default, self.operators, self.auto_operator_ids = store, default, operators, auto_operator_ids

    def eligible(self, user_id: int | None) -> bool:
        """Owners and listed auto-operators, and only while they may control collection."""
        return (self.operators.is_owner(user_id) or user_id in self.auto_operator_ids) and self.operators.can_control(user_id)

    async def enabled(self) -> bool:
        try:
            stored = await self.store.get(AUTO_MODE_KEY)
        except Exception as exc:  # noqa: BLE001 - an unreadable switch falls back to confirmation
            log.warning("telegram.control.auto_read_failed", extra={"error": type(exc).__name__})
            return False
        return self.default if stored is None else stored[0] == "on"

    async def applies_to(self, user_id: int | None) -> bool:
        return self.eligible(user_id) and await self.enabled()

    async def command(self, user_id: int | None, argument: str) -> Reply:
        """Owner-only ``/auto on|off|status``."""
        if not self.operators.is_owner(user_id):
            return Reply("Only an owner can switch auto mode.")
        assert user_id is not None
        action = argument.strip().lower() or "status"
        if action in {"on", "off"}:
            await self.store.put(AUTO_MODE_KEY, action, user_id)
            log.info("telegram.control.auto_switched", extra={"value": action, "user_id": user_id})
            if action == "on":
                return Reply("Auto mode is on: /campaign, /run, /pause and /resume (and goals written or said as plain text) "
                             "from auto-operators are queued without confirmation. /cancel still asks; quotas and breakers still apply.")
            return Reply("Auto mode is off: every state-changing command needs confirmation again.")
        if action != "status":
            return Reply("Use /auto on, /auto off or /auto status.")
        stored = await self.store.get(AUTO_MODE_KEY)
        if stored is None:
            state = f"{'on' if self.default else 'off'} (AUTO_MODE default from .env)"
        else:
            when = stored[2].strftime("%Y-%m-%d %H:%M UTC") if stored[2] else "unknown time"
            state = f"{stored[0]} (set by {stored[1]} at {when})"
        listed = sorted(self.auto_operator_ids - self.operators.owners)
        active = [str(u) for u in listed if self.operators.can_control(u)]
        ignored = [str(u) for u in listed if not self.operators.can_control(u)]
        text = f"Auto mode: {state}.\nAuto-operators: owners{''.join(', ' + u for u in active)}."
        if ignored:
            text += f"\nIgnored (not an owner or operator): {', '.join(ignored)}."
        return Reply(text)
