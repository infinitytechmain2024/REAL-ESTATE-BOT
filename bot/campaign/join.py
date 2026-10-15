"""«Вступил ✅» under a closed-group notice (CAMPAIGN_JOIN_NOTICES): the one write the control plane makes.

Kept apart from ``runs`` so the telegram image, which copies ``bot/campaign`` only for planning, imports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


async def confirm_group_join(pool: asyncpg.Pool[asyncpg.Record], group_id: str) -> str:
    """«Вступил»: ``queued`` (read again: queued, no batch, activity UNKNOWN), ``ended`` (the campaign is over: nothing
    is queued), ``stale`` (unknown group, no notice yet, or already confirmed)."""
    from .models import TERMINAL_STATES

    async with pool.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """select c.state, g.join_notice_at is not null as noticed, g.join_confirmed_at is not null as confirmed
                 from campaign_groups g join campaigns c on c.id = g.campaign_id where g.id = $1::uuid for update of g""",
            group_id)
        if row is None or not row["noticed"] or row["confirmed"]:
            return "stale"
        if row["state"] in TERMINAL_STATES:
            return "ended"
        await conn.execute(
            """update campaign_groups set join_confirmed_at = now(), state = 'queued', activity = 'UNKNOWN',
                                          batch_id = null, reject_reason = null where id = $1::uuid""", group_id)
        return "queued"
