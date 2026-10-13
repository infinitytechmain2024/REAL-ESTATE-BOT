"""PostgreSQL persistence and analysis handoff for Apify Facebook runs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import asyncpg

from bot.acquisition.evidence import Evidence
from bot.utils import costs

from .provider import ApifyBatch, ApifyFacebookConfig


class PostgresApifyRunStore:
    def __init__(
        self,
        pool: asyncpg.Pool[asyncpg.Record],
        config: ApifyFacebookConfig,
    ) -> None:
        self.pool = pool
        self.config = config

    async def load_plan(self, batch_id: str) -> ApifyBatch:
        batch = await self.pool.fetchrow(
            """select b.id::text, b.campaign_id::text
                 from acquisition_batches b
                where b.id = $1::uuid and b.platform = 'facebook'
                  and b.acquisition_method = 'apify' and b.state in ('queued', 'running')""",
            batch_id,
        )
        if batch is None:
            raise ValueError("queued or running Apify Facebook batch was not found")
        urls = await self.pool.fetch(
            """select s.canonical_url
                 from acquisition_batch_items i
                 join monitoring_sources s on s.id = i.source_id
                where i.batch_id = $1::uuid order by i.sequence_no""",
            batch_id,
        )
        if not urls:
            raise ValueError("Apify Facebook batch has no source groups")
        run = await self.pool.fetchrow(
            """select run_id, dataset_id, import_checkpoint
                 from apify_runs where batch_id = $1::uuid and status = 'RUNNING'
                order by id desc limit 1""",
            batch_id,
        )
        return ApifyBatch(
            batch_id=batch["id"],
            campaign_id=batch["campaign_id"],
            group_urls=tuple(row["canonical_url"] for row in urls),
            run_id=run["run_id"] if run else None,
            dataset_id=run["dataset_id"] if run else None,
            checkpoint=run["import_checkpoint"] if run else 0,
        )

    async def save_run(
        self, batch_id: str, run_id: str, dataset_id: str | None, input_hash: str
    ) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            campaign_id = await conn.fetchval(
                "select campaign_id from acquisition_batches where id = $1::uuid for update", batch_id
            )
            if campaign_id is None:
                raise ValueError("Apify batch campaign was not found")
            await conn.execute(
                """insert into apify_runs
                       (campaign_id, batch_id, actor_id, actor_build, run_id, dataset_id, status, input_hash)
                   values ($1, $2::uuid, $3, $4, $5, $6, 'RUNNING', $7)
                   on conflict (run_id) do update set dataset_id = coalesce(excluded.dataset_id, apify_runs.dataset_id)""",
                campaign_id, batch_id, self.config.actor_id, self.config.actor_build,
                run_id, dataset_id, input_hash,
            )
            await conn.execute(
                """update acquisition_batches set state = 'running', started_at = coalesce(started_at, now())
                    where id = $1::uuid and state = 'queued'""",
                batch_id,
            )

    async def save_launch_unknown(self, batch_id: str, input_hash: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            campaign_id = await conn.fetchval(
                "select campaign_id from acquisition_batches where id = $1::uuid for update", batch_id
            )
            if campaign_id is None:
                raise ValueError("Apify batch campaign was not found")
            await conn.execute(
                """insert into apify_runs
                       (campaign_id, batch_id, actor_id, actor_build, status, input_hash,
                        finished_at, billed_usd, error_message)
                   values ($1, $2::uuid, $3, $4, 'LAUNCH_UNKNOWN', $5, now(), $6,
                           'launch_unknown')""",
                campaign_id, batch_id, self.config.actor_id, self.config.actor_build, input_hash,
                self.config.max_usd_per_run,
            )
            await self._finish_batch(conn, batch_id, "failed", "launch_unknown")
        await costs.record_unique(
            "api",
            key=f"apify-facebook-launch-unknown:{batch_id}",
            provider="apify",
            item=self.config.actor_id,
            cost_usd=self.config.max_usd_per_run,
            code="estimated_launch_unknown",
            campaign_id=str(campaign_id),
        )

    async def save_item(
        self,
        run_id: str,
        item_index: int,
        raw_item: dict[str, Any],
        evidence: Evidence,
    ) -> None:
        payload = json.dumps(raw_item, ensure_ascii=False)
        async with self.pool.acquire() as conn, conn.transaction():
            run = await conn.fetchrow(
                "select id, batch_id from apify_runs where run_id = $1 and status = 'RUNNING' for update",
                run_id,
            )
            if run is None:
                raise ValueError("running Apify audit row was not found")
            await conn.execute(
                """insert into apify_raw_items (run_id, item_index, payload)
                   values ($1, $2, $3::jsonb)
                   on conflict (run_id, item_index) do update set payload = excluded.payload""",
                run_id, item_index, payload,
            )
            source = await self._source(conn, run["batch_id"], evidence.source.group_url)
            if source is None or evidence.source.post_url is None:
                return
            acquisition_run_id = await conn.fetchval(
                """select id from acquisition_runs
                    where apify_run_id = $1 and source_id = $2 limit 1""",
                run["id"], source["source_id"],
            )
            if acquisition_run_id is None:
                acquisition_run_id = await conn.fetchval(
                    """insert into acquisition_runs
                           (source_id, batch_item_id, acquisition_method, state, max_pages,
                            max_runtime_seconds, allowed_skills, started_at, apify_run_id)
                       values ($1, $2, 'apify', 'running', 1, $3,
                               '["apify_facebook_dataset"]'::jsonb, now(), $4)
                       returning id""",
                    source["source_id"], source["item_id"], int(self.config.timeout_seconds), run["id"],
                )
                await conn.execute(
                    """update acquisition_batch_items
                          set state = 'running', attempt_count = attempt_count + 1,
                              started_at = coalesce(started_at, now())
                        where id = $1 and state = 'queued'""",
                    source["item_id"],
                )
            post_id = _post_id(raw_item, evidence.source.post_url, evidence.raw_text)
            content_hash = hashlib.sha256(
                (evidence.source.post_url + "\n" + evidence.raw_text).encode()
            ).hexdigest()
            await conn.execute(
                """insert into collected_posts
                       (source_id, acquisition_run_id, platform_post_id, canonical_url, body_text,
                        published_at, content_hash, raw_payload, state)
                   values ($1, $2, $3, $4, $5, $6, $7, '{}'::jsonb, 'normalised')
                   on conflict do nothing""",
                source["source_id"], acquisition_run_id, post_id, evidence.source.post_url,
                evidence.raw_text, evidence.source.published_at, content_hash,
            )

    async def checkpoint(self, run_id: str, offset: int) -> None:
        await self.pool.execute(
            """update apify_runs
                  set import_checkpoint = greatest(import_checkpoint, $2),
                      items_imported = greatest(items_imported, $2)
                where run_id = $1 and status = 'RUNNING'""",
            run_id, offset,
        )

    async def finish(
        self,
        run_id: str,
        status: str,
        item_count: int,
        billed_usd: float | None,
        detail: str | None = None,
    ) -> None:
        succeeded = status in {"succeeded", "succeeded_empty"}
        db_status = "SUCCEEDED" if succeeded else "FAILED"
        async with self.pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """update apify_runs
                      set status = $2, finished_at = now(), import_checkpoint = greatest(import_checkpoint, $3),
                          items_imported = greatest(items_imported, $3), billed_usd = $4, error_message = $5
                    where run_id = $1 and status = 'RUNNING'
                returning campaign_id::text, batch_id::text""",
                run_id, db_status, item_count, billed_usd, detail,
            )
            if row is None:
                return
            await conn.execute(
                """update acquisition_runs set state = $2, finished_at = now(), error_code = $3
                    where apify_run_id = (select id from apify_runs where run_id = $1)
                      and state = 'running'""",
                run_id, "succeeded" if succeeded else "failed", detail,
            )
            await self._finish_batch(
                conn, row["batch_id"], "succeeded" if succeeded else "failed", detail
            )
        if billed_usd is not None:
            await costs.record_unique(
                "api",
                key=f"apify-facebook:{run_id}",
                provider="apify",
                item=self.config.actor_id,
                cost_usd=billed_usd,
                campaign_id=row["campaign_id"],
            )

    @staticmethod
    async def _source(
        conn: asyncpg.Connection[asyncpg.Record], batch_id: Any, group_url: str | None
    ) -> asyncpg.Record | None:
        if group_url:
            row = await conn.fetchrow(
                """select i.id item_id, i.source_id
                     from acquisition_batch_items i join monitoring_sources s on s.id = i.source_id
                    where i.batch_id = $1 and rtrim(s.canonical_url, '/') = rtrim($2, '/')""",
                batch_id, group_url,
            )
            if row is not None:
                return row
        rows = await conn.fetch(
            "select id item_id, source_id from acquisition_batch_items where batch_id = $1", batch_id
        )
        return rows[0] if len(rows) == 1 else None

    @staticmethod
    async def _finish_batch(
        conn: asyncpg.Connection[asyncpg.Record], batch_id: str, state: str, detail: str | None
    ) -> None:
        item_state = "succeeded" if state == "succeeded" else "failed"
        await conn.execute(
            """update acquisition_batch_items
                  set state = $2, finished_at = now(), last_error_code = $3
                where batch_id = $1::uuid and state in ('queued', 'running')""",
            batch_id, item_state, detail,
        )
        await conn.execute(
            """update acquisition_batches set state = $2, finished_at = now()
                where id = $1::uuid and state in ('queued', 'running')""",
            batch_id, state,
        )


def _post_id(item: dict[str, Any], url: str, text: str) -> str:
    for key in ("postId", "id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return hashlib.sha256((url + "\n" + text).encode()).hexdigest()
