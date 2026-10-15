"""«Вступил ✅» under a closed-group notice (CAMPAIGN_JOIN_NOTICES): the one write the control plane makes.

Kept apart from ``runs`` so the telegram image, which copies ``bot/campaign`` only for planning, imports it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


async def confirm_group_join(pool: asyncpg.Pool[asyncpg.Record], group_id: str) -> bool:
    """«Вступил»: the group is read again (queued, no batch, activity UNKNOWN); only once, after its notice."""
    return bool(await pool.fetchval(
        """update campaign_groups set join_confirmed_at = now(), state = 'queued', activity = 'UNKNOWN',
                                      batch_id = null, reject_reason = null
            where id = $1::uuid and join_notice_at is not null and join_confirmed_at is null returning true""",
        group_id))
