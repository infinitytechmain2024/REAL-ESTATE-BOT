"""Durable campaign records; state changes follow the migration 014 state machine."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from .models import TERMINAL_STATES, TRANSITIONS, Campaign, CampaignPlan, can_transition

if TYPE_CHECKING:
    import asyncpg

STORE_ACTOR = "campaign:store"
MAX_SOURCE_TEXT = 2000


class CampaignStore(Protocol):
    async def create(self, plan: CampaignPlan, *, chat_id: int, requested_by: int,
                     source_text: str, actor: str) -> str: ...

    async def get(self, campaign_id: str) -> Campaign | None: ...

    async def set_state(self, campaign_id: str, state: str, actor: str, reason: str | None = None) -> bool: ...

    async def set_status_message(self, campaign_id: str, message_id: int, *, actor: str = STORE_ACTOR) -> bool: ...


def _check_create(source_text: str) -> None:
    if not source_text or len(source_text) > MAX_SOURCE_TEXT:
        raise ValueError(f"source_text must be 1..{MAX_SOURCE_TEXT} characters")


def _check_state(state: str) -> None:
    if state not in TRANSITIONS:
        raise ValueError(f"unknown campaign state: {state}")


def _uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        return None


class PostgresCampaignStore:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self.pool = pool

    async def create(self, plan: CampaignPlan, *, chat_id: int, requested_by: int,
                     source_text: str, actor: str) -> str:
        _check_create(source_text)
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn, actor)
            return await conn.fetchval(
                """insert into campaigns (telegram_chat_id, requested_by, source_text, plan, plan_version)
                   values ($1, $2, $3, $4::jsonb, $5) returning id::text""",
                chat_id, requested_by, source_text, plan.model_dump_json(), plan.plan_version,
            )

    async def get(self, campaign_id: str) -> Campaign | None:
        key = _uuid(campaign_id)
        if key is None:
            return None
        row = await self.pool.fetchrow(
            """select id::text, plan::text, state, telegram_chat_id, requested_by, source_text,
                      status_message_id, stop_reason, created_at, finished_at
               from campaigns where id = $1::uuid""",
            key,
        )
        return _campaign(row) if row else None

    async def set_state(self, campaign_id: str, state: str, actor: str, reason: str | None = None) -> bool:
        """Move to ``state`` only from a state that may legally reach it; False otherwise."""
        _check_state(state)
        key = _uuid(campaign_id)
        sources = [s for s, targets in TRANSITIONS.items() if state in targets]
        if key is None or not sources:
            return False
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn, actor)
            row = await conn.fetchrow(
                """update campaigns set state = $2, stop_reason = coalesce($3, stop_reason)
                   where id = $1::uuid and state = any($4::text[]) returning id""",
                key, state, reason, sources,
            )
        return row is not None

    async def set_status_message(self, campaign_id: str, message_id: int, *, actor: str = STORE_ACTOR) -> bool:
        key = _uuid(campaign_id)
        if key is None:
            return False
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn, actor)
            row = await conn.fetchrow(
                "update campaigns set status_message_id = $2 where id = $1::uuid returning id", key, message_id,
            )
        return row is not None


class MemoryCampaignStore:
    """In-process twin of ``PostgresCampaignStore`` for tests and dry runs."""

    def __init__(self) -> None:
        self.campaigns: dict[str, Campaign] = {}
        self.audit: list[tuple[str, str, str | None, str]] = []  # (id, actor, old_state, new_state)

    async def create(self, plan: CampaignPlan, *, chat_id: int, requested_by: int,
                     source_text: str, actor: str) -> str:
        _check_create(source_text)
        campaign_id = str(uuid.uuid4())
        self.campaigns[campaign_id] = Campaign(
            id=campaign_id, plan=plan, state="planned", chat_id=chat_id, requested_by=requested_by,
            source_text=source_text, status_message_id=None, stop_reason=None, created_at=datetime.now(UTC),
        )
        self.audit.append((campaign_id, actor, None, "planned"))
        return campaign_id

    async def get(self, campaign_id: str) -> Campaign | None:
        return self.campaigns.get(campaign_id)

    async def set_state(self, campaign_id: str, state: str, actor: str, reason: str | None = None) -> bool:
        _check_state(state)
        current = self.campaigns.get(campaign_id)
        if current is None or not can_transition(current.state, state):
            return False
        changes: dict[str, Any] = {"state": state, "stop_reason": reason or current.stop_reason}
        if state in TERMINAL_STATES:
            changes["finished_at"] = datetime.now(UTC)
        self.campaigns[campaign_id] = replace(current, **changes)
        self.audit.append((campaign_id, actor, current.state, state))
        return True

    async def set_status_message(self, campaign_id: str, message_id: int, *, actor: str = STORE_ACTOR) -> bool:
        current = self.campaigns.get(campaign_id)
        if current is None:
            return False
        self.campaigns[campaign_id] = replace(current, status_message_id=message_id)
        return True


def _campaign(row: asyncpg.Record) -> Campaign:
    return Campaign(
        id=row["id"], plan=CampaignPlan.model_validate_json(row["plan"]), state=row["state"],
        chat_id=row["telegram_chat_id"], requested_by=row["requested_by"], source_text=row["source_text"],
        status_message_id=row["status_message_id"], stop_reason=row["stop_reason"],
        created_at=row["created_at"], finished_at=row["finished_at"],
    )


async def _set_actor(conn: asyncpg.Connection[asyncpg.Record], actor: str) -> None:
    """Attribute every audit row written by this transaction."""
    await conn.execute("select set_config('app.actor', $1, true)", actor)
