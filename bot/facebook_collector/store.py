"""PostgreSQL persistence for the dedicated Facebook batch collector."""

from __future__ import annotations

import hashlib
import json

import asyncpg

from .models import BatchCancelled, BatchItem, BatchPlan, CollectedPost, GroupState


class PostgresCollectorStore:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self.pool = pool

    async def load_plan(self, batch_id: str) -> BatchPlan:
        row = await self.pool.fetchrow(
            """select b.id, b.max_items, p.id profile_id, p.profile_name, p.state profile_state
               from acquisition_batches b join browser_profiles p on p.platform = b.platform
              where b.id = $1 and b.platform = 'facebook' and b.acquisition_method = 'facebook_connector'
                and b.state = 'queued' and p.state = 'ready'
              order by p.created_at limit 1""", batch_id,
        )
        if not row:
            raise ValueError("queued Facebook batch with a ready Facebook profile was not found")
        rows = await self.pool.fetch(
            """select i.id, i.source_id, s.canonical_url, i.sequence_no
                 from acquisition_batch_items i join monitoring_sources s on s.id = i.source_id
                where i.batch_id = $1 and i.state = 'queued' and s.platform = 'facebook'
                order by i.sequence_no""", batch_id,
        )
        if not rows or len(rows) > row["max_items"] or len(rows) > 20:
            raise ValueError("Facebook batch must contain between 1 and 20 queued items")
        return BatchPlan(str(row["id"]), str(row["profile_id"]), row["profile_name"], row["profile_state"], row["max_items"], tuple(
            BatchItem(str(item["id"]), str(item["source_id"]), item["canonical_url"], item["sequence_no"]) for item in rows
        ))

    async def start(self, plan: BatchPlan) -> str:
        async with self.pool.acquire() as conn, conn.transaction():
            claimed = await conn.execute("update acquisition_batches set state='running', started_at=now() where id=$1 and state='queued'", plan.id)
            if not claimed.endswith("1"):
                raise ValueError("batch could not be claimed")
            batch_run = await conn.fetchval(
                "insert into batch_runs(batch_id,browser_profile_id,state) values($1,$2,'queued') returning id", plan.id, plan.browser_profile_id
            )
            await conn.execute("update batch_runs set state='running', started_at=now() where id=$1", batch_run)
            await conn.execute("update browser_profiles set state='in_use', last_used_at=now() where id=$1 and state='ready'", plan.browser_profile_id)
            return str(batch_run)

    async def start_item(self, item: BatchItem, batch_run_id: str, profile_id: str, *, max_runtime_seconds: int) -> str:
        async with self.pool.acquire() as conn, conn.transaction():
            result = await conn.execute("update acquisition_batch_items set state='running', attempt_count=attempt_count+1, started_at=now() where id=$1 and state='queued'", item.id)
            if not result.endswith("1"):
                if await conn.fetchval("select state from acquisition_batches where id=(select batch_id from acquisition_batch_items where id=$1)", item.id) == "cancelled":
                    raise BatchCancelled(item.id)
                raise ValueError("batch item could not be claimed")
            run_id = await conn.fetchval(
                """insert into acquisition_runs(source_id,batch_item_id,batch_run_id,browser_profile_id,acquisition_method,state,max_pages,max_runtime_seconds,allowed_skills)
                   values($1,$2,$3,$4,'facebook_connector','queued',1,$5,'[\"facebook_group_snapshot\"]'::jsonb) returning id""",
                item.source_id, item.id, batch_run_id, profile_id, max_runtime_seconds,
            )
            await conn.execute("update acquisition_runs set state='running', started_at=now() where id=$1", run_id)
            return str(run_id)

    async def save_post(self, source_id: str, run_id: str, post: CollectedPost) -> None:
        content_hash = hashlib.sha256((post.canonical_url + "\n" + post.body_text).encode()).hexdigest()
        await self.pool.execute(
            """insert into collected_posts(source_id,acquisition_run_id,platform_post_id,canonical_url,body_text,published_at,content_hash,raw_payload)
               values($1,$2,$3,$4,$5,$6::timestamptz,$7,'{}'::jsonb)
               on conflict (source_id,platform_post_id) do nothing""",
            source_id, run_id, post.platform_post_id, post.canonical_url, post.body_text, post.published_at, content_hash,
        )

    async def finish_item(self, item: BatchItem, run_id: str, state: str, group_state: GroupState, detail: str | None = None, *, diagnostics: dict[str, object] | None = None) -> None:
        metadata = json.dumps({
            "facebook_group_state": group_state.value, "facebook_group_state_updated_at": "now",
            # Latest read only (overwritten each run): counts, final URL, title, screenshot name.
            "facebook_last_read": {"run_id": run_id, "state": state, **(diagnostics or {})},
        })
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("update acquisition_runs set state=$2, finished_at=now(), error_code=$3 where id=$1 and state='running'", run_id, state, detail)
            await conn.execute("update acquisition_batch_items set state=$2, finished_at=now(), last_error_code=$3 where id=$1 and state='running'", item.id, state, detail)
            await conn.execute("update monitoring_sources set configuration=configuration || $2::jsonb where id=$1", item.source_id, metadata)

    async def finish_batch(self, plan: BatchPlan, batch_run_id: str, state: str, reason: str | None = None) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            # A batch cancelled mid-run stays cancelled; only the collector's own
            # running rows are closed, and the profile is always handed back.
            await conn.execute("update batch_runs set state=$2, finished_at=now(), stop_reason=$3 where id=$1 and state='running'", batch_run_id, state, reason)
            await conn.execute(
                "update acquisition_batches set state=$2, finished_at=now() where id=$1 and state='running'",
                plan.id, state if state in {"succeeded", "cancelled"} else "failed",
            )
            await conn.execute("update browser_profiles set state='ready' where id=$1 and state='in_use'", plan.browser_profile_id)

    async def challenge(self, plan: BatchPlan, batch_run_id: str, item: BatchItem, run_id: str, reason: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute("update acquisition_runs set state='awaiting_human_verification', stop_reason=$2 where id=$1", run_id, reason)
            await conn.execute("update acquisition_batch_items set state='awaiting_human_verification', last_error_code='facebook_challenge' where id=$1", item.id)
            await conn.execute("update batch_runs set state='human_verification_required', stop_reason=$2 where id=$1 and state='running'", batch_run_id, reason)
            await conn.execute("update acquisition_batches set state='human_verification_required' where id=$1 and state='running'", plan.id)
            await conn.execute("update monitoring_sources set state='human_verification_required' where id=$1 and state='active'", item.source_id)
            await conn.execute("update browser_profiles set state='human_verification_required' where id=$1 and state='in_use'", plan.browser_profile_id)
            await conn.execute(
                """insert into verification_jobs(source_id,acquisition_run_id,job_type,state,requested_by,resolution_note)
                   values($1,$2,'facebook_challenge','requested','facebook_batch_collector',$3)
                   on conflict (source_id,job_type) where state in ('requested','active') do nothing""",
                item.source_id, run_id, reason,
            )
