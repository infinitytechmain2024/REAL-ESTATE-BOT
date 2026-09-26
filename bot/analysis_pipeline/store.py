from __future__ import annotations

import hashlib
import json
from typing import Any

from .formatters import finding_payload
from .models import Evidence
from .openrouter import PROMPT_VERSION

ACTOR = "analysis_pipeline"


class PostgresAnalysisStore:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.pool: Any = None

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

    async def pending(self, limit: int, *, claim_seconds: int = 300):
        """Claim up to ``limit`` normalised posts for this worker.

        The claim is a row value, not a lock, so it survives the OpenRouter
        call; an expired claim (worker died) is taken over by the next worker.
        Sources created by /run are ``both``: they are analysed for each vertical.
        """
        async with self._pool().acquire() as c, c.transaction():
            await c.execute("select set_config('app.actor',$1,true)", ACTOR)
            return await c.fetch(
                """with picked as (
                       select p.id from collected_posts p join monitoring_sources s on s.id=p.source_id
                        where p.state='normalised' and s.state='active' and s.deleted_at is null
                          and s.vertical in ('real_estate','investors','both')
                          and (p.analysis_claimed_at is null or p.analysis_claimed_at < now() - make_interval(secs => $2))
                        order by p.collected_at
                        for update of p skip locked limit $1)
                   update collected_posts p set analysis_claim_token=gen_random_uuid(), analysis_claimed_at=now()
                     from picked, monitoring_sources s
                    where p.id=picked.id and s.id=p.source_id
                returning p.id, p.source_id, p.canonical_url, coalesce(p.body_text,'') as body_text,
                          coalesce(nullif(p.raw_payload->>'title',''), s.configuration->'facebook_last_read'->>'title', '') as title, p.published_at, s.vertical,
                          p.analysis_claim_token,
                          coalesce((select jsonb_agg(c.body_text order by c.created_at)
                                      from (select body_text, created_at from collected_comments
                                             where post_id=p.id and state in ('collected','relevant') and body_text is not null
                                             order by created_at limit 10) c), '[]'::jsonb)::text as comments""",
                limit,
                claim_seconds,
            )

    async def save(self, evidence: Evidence, vertical: str, outcome, model: str, claim_token: str | None = None):
        """Store an accepted finding while this worker still holds the post's claim."""
        if not outcome.accepted:
            return None
        async with self._pool().acquire() as c, c.transaction():
            await c.execute("select set_config('app.actor',$1,true)", ACTOR)
            held = await c.fetchval(
                """select 1 from collected_posts where id=$1::uuid and state='normalised'
                     and ($2::uuid is null or analysis_claim_token=$2::uuid) for update""",
                evidence.post_id,
                claim_token,
            )
            if not held:
                return None
            payload = {**finding_payload(outcome.result, evidence), "formatted": outcome.formatted}
            key = hashlib.sha256(f"{vertical}:{evidence.post_id}".encode()).hexdigest()
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
                    {"prompt_version": PROMPT_VERSION, "model": model, "language": outcome.language}
                ),
            )
            return str(row["id"])

    async def finalize(self, post_id: str, claim_token: str | None, *, accepted: bool) -> bool:
        """One final state per post, after every vertical: analysed if any finding, else rejected."""
        async with self._pool().acquire() as c, c.transaction():
            await c.execute("select set_config('app.actor',$1,true)", ACTOR)
            status = await c.execute(
                """update collected_posts set state=$3, analysis_claim_token=null, analysis_claimed_at=null
                    where id=$1::uuid and state='normalised' and ($2::uuid is null or analysis_claim_token=$2::uuid)""",
                post_id,
                claim_token,
                "analysed" if accepted else "rejected",
            )
            return status.endswith(" 1")

    async def release(self, post_id: str, claim_token: str) -> None:
        """Give a post back after a transient error; the next cycle retries it."""
        await self._pool().execute(
            """update collected_posts set analysis_claim_token=null, analysis_claimed_at=null
                where id=$1::uuid and analysis_claim_token=$2::uuid and state='normalised'""",
            post_id,
            claim_token,
        )

    async def campaign_finding_ids(self, finding_ids: list[str]) -> set[str]:
        """The findings among ``finding_ids`` whose post came from a campaign's Facebook batch."""
        rows = await self._pool().fetch(
            """select f.id::text as id from findings f
                 join collected_posts p on p.id=f.post_id
                 join acquisition_runs r on r.id=p.acquisition_run_id
                 join acquisition_batch_items i on i.id=r.batch_item_id
                 join acquisition_batches b on b.id=i.batch_id
                where f.id = any($1::uuid[]) and b.campaign_id is not null""",
            finding_ids,
        )
        return {r["id"] for r in rows}

    async def save_digest(self, chat_id: int, vertical: str, finding_ids: list[str], body: str):
        key = hashlib.sha256(f"{chat_id}:{vertical}:{','.join(sorted(finding_ids))}".encode()).hexdigest()
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

    async def unsent_digests(self, chat_id: int, limit: int = 20) -> list[tuple[str, str]]:
        """Digests recorded but not yet delivered (Telegram was down): oldest first."""
        rows = await self._pool().fetch(
            """select id::text as id, body from analysis_digests
                where state='queued' and telegram_chat_id=$1 order by created_at limit $2""",
            chat_id,
            limit,
        )
        return [(r["id"], r["body"]) for r in rows]

    async def mark_digest_sent(self, digest_id: str, telegram_message_id: int) -> None:
        async with self._pool().acquire() as c, c.transaction():
            await c.execute("select set_config('app.actor',$1,true)", ACTOR)
            finding_ids = await c.fetchval(
                """update analysis_digests set state='sent', telegram_message_id=$2, sent_at=now(), updated_at=now()
                    where id=$1::uuid and state='queued' returning finding_ids::text""",
                digest_id,
                telegram_message_id,
            )
            if finding_ids:
                await c.execute(
                    """update findings set state='delivered'
                        where id = any(select jsonb_array_elements_text($1::jsonb)::uuid) and state='ready'""",
                    finding_ids,
                )
