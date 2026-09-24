from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .models import Evidence

if TYPE_CHECKING:
    pass


class PostgresAnalysisStore:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool = None

    async def connect(self):
        import asyncpg

        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=3)

    async def close(self):
        if self.pool:
            await self.pool.close()

    def _pool(self):
        if not self.pool:
            raise RuntimeError("store not connected")
        return self.pool

    async def pending(self, limit: int):
        async with self._pool().acquire() as c:
            rows = await c.fetch(
                """select p.id, p.source_id, p.canonical_url, coalesce(p.body_text,'') as body_text, coalesce(p.raw_payload->>'title','') as title, p.published_at, s.vertical
              from collected_posts p join monitoring_sources s on s.id=p.source_id
              where p.state='normalised' and s.state='active' and s.vertical in ('real_estate','investors')
              order by p.collected_at for update of p skip locked limit $1""",
                limit,
            )
            return rows

    async def save(self, evidence: Evidence, vertical: str, outcome, model: str):
        async with self._pool().acquire() as c, c.transaction():
            await c.execute("select set_config('app.actor','analysis_pipeline',true)")
            if not outcome.accepted:
                await c.execute(
                    "update collected_posts set state='rejected' where id=$1::uuid and state='normalised'",
                    evidence.post_id,
                )
                return None
            payload = {
                "schema_version": "analysis-v1",
                "summary": outcome.result.summary,
                "location": outcome.result.location,
                "price_signals": outcome.result.price_signals,
                "original_post_link": evidence.canonical_url,
                "related_links": outcome.result.related_links,
                "formatted": outcome.formatted,
            }
            key = (
                __import__("hashlib").sha256(f"{vertical}:{evidence.post_id}".encode()).hexdigest()
            )
            row = await c.fetchrow(
                """insert into findings(vertical,source_id,post_id,finding_type,dedupe_key,structured_payload,confidence,state,analysis_metadata)
                 values($1,$2::uuid,$3::uuid,$4,$5,$6::jsonb,$7,'ready',$8::jsonb)
                 on conflict(dedupe_key) do update set structured_payload=excluded.structured_payload, confidence=excluded.confidence, analysis_metadata=excluded.analysis_metadata
                 returning id""",
                vertical,
                evidence.source_id,
                evidence.post_id,
                "real_estate_proposition" if vertical == "real_estate" else "investor_lead",
                key,
                json.dumps(payload),
                outcome.result.confidence,
                json.dumps(
                    {"prompt_version": "analysis-v1", "model": model, "language": outcome.language}
                ),
            )
            await c.execute(
                "update collected_posts set state='analysed' where id=$1::uuid and state='normalised'",
                evidence.post_id,
            )
            return str(row["id"])

    async def save_digest(self, chat_id: int, vertical: str, finding_ids: list[str], body: str):
        key = (
            __import__("hashlib")
            .sha256(f"{chat_id}:{vertical}:{','.join(sorted(finding_ids))}".encode())
            .hexdigest()
        )
        async with self._pool().acquire() as c, c.transaction():
            row = await c.fetchrow(
                """insert into analysis_digests(idempotency_key,telegram_chat_id,vertical,finding_ids,body)
            values($1,$2,$3,$4::jsonb,$5) on conflict(idempotency_key) do update set idempotency_key=excluded.idempotency_key returning id,(xmax<>0) duplicate""",
                key,
                chat_id,
                vertical,
                json.dumps(finding_ids),
                body,
            )
            return str(row["id"]), bool(row["duplicate"])

    async def mark_digest_sent(self, digest_id: str, telegram_message_id: int) -> None:
        async with self._pool().acquire() as c, c.transaction():
            await c.execute(
                "update analysis_digests set state='sent', telegram_message_id=$2, sent_at=now(), updated_at=now() where id=$1::uuid and state='queued'",
                digest_id,
                telegram_message_id,
            )
