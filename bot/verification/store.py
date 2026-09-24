"""Persistence for the verification flow (migrations 003 and 009).

Every state change uses the transition graph of migration 003, so the
database's own guard and audit triggers apply; every write sets ``app.actor``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .models import OPEN_STATES, AccessToken, Event, Job, Launch, PageSession


class VerificationStore(Protocol):
    async def unannounced_jobs(self) -> list[Job]: ...
    async def jobs_needing_reminder(self, renotify_seconds: int) -> list[Job]: ...
    async def mark_announced(self, job_id: str, profile_id: str | None, kind: str, sensitive: bool, ttl_seconds: int) -> Job: ...
    async def get_job(self, job_id: str) -> Job | None: ...
    async def issue_token(self, job_id: str, user_id: int, profile_id: str, token_sha256: str, ttl_seconds: int) -> None: ...
    async def consume_token(self, token_sha256: str, user_id: int, identity: str) -> AccessToken | None: ...
    async def create_session(self, token: AccessToken, identity: str, cookie_sha256: str, csrf: str, ttl_seconds: int) -> PageSession: ...
    async def get_session(self, cookie_sha256: str) -> PageSession | None: ...
    async def claim(self, job_id: str, user_id: int, actor: str) -> bool: ...
    async def mark_solved(self, job_id: str, actor: str) -> None: ...
    async def confirm_recovery(self, job_id: str, actor: str) -> bool: ...
    async def resume(self, job_id: str, actor: str, notify_user_id: int | None = None) -> str | None: ...
    async def launch_updates(self, stale_seconds: int) -> list[Launch]: ...
    async def mark_launch_notified(self, launch_id: str, state: str) -> None: ...
    async def cancel(self, job_id: str, actor: str) -> bool: ...
    async def fail(self, job_id: str, actor: str) -> bool: ...
    async def sensitive_stop(self, job_id: str, kind: str, actor: str) -> None: ...
    async def expire_due(self, actor: str) -> list[Job]: ...
    async def add_event(self, job_id: str, event: str, actor: str, detail: dict[str, Any] | None = None) -> None: ...
    async def events(self, job_id: str) -> list[Event]: ...
    async def approved_roles(self) -> dict[int, str]: ...


_JOB_SELECT = """
select j.id::text as id, j.state, j.job_type, j.source_id::text as source_id, s.canonical_url, s.platform,
       j.resolution_note, coalesce(j.browser_profile_id, r.browser_profile_id)::text as profile_id,
       p.profile_name, p.state as profile_state, i.batch_id::text as batch_id, j.challenge_kind, j.sensitive,
       j.claimed_by, j.notified_at, j.solved_at, j.recovered_at, j.resumed_at, j.expires_at
  from public.verification_jobs j
  join public.monitoring_sources s on s.id = j.source_id
  left join public.acquisition_runs r on r.id = j.acquisition_run_id
  left join public.browser_profiles p on p.id = coalesce(j.browser_profile_id, r.browser_profile_id)
  left join public.acquisition_batch_items i on i.id = r.batch_item_id
"""


def _job(row: Any) -> Job:
    return Job(
        id=row["id"], state=row["state"], job_type=row["job_type"], source_id=row["source_id"],
        source_url=row["canonical_url"], platform=row["platform"], resolution_note=row["resolution_note"],
        profile_id=row["profile_id"], profile_name=row["profile_name"], profile_state=row["profile_state"],
        batch_id=row["batch_id"], challenge_kind=row["challenge_kind"], sensitive=row["sensitive"],
        claimed_by=row["claimed_by"], notified_at=row["notified_at"], solved_at=row["solved_at"],
        recovered_at=row["recovered_at"], resumed_at=row["resumed_at"], expires_at=row["expires_at"],
    )


class PostgresVerificationStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: Any = None

    async def connect(self) -> None:
        import asyncpg

        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=4)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    def _pool(self) -> Any:
        if self.pool is None:
            raise RuntimeError("PostgresVerificationStore is not connected")
        return self.pool

    async def unannounced_jobs(self) -> list[Job]:
        rows = await self._pool().fetch(_JOB_SELECT + " where j.state in ('requested','active') and j.notified_at is null order by j.requested_at")
        return [_job(r) for r in rows]

    async def jobs_needing_reminder(self, renotify_seconds: int) -> list[Job]:
        rows = await self._pool().fetch(
            _JOB_SELECT + """
             where j.state in ('requested','active') and not j.sensitive and j.notified_at is not null
               and not exists (select 1 from public.verification_page_sessions ps
                                where ps.verification_job_id = j.id and ps.revoked_at is null and ps.expires_at > now())
               and coalesce((select max(t.created_at) from public.verification_access_tokens t
                              where t.verification_job_id = j.id), j.notified_at) < now() - ($1 * interval '1 second')
             order by j.requested_at""",
            renotify_seconds,
        )
        return [_job(r) for r in rows]

    async def mark_announced(self, job_id: str, profile_id: str | None, kind: str, sensitive: bool, ttl_seconds: int) -> Job:
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', 'verification', true)")
            await conn.execute(
                """update public.verification_jobs
                      set browser_profile_id = coalesce(browser_profile_id, $2::uuid), challenge_kind = $3,
                          sensitive = sensitive or $4, notified_at = now(),
                          expires_at = coalesce(expires_at, now() + ($5 * interval '1 second'))
                    where id = $1::uuid""",
                job_id, profile_id, kind, sensitive, ttl_seconds,
            )
        job = await self.get_job(job_id)
        assert job is not None
        return job

    async def get_job(self, job_id: str) -> Job | None:
        row = await self._pool().fetchrow(_JOB_SELECT + " where j.id = $1::uuid", job_id)
        return _job(row) if row else None

    async def issue_token(self, job_id: str, user_id: int, profile_id: str, token_sha256: str, ttl_seconds: int) -> None:
        await self._pool().execute(
            """insert into public.verification_access_tokens
                 (verification_job_id, telegram_user_id, browser_profile_id, token_sha256, expires_at)
               values ($1::uuid, $2, $3::uuid, $4, now() + ($5 * interval '1 second'))""",
            job_id, user_id, profile_id, token_sha256, ttl_seconds,
        )

    async def consume_token(self, token_sha256: str, user_id: int, identity: str) -> AccessToken | None:
        # One atomic statement makes a token single-use; the user condition means
        # nobody but its recipient can use it -- or burn it.
        row = await self._pool().fetchrow(
            """update public.verification_access_tokens set used_at = now(), used_by_identity = $3
                where token_sha256 = $1 and telegram_user_id = $2
                  and used_at is null and revoked_at is null and expires_at > now()
            returning id::text, verification_job_id::text, telegram_user_id, browser_profile_id::text""",
            token_sha256, user_id, identity,
        )
        return AccessToken(row[0], row[1], row[2], row[3]) if row else None

    async def create_session(self, token: AccessToken, identity: str, cookie_sha256: str, csrf: str, ttl_seconds: int) -> PageSession:
        session_id = await self._pool().fetchval(
            """insert into public.verification_page_sessions
                 (access_token_id, verification_job_id, telegram_user_id, browser_profile_id, identity,
                  cookie_sha256, csrf_token, expires_at)
               values ($1::uuid, $2::uuid, $3, $4::uuid, $5, $6, $7, now() + ($8 * interval '1 second'))
            returning id::text""",
            token.id, token.job_id, token.user_id, token.profile_id, identity, cookie_sha256, csrf, ttl_seconds,
        )
        return PageSession(session_id, token.job_id, token.user_id, token.profile_id, identity, csrf)

    async def get_session(self, cookie_sha256: str) -> PageSession | None:
        row = await self._pool().fetchrow(
            """select id::text, verification_job_id::text, telegram_user_id, browser_profile_id::text, identity, csrf_token
                 from public.verification_page_sessions
                where cookie_sha256 = $1 and revoked_at is null and expires_at > now()""",
            cookie_sha256,
        )
        return PageSession(*row) if row else None

    async def claim(self, job_id: str, user_id: int, actor: str) -> bool:
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", actor)
            claimed = await conn.fetchval(
                """update public.verification_jobs set state = 'active', claimed_by = $2, claimed_at = now()
                    where id = $1::uuid and state = 'requested' and not sensitive returning id""",
                job_id, user_id,
            )
            if claimed:
                return True
            return bool(await conn.fetchval(
                "select 1 from public.verification_jobs where id = $1::uuid and state = 'active' and claimed_by = $2",
                job_id, user_id,
            ))

    async def mark_solved(self, job_id: str, actor: str) -> None:
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", actor)
            await conn.execute("update public.verification_jobs set solved_at = now() where id = $1::uuid and state = 'active'", job_id)

    async def confirm_recovery(self, job_id: str, actor: str) -> bool:
        job = await self.get_job(job_id)
        if job is None or job.state != "active":
            return False
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", actor)
            done = await conn.fetchval(
                """update public.verification_jobs
                      set state = 'verified', recovered_at = now(), resolved_at = now(), resolved_by = $2
                    where id = $1::uuid and state = 'active' returning id""",
                job_id, actor,
            )
            if not done:
                return False
            if job.profile_id:
                await conn.execute(
                    "update public.browser_profiles set state = 'ready', last_verified_at = now() where id = $1::uuid and state = 'human_verification_required'",
                    job.profile_id,
                )
            await conn.execute(
                "update public.monitoring_sources set state = 'active' where id = $1::uuid and state = 'human_verification_required'",
                job.source_id,
            )
            return True

    async def resume(self, job_id: str, actor: str, notify_user_id: int | None = None) -> str | None:
        job = await self.get_job(job_id)
        if job is None or job.state != "verified" or job.resumed_at is not None:
            raise ValueError("only a verified, not yet resumed job can resume its run")
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", actor)
            if job.batch_id:
                b = job.batch_id
                await conn.execute(
                    """update public.acquisition_runs set state = 'stopped', finished_at = now(), stop_reason = 'verification_resumed'
                        where state = 'awaiting_human_verification'
                          and batch_item_id in (select id from public.acquisition_batch_items where batch_id = $1::uuid)""", b)
                await conn.execute(
                    "update public.acquisition_batch_items set state = 'queued' where batch_id = $1::uuid and state = 'awaiting_human_verification'", b)
                await conn.execute(
                    """update public.batch_runs set state = 'stopped', finished_at = now(), stop_reason = 'verification_resumed'
                        where batch_id = $1::uuid and state = 'human_verification_required'""", b)
                await conn.execute(
                    """update public.monitoring_sources set state = 'active' where state = 'human_verification_required'
                          and id in (select source_id from public.acquisition_batch_items where batch_id = $1::uuid)""", b)
                requeued = await conn.execute(
                    "update public.acquisition_batches set state = 'queued' where id = $1::uuid and state = 'human_verification_required'", b)
                # facebook-runner starts it; the same transaction, so a queued
                # batch never waits on a request that was not written.
                if requeued.endswith(" 1"):
                    await conn.execute(
                        """insert into public.collector_launch_requests(batch_id, verification_job_id, requested_by, notify_telegram_id)
                           values ($1::uuid, $2::uuid, $3, $4) on conflict do nothing""", b, job_id, actor, notify_user_id)
            await conn.execute("update public.verification_jobs set resumed_at = now() where id = $1::uuid", job_id)
        return job.batch_id

    async def launch_updates(self, stale_seconds: int) -> list[Launch]:
        rows = await self._pool().fetch(
            """select id::text as id, batch_id::text as batch_id, state, notify_telegram_id, result, error,
                      verification_job_id::text as job_id,
                      state = 'pending' and requested_at < now() - make_interval(secs => $1) as stale
                 from public.collector_launch_requests
                where (state <> 'pending' and state is distinct from notified_state)
                   or (state = 'pending' and notified_state is null and requested_at < now() - make_interval(secs => $1))
                order by requested_at""", stale_seconds)
        return [Launch(r["id"], r["batch_id"], r["state"], r["notify_telegram_id"], r["result"], r["error"], r["job_id"], r["stale"])
                for r in rows]

    async def mark_launch_notified(self, launch_id: str, state: str) -> None:
        await self._pool().execute(
            "update public.collector_launch_requests set notified_state = $2 where id = $1::uuid", launch_id, state)

    async def cancel(self, job_id: str, actor: str) -> bool:
        job = await self.get_job(job_id)
        if job is None or not job.is_open:
            return False
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", actor)
            await conn.execute(
                "update public.verification_jobs set state = 'cancelled', resolved_at = now(), resolved_by = $2 where id = $1::uuid and state in ('requested','active')",
                job_id, actor)
            if job.batch_id:
                await _close_batch(conn, job.batch_id, item_state="cancelled", run_state="cancelled", batch_run_state="cancelled", batch_state="cancelled")
            await _revoke(conn, job_id)
        return True

    async def fail(self, job_id: str, actor: str) -> bool:
        job = await self.get_job(job_id)
        if job is None or not job.is_open:
            return False
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", actor)
            # requested -> active -> rejected: both legal steps of the 003 graph.
            await conn.execute("update public.verification_jobs set state = 'active' where id = $1::uuid and state = 'requested'", job_id)
            await conn.execute(
                "update public.verification_jobs set state = 'rejected', resolved_at = now(), resolved_by = $2 where id = $1::uuid and state = 'active'",
                job_id, actor)
            if job.batch_id:
                await _close_batch(conn, job.batch_id, item_state="failed", run_state="stopped", batch_run_state="stopped", batch_state="failed")
            if job.profile_id:
                await conn.execute(
                    "update public.browser_profiles set state = 'quarantined' where id = $1::uuid and state = 'human_verification_required'",
                    job.profile_id)
            await _revoke(conn, job_id)
        return True

    async def sensitive_stop(self, job_id: str, kind: str, actor: str) -> None:
        job = await self.get_job(job_id)
        async with self._pool().acquire() as conn, conn.transaction():
            await conn.execute("select set_config('app.actor', $1, true)", actor)
            await conn.execute("update public.verification_jobs set sensitive = true, challenge_kind = $2 where id = $1::uuid", job_id, kind)
            if job and job.profile_id:
                await conn.execute(
                    "update public.browser_profiles set state = 'quarantined' where id = $1::uuid and state in ('human_verification_required', 'ready')",
                    job.profile_id)
            await _revoke(conn, job_id)

    async def expire_due(self, actor: str) -> list[Job]:
        rows = await self._pool().fetch(_JOB_SELECT + " where j.state in ('requested','active') and j.expires_at <= now()")
        expired = []
        for row in rows:
            async with self._pool().acquire() as conn, conn.transaction():
                await conn.execute("select set_config('app.actor', $1, true)", actor)
                done = await conn.fetchval(
                    "update public.verification_jobs set state = 'expired', resolved_at = now(), resolved_by = $2 where id = $1::uuid and state in ('requested','active') returning id",
                    row["id"], actor)
                await _revoke(conn, row["id"])
            if done:
                expired.append(_job(row))
        return expired

    async def add_event(self, job_id: str, event: str, actor: str, detail: dict[str, Any] | None = None) -> None:
        await self._pool().execute(
            "insert into public.verification_events (verification_job_id, event, actor, detail) values ($1::uuid, $2, $3, $4::jsonb)",
            job_id, event, actor, json.dumps(detail or {}),
        )

    async def approved_roles(self) -> dict[int, str]:
        """People an owner approved in Telegram (migration 011); owners come from .env."""
        rows = await self._pool().fetch("select telegram_user_id, role from public.telegram_operators where state = 'approved'")
        return {r[0]: r[1] for r in rows}

    async def events(self, job_id: str) -> list[Event]:
        rows = await self._pool().fetch(
            "select event, actor, detail, occurred_at from public.verification_events where verification_job_id = $1::uuid order by id",
            job_id,
        )
        return [Event(r["event"], r["actor"], json.loads(r["detail"]) if isinstance(r["detail"], str) else dict(r["detail"]), r["occurred_at"]) for r in rows]


async def _close_batch(conn: Any, batch_id: str, *, item_state: str, run_state: str, batch_run_state: str, batch_state: str) -> None:
    await conn.execute(
        f"""update public.acquisition_runs set state = '{run_state}', finished_at = now(), stop_reason = 'verification_closed'
             where state = 'awaiting_human_verification'
               and batch_item_id in (select id from public.acquisition_batch_items where batch_id = $1::uuid)""", batch_id)
    await conn.execute(
        f"update public.acquisition_batch_items set state = '{item_state}' where batch_id = $1::uuid and state = 'awaiting_human_verification'",
        batch_id)
    await conn.execute(
        f"update public.batch_runs set state = '{batch_run_state}', finished_at = now() where batch_id = $1::uuid and state = 'human_verification_required'",
        batch_id)
    await conn.execute(
        f"update public.acquisition_batches set state = '{batch_state}', finished_at = now() where id = $1::uuid and state = 'human_verification_required'",
        batch_id)


async def _revoke(conn: Any, job_id: str) -> None:
    await conn.execute(
        "update public.verification_access_tokens set revoked_at = now() where verification_job_id = $1::uuid and used_at is null and revoked_at is null",
        job_id)
    await conn.execute(
        "update public.verification_page_sessions set revoked_at = now() where verification_job_id = $1::uuid and revoked_at is null",
        job_id)


# --- in-memory double with the same rules ----------------------------------------


@dataclass
class _Token:
    record: AccessToken
    sha: str
    expires_at: datetime
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    used_at: datetime | None = None
    revoked: bool = False


@dataclass
class _Session:
    record: PageSession
    expires_at: datetime
    revoked: bool = False


@dataclass
class _Launch:
    record: Launch
    requested_at: datetime
    notified_state: str | None = None


class MemoryVerificationStore:
    """Test double. ``world`` holds the states the Postgres store would change."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.tokens: dict[str, _Token] = {}
        self.sessions: dict[str, _Session] = {}
        self.log: dict[str, list[Event]] = {}
        self.world: dict[str, str] = {}
        self.roles: dict[int, str] = {}
        self.launches: dict[str, _Launch] = {}

    async def approved_roles(self) -> dict[int, str]:
        return dict(self.roles)

    def add_job(self, job: Job) -> Job:
        self.jobs[job.id] = job
        if job.profile_id:
            self.world.setdefault(f"profile:{job.profile_id}", "human_verification_required")
        if job.batch_id:
            self.world.setdefault(f"batch:{job.batch_id}", "human_verification_required")
            self.world.setdefault(f"item:{job.batch_id}", "awaiting_human_verification")
        self.world.setdefault(f"source:{job.source_id}", "human_verification_required")
        return job

    async def unannounced_jobs(self) -> list[Job]:
        return [j for j in self.jobs.values() if j.is_open and j.notified_at is None]

    async def jobs_needing_reminder(self, renotify_seconds: int) -> list[Job]:
        now = datetime.now(UTC)
        out = []
        for job in self.jobs.values():
            if not job.is_open or job.sensitive or job.notified_at is None:
                continue
            if any(s.record.job_id == job.id and not s.revoked and s.expires_at > now for s in self.sessions.values()):
                continue
            last = max([t.created_at for t in self.tokens.values() if t.record.job_id == job.id] or [job.notified_at])
            if last < now - timedelta(seconds=renotify_seconds):
                out.append(job)
        return out

    async def mark_announced(self, job_id: str, profile_id: str | None, kind: str, sensitive: bool, ttl_seconds: int) -> Job:
        job = self.jobs[job_id]
        self.jobs[job_id] = replace(
            job, profile_id=job.profile_id or profile_id, challenge_kind=kind, sensitive=job.sensitive or sensitive,
            notified_at=datetime.now(UTC), expires_at=job.expires_at or datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )
        return self.jobs[job_id]

    async def get_job(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    async def issue_token(self, job_id: str, user_id: int, profile_id: str, token_sha256: str, ttl_seconds: int) -> None:
        self.tokens[token_sha256] = _Token(AccessToken(str(uuid.uuid4()), job_id, user_id, profile_id), token_sha256, datetime.now(UTC) + timedelta(seconds=ttl_seconds))

    async def consume_token(self, token_sha256: str, user_id: int, identity: str) -> AccessToken | None:
        token = self.tokens.get(token_sha256)
        if token is None or token.record.user_id != user_id or token.used_at or token.revoked or token.expires_at <= datetime.now(UTC):
            return None
        token.used_at = datetime.now(UTC)
        return token.record

    async def create_session(self, token: AccessToken, identity: str, cookie_sha256: str, csrf: str, ttl_seconds: int) -> PageSession:
        record = PageSession(str(uuid.uuid4()), token.job_id, token.user_id, token.profile_id, identity, csrf)
        self.sessions[cookie_sha256] = _Session(record, datetime.now(UTC) + timedelta(seconds=ttl_seconds))
        return record

    async def get_session(self, cookie_sha256: str) -> PageSession | None:
        session = self.sessions.get(cookie_sha256)
        if session is None or session.revoked or session.expires_at <= datetime.now(UTC):
            return None
        return session.record

    async def claim(self, job_id: str, user_id: int, actor: str) -> bool:
        job = self.jobs[job_id]
        if job.state == "requested" and not job.sensitive:
            self.jobs[job_id] = replace(job, state="active", claimed_by=user_id)
            return True
        return job.state == "active" and job.claimed_by == user_id

    async def mark_solved(self, job_id: str, actor: str) -> None:
        if self.jobs[job_id].state == "active":
            self.jobs[job_id] = replace(self.jobs[job_id], solved_at=datetime.now(UTC))

    async def confirm_recovery(self, job_id: str, actor: str) -> bool:
        job = self.jobs[job_id]
        if job.state != "active":
            return False
        self.jobs[job_id] = replace(job, state="verified", recovered_at=datetime.now(UTC))
        if job.profile_id:
            self.world[f"profile:{job.profile_id}"] = "ready"
        self.world[f"source:{job.source_id}"] = "active"
        return True

    async def resume(self, job_id: str, actor: str, notify_user_id: int | None = None) -> str | None:
        job = self.jobs[job_id]
        if job.state != "verified" or job.resumed_at is not None:
            raise ValueError("only a verified, not yet resumed job can resume its run")
        if job.batch_id:
            self.world[f"batch:{job.batch_id}"] = "queued"
            self.world[f"item:{job.batch_id}"] = "queued"
            launch_id = str(uuid.uuid4())
            self.launches[launch_id] = _Launch(Launch(launch_id, job.batch_id, "pending", notify_user_id, job_id=job_id), datetime.now(UTC))
        self.jobs[job_id] = replace(job, resumed_at=datetime.now(UTC))
        return job.batch_id

    async def launch_updates(self, stale_seconds: int) -> list[Launch]:
        cutoff = datetime.now(UTC) - timedelta(seconds=stale_seconds)
        out = []
        for item in self.launches.values():
            launch = item.record
            if launch.state != "pending" and launch.state != item.notified_state:
                out.append(launch)
            elif launch.state == "pending" and item.notified_state is None and item.requested_at < cutoff:
                out.append(replace(launch, stale=True))
        return out

    async def mark_launch_notified(self, launch_id: str, state: str) -> None:
        self.launches[launch_id].notified_state = state

    def set_launch(self, launch_id: str, state: str, result: str | None = None, error: str | None = None) -> None:
        """Test helper: what facebook-runner would write."""
        item = self.launches[launch_id]
        item.record = replace(item.record, state=state, result=result, error=error)

    def _revoke(self, job_id: str) -> None:
        for token in self.tokens.values():
            if token.record.job_id == job_id and not token.used_at:
                token.revoked = True
        for session in self.sessions.values():
            if session.record.job_id == job_id:
                session.revoked = True

    async def cancel(self, job_id: str, actor: str) -> bool:
        job = self.jobs[job_id]
        if not job.is_open:
            return False
        self.jobs[job_id] = replace(job, state="cancelled")
        if job.batch_id:
            self.world[f"batch:{job.batch_id}"] = "cancelled"
            self.world[f"item:{job.batch_id}"] = "cancelled"
        self._revoke(job_id)
        return True

    async def fail(self, job_id: str, actor: str) -> bool:
        job = self.jobs[job_id]
        if not job.is_open:
            return False
        self.jobs[job_id] = replace(job, state="rejected")
        if job.batch_id:
            self.world[f"batch:{job.batch_id}"] = "failed"
            self.world[f"item:{job.batch_id}"] = "failed"
        if job.profile_id:
            self.world[f"profile:{job.profile_id}"] = "quarantined"
        self._revoke(job_id)
        return True

    async def sensitive_stop(self, job_id: str, kind: str, actor: str) -> None:
        job = self.jobs[job_id]
        self.jobs[job_id] = replace(job, sensitive=True, challenge_kind=kind)
        if job.profile_id:
            self.world[f"profile:{job.profile_id}"] = "quarantined"
        self._revoke(job_id)

    async def expire_due(self, actor: str) -> list[Job]:
        now, out = datetime.now(UTC), []
        for job in list(self.jobs.values()):
            if job.state in OPEN_STATES and job.expires_at and job.expires_at <= now:
                self.jobs[job.id] = replace(job, state="expired")
                self._revoke(job.id)
                out.append(job)
        return out

    async def add_event(self, job_id: str, event: str, actor: str, detail: dict[str, Any] | None = None) -> None:
        self.log.setdefault(job_id, []).append(Event(event, actor, dict(detail or {}), datetime.now(UTC)))

    async def events(self, job_id: str) -> list[Event]:
        return list(self.log.get(job_id, []))
