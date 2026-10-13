"""PostgreSQL persistence of the Apify provider (migration 046): the same batch tables as the browser collector, plus
``apify_runs`` / ``apify_raw_items``.

Differences from ``PostgresCollectorStore`` that the provider relies on:

* no browser profile is claimed (the batch run has none);
* every group gets its ``acquisition_runs`` row (method ``apify``) when the batch starts, because the dataset mixes
  the groups' posts; the batch items themselves stay ``queued`` until they finish, so the one-running-item-per-batch
  index of migration 003 is never touched;
* posts are inserted exactly like the browser collector's (``collected_posts``, state ``normalised``, the same
  unique keys), so a post read by both providers is one row.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

from ..models import BatchItem, BatchPlan, GroupState
from .provider import LAUNCH_UNKNOWN, ApifyRun

ACTOR = "facebook_apify"
_TERMINAL = ("SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED", LAUNCH_UNKNOWN)
_RUN_FIELDS = {"status", "dataset_id", "import_checkpoint", "items_imported", "billed_usd", "error_message"}


class PostgresApifyFacebookStore:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record], *, max_runtime_seconds: int = 600) -> None:
        self.pool = pool
        self.max_runtime_seconds = max(1, min(3600, int(max_runtime_seconds)))

    async def load_plan(self, batch_id: str) -> BatchPlan:
        row = await self.pool.fetchrow(
            """select id, max_items from acquisition_batches
                where id = $1::uuid and platform = 'facebook' and state = 'queued'""", batch_id)
        if not row:
            raise ValueError("queued Facebook batch was not found")
        rows = await self.pool.fetch(
            """select i.id, i.source_id, s.canonical_url, i.sequence_no
                 from acquisition_batch_items i join monitoring_sources s on s.id = i.source_id
                where i.batch_id = $1::uuid and i.state = 'queued' and s.platform = 'facebook'
                order by i.sequence_no""", batch_id)
        if not rows or len(rows) > row["max_items"] or len(rows) > 20:
            raise ValueError("Facebook batch must contain between 1 and 20 queued items")
        return BatchPlan(str(row["id"]), "", "", "", row["max_items"], tuple(
            BatchItem(str(r["id"]), str(r["source_id"]), r["canonical_url"], r["sequence_no"]) for r in rows))

    async def campaign_of(self, batch_id: str) -> str | None:
        value = await self.pool.fetchval("select campaign_id::text from acquisition_batches where id = $1::uuid", batch_id)
        return str(value) if value else None

    async def start(self, plan: BatchPlan) -> str:
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            claimed = await conn.execute(
                "update acquisition_batches set state = 'running', started_at = now() where id = $1::uuid and state = 'queued'",
                plan.id)
            if not claimed.endswith("1"):
                raise ValueError("batch could not be claimed")
            batch_run = await conn.fetchval(
                "insert into batch_runs (batch_id, browser_profile_id, state) values ($1::uuid, null, 'queued') returning id",
                plan.id)
            await conn.execute("update batch_runs set state = 'running', started_at = now() where id = $1", batch_run)
            return str(batch_run)

    async def start_item(self, item: BatchItem, batch_run_id: str) -> str:
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            await conn.execute(
                """update acquisition_batch_items set attempt_count = least(attempt_count + 1, 10)
                    where id = $1::uuid and state = 'queued'""", item.id)
            run_id = await conn.fetchval(
                """insert into acquisition_runs (source_id, batch_item_id, batch_run_id, acquisition_method, state,
                                                 max_pages, max_runtime_seconds, allowed_skills)
                   values ($1::uuid, $2::uuid, $3::uuid, 'apify', 'queued', 1, $4,
                           '["apify_facebook_groups"]'::jsonb) returning id""",
                item.source_id, item.id, batch_run_id, self.max_runtime_seconds)
            await conn.execute("update acquisition_runs set state = 'running', started_at = now() where id = $1", run_id)
            return str(run_id)

    async def get_run(self, batch_id: str) -> ApifyRun | None:
        row = await self.pool.fetchrow(
            """select batch_id::text, run_id, status, dataset_id, import_checkpoint, items_imported
                 from apify_runs where batch_id = $1::uuid""", batch_id)
        if row is None:
            return None
        return ApifyRun(row["batch_id"], row["run_id"], row["status"], row["dataset_id"], row["import_checkpoint"],
                        row["items_imported"])

    async def save_run(self, batch_id: str, campaign_id: str | None, *, actor_id: str, build: str, run_id: str,
                       status: str, input_hash: str, dataset_id: str | None = None) -> None:
        await self.pool.execute(
            """insert into apify_runs (batch_id, campaign_id, actor_id, actor_build, run_id, dataset_id, status, input_hash,
                                       finished_at)
               values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, case when $7 = any($9::text[]) then now() end)
               on conflict (batch_id) do nothing""",
            batch_id, campaign_id, actor_id, build, run_id, dataset_id, status, input_hash, list(_TERMINAL))

    async def update_run(self, run_id: str, /, **fields: Any) -> None:
        unknown = set(fields) - _RUN_FIELDS
        if unknown:
            raise ValueError(f"unknown apify_runs fields: {sorted(unknown)}")
        if not fields:
            return
        names = sorted(fields)
        sets = ", ".join(f"{name} = ${i}" for i, name in enumerate(names, start=2))
        finished = ", finished_at = coalesce(finished_at, now())" if fields.get("status") in _TERMINAL else ""
        await self.pool.execute(f"update apify_runs set {sets}{finished} where run_id = $1",
                                run_id, *(fields[n] for n in names))

    async def spent(self, campaign_id: str) -> float:
        value = await self.pool.fetchval(
            "select coalesce(sum(billed_usd), 0) from apify_runs where campaign_id = $1::uuid", campaign_id)
        return float(value or 0)

    async def save_raw(self, run_id: str, index: int, payload: dict[str, Any]) -> bool:
        return bool(await self.pool.fetchval(
            """insert into apify_raw_items (run_id, item_index, payload) values ($1, $2, $3::jsonb)
               on conflict (run_id, item_index) do nothing returning 1""",
            run_id, index, json.dumps(payload, ensure_ascii=False)))

    async def save_post(self, source_id: str, acquisition_run_id: str, row: dict[str, Any]) -> None:
        await self.pool.execute(
            """insert into collected_posts (source_id, acquisition_run_id, platform_post_id, canonical_url, body_text,
                                            published_at, content_hash, raw_payload, state)
               values ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8::jsonb, $9)
               on conflict do nothing""",
            source_id, acquisition_run_id, row["platform_post_id"], row["canonical_url"][:2048], row["body_text"],
            row["published_at"], row["content_hash"], json.dumps(row["raw_payload"], ensure_ascii=False), row["state"])

    async def finish_item(self, item: BatchItem, run_id: str, state: str, group_state: GroupState,
                          detail: str | None = None) -> None:
        metadata = json.dumps({"facebook_group_state": group_state.value, "facebook_group_state_updated_at": "now",
                               "facebook_last_read": {"run_id": run_id, "state": state, "provider": "apify",
                                                      **({"detail": detail} if detail else {})}})
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            await conn.execute(
                """update acquisition_runs set state = $2, finished_at = now(), error_code = $3
                    where id = $1::uuid and state = 'running'""", run_id, state, detail)
            # queued -> running -> final inside one transaction: the items finish one at a time, so the
            # one-running-item-per-batch index and the transition rules of migration 003 both hold.
            await conn.execute(
                """update acquisition_batch_items set state = 'running', started_at = coalesce(started_at, now())
                    where id = $1::uuid and state = 'queued'""", item.id)
            await conn.execute(
                """update acquisition_batch_items set state = $2, finished_at = now(), last_error_code = $3
                    where id = $1::uuid and state = 'running'""", item.id, state, detail)
            await conn.execute(
                "update monitoring_sources set configuration = configuration || $2::jsonb where id = $1::uuid",
                item.source_id, metadata)

    async def finish_batch(self, plan: BatchPlan, batch_run_id: str, state: str, reason: str | None = None) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            await conn.execute(
                """update batch_runs set state = $2, finished_at = now(), stop_reason = $3
                    where id = $1::uuid and state = 'running'""", batch_run_id, state, reason)
            await conn.execute(
                """update acquisition_batches set state = $2, finished_at = now()
                    where id = $1::uuid and state = 'running'""",
                plan.id, state if state in {"succeeded", "cancelled"} else "failed")


async def _actor(conn: asyncpg.Connection[asyncpg.Record]) -> None:
    await conn.execute("select set_config('app.actor', $1, true)", ACTOR)
