"""Durable deduplication, transcript and confirmation persistence."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from bot.control_plane.models import (
    IncomingMessage,
    LiveProfile,
    LiveSession,
    StatusSnapshot,
    TranscriptionFailure,
    TranscriptResult,
)


class ControlPlaneStore(Protocol):
    async def claim_message(self, message: IncomingMessage) -> bool: ...
    async def save_transcript(self, message: IncomingMessage, transcript: TranscriptResult) -> None: ...
    async def record_transcription_failure(self, message: IncomingMessage, failure: TranscriptionFailure) -> None: ...
    async def create_confirmation(self, message: IncomingMessage, command: str, arguments: str, *, ttl_seconds: int) -> str: ...
    async def consume_confirmation(self, message: IncomingMessage, token: str) -> tuple[str, str, str | None] | None: ...
    async def status_snapshot(self) -> StatusSnapshot: ...


class MemoryControlPlaneStore:
    """Test/local fallback. Production uses Postgres and survives restarts."""

    def __init__(self) -> None:
        self.messages: set[tuple[int, int]] = set()
        self.transcripts: dict[tuple[int, int], TranscriptResult] = {}
        self.failures: dict[tuple[int, int], TranscriptionFailure] = {}
        self.snapshot = StatusSnapshot()
        self.confirmations: dict[str, tuple[int, int, str, str, datetime]] = {}

    async def claim_message(self, message: IncomingMessage) -> bool:
        key = (message.chat_id, message.message_id)
        if key in self.messages:
            return False
        self.messages.add(key)
        return True

    async def save_transcript(self, message: IncomingMessage, transcript: TranscriptResult) -> None:
        self.transcripts[(message.chat_id, message.message_id)] = transcript

    async def record_transcription_failure(self, message: IncomingMessage, failure: TranscriptionFailure) -> None:
        self.failures[(message.chat_id, message.message_id)] = failure

    async def create_confirmation(self, message: IncomingMessage, command: str, arguments: str, *, ttl_seconds: int) -> str:
        token = uuid.uuid4().hex[:10]
        self.confirmations[token] = (message.chat_id, message.user_id or 0, command, arguments, datetime.now(UTC) + timedelta(seconds=ttl_seconds))
        return token

    async def consume_confirmation(self, message: IncomingMessage, token: str) -> tuple[str, str, str | None] | None:
        pending = self.confirmations.pop(token, None)
        if pending is None:
            return None
        chat_id, user_id, command, arguments, expires_at = pending
        if (chat_id, user_id) != (message.chat_id, message.user_id) or datetime.now(UTC) >= expires_at:
            return None
        return command, arguments, None

    async def status_snapshot(self) -> StatusSnapshot:
        return self.snapshot


class PostgresControlPlaneStore:
    """asyncpg implementation; inserts are the atomic idempotency boundary."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: object | None = None

    async def connect(self) -> None:
        import asyncpg
        self.pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()  # type: ignore[attr-defined]

    def _pool(self) -> object:
        if self.pool is None:
            raise RuntimeError("PostgresControlPlaneStore is not connected")
        return self.pool

    async def claim_message(self, message: IncomingMessage) -> bool:
        result = await self._pool().execute(  # type: ignore[attr-defined]
            """insert into public.telegram_inbound_messages
                 (telegram_chat_id, telegram_message_id, telegram_user_id, message_kind, text_body)
               values ($1, $2, $3, $4, $5)
               on conflict (telegram_chat_id, telegram_message_id) do nothing""",
            message.chat_id, message.message_id, message.user_id, message.kind, message.text,
        )
        return result == "INSERT 0 1"

    async def save_transcript(self, message: IncomingMessage, transcript: TranscriptResult) -> None:
        await self._pool().execute(  # type: ignore[attr-defined]
            """update public.telegram_inbound_messages
               set transcript=$3, detected_language=$4, transcription_confidence=$5,
                   transcription_model=$6, transcription_provider=$7, transcription_cost_usd=$8,
                   transcription_audio_seconds=$9, transcription_request_status=$10,
                   processing_state='processed', processed_at=now()
               where telegram_chat_id=$1 and telegram_message_id=$2""",
            message.chat_id, message.message_id, transcript.text, transcript.language,
            transcript.confidence, transcript.model, transcript.provider, transcript.cost_usd,
            transcript.audio_seconds, transcript.request_status,
        )

    async def record_transcription_failure(self, message: IncomingMessage, failure: TranscriptionFailure) -> None:
        await self._pool().execute(  # type: ignore[attr-defined]
            """update public.telegram_inbound_messages
               set transcription_model=$3, transcription_provider=$4, transcription_request_status=$5,
                   error_code=$6, processing_state='failed', processed_at=now()
               where telegram_chat_id=$1 and telegram_message_id=$2""",
            message.chat_id, message.message_id, failure.model, failure.provider,
            failure.request_status, failure.error_code,
        )

    async def create_confirmation(self, message: IncomingMessage, command: str, arguments: str, *, ttl_seconds: int) -> str:
        token = uuid.uuid4().hex[:10]
        await self._pool().execute(  # type: ignore[attr-defined]
            """insert into public.telegram_command_confirmations
               (token, telegram_chat_id, telegram_user_id, command, arguments, expires_at)
               values ($1, $2, $3, $4, $5, now() + ($6 * interval '1 second'))""",
            token, message.chat_id, message.user_id, command, arguments, ttl_seconds,
        )
        return token

    async def consume_confirmation(self, message: IncomingMessage, token: str) -> tuple[str, str, str | None] | None:
        row = await self._pool().fetchrow(  # type: ignore[attr-defined]
            """update public.telegram_command_confirmations set state='confirmed', confirmed_at=now()
               where token=$1 and telegram_chat_id=$2 and telegram_user_id=$3
                 and state='pending' and expires_at > now()
               returning id, command, arguments""",
            token, message.chat_id, message.user_id,
        )
        return (row["command"], row["arguments"], str(row["id"])) if row else None

    async def status_snapshot(self) -> StatusSnapshot:
        row = await self._pool().fetchrow(STATUS_SQL)  # type: ignore[attr-defined]
        return StatusSnapshot(**dict(row))


# One round trip of counts only. Every table here comes from migrations
# 003-006, which the control plane already requires.
STATUS_SQL = """
select
  (select count(*) from public.orchestration_commands where state = 'queued')::int as commands_queued,
  (select count(*) from public.orchestration_commands where state = 'running')::int as commands_running,
  (select count(*) from public.acquisition_batches where state in ('planned', 'queued', 'running'))::int as batches_active,
  (select count(*) from public.acquisition_batches where state = 'human_verification_required')::int as batches_need_verification,
  (select max(finished_at) from public.acquisition_batches) as last_batch_finished_at,
  (select count(*) from public.acquisition_runs where state = 'running')::int as runs_running,
  (select count(*) from public.acquisition_runs where state = 'awaiting_human_verification')::int as runs_need_verification,
  (select count(*) from public.monitoring_sources where deleted_at is null and state = 'active')::int as sources_active,
  (select count(*) from public.monitoring_sources where deleted_at is null and state = 'paused')::int as sources_paused,
  (select count(*) from public.monitoring_sources
     where deleted_at is null and state = 'human_verification_required')::int as sources_need_verification,
  (select count(*) from public.browser_profiles where deleted_at is null and state = 'ready')::int as profiles_ready,
  (select count(*) from public.browser_profiles where deleted_at is null and state = 'in_use')::int as profiles_in_use,
  (select count(*) from public.browser_profiles
     where deleted_at is null and state in ('human_verification_required', 'quarantined'))::int as profiles_need_attention,
  (select count(*) from public.verification_jobs where state in ('requested', 'active'))::int as verification_jobs_open,
  (select count(*) from public.collected_posts where collected_at > now() - interval '24 hours')::int as posts_last_24h,
  (select count(*) from public.telegram_inbound_messages
     where message_kind = 'voice' and transcription_cost_usd is not null
       and received_at > now() - interval '30 days')::int as voice_notes_30d,
  (select sum(transcription_cost_usd) from public.telegram_inbound_messages
     where received_at > now() - interval '30 days') as voice_cost_usd_30d
"""


# --- live browser sessions (migration 007) ------------------------------------

_LIVE_SELECT = """
select s.id::text as id, s.reason, s.state, s.expires_at, s.opened_by,
       p.id::text as profile_id, p.profile_name, p.platform, p.state as profile_state
  from public.live_view_sessions s
  join public.browser_profiles p on p.id = s.browser_profile_id
"""


def _live_session(row: object) -> LiveSession:
    r = row  # asyncpg.Record
    profile = LiveProfile(r["profile_id"], r["profile_name"], r["platform"], r["profile_state"])  # type: ignore[index]
    return LiveSession(r["id"], profile, r["reason"], r["state"], r["expires_at"], r["opened_by"])  # type: ignore[index]


class PostgresLiveViewStore:
    """Shares the control plane's pool; every write sets app.actor for the audit log."""

    def __init__(self, store: PostgresControlPlaneStore) -> None:
        self._store = store

    def _pool(self) -> object:
        return self._store._pool()

    async def ensure_profile(self, platform: str, name: str, actor: str) -> LiveProfile:
        async with self._pool().acquire() as conn, conn.transaction():  # type: ignore[attr-defined]
            await conn.execute("select set_config('app.actor', $1, true)", actor)
            await conn.execute(
                """insert into public.browser_profiles (profile_name, platform, storage_locator)
                   values ($1, $2, 'volume:browser_profiles') on conflict (profile_name) do nothing""",
                name, platform,
            )
            row = await conn.fetchrow(
                "select id::text, profile_name, platform, state from public.browser_profiles where profile_name=$1 and deleted_at is null",
                name,
            )
        if row is None:
            raise RuntimeError(f"profile {name} is deleted")
        return LiveProfile(row["id"], row["profile_name"], row["platform"], row["state"])

    async def request_live_view(self, profile: LiveProfile, reason: str, requested_by: str, ttl_seconds: int) -> tuple[LiveSession, bool]:
        pool = self._pool()
        created = await pool.fetchval(  # type: ignore[attr-defined]
            """insert into public.live_view_sessions (browser_profile_id, reason, requested_by, expires_at)
               values ($1::uuid, $2, $3, now() + ($4 * interval '1 second'))
               on conflict do nothing returning id""",
            profile.id, reason, requested_by, ttl_seconds,
        )
        row = await pool.fetchrow(  # type: ignore[attr-defined]
            _LIVE_SELECT + " where s.browser_profile_id = $1::uuid and s.state in ('requested', 'open')", profile.id
        )
        return _live_session(row), created is not None

    async def get_live_view(self, session_id: str) -> LiveSession | None:
        row = await self._pool().fetchrow(_LIVE_SELECT + " where s.id = $1::uuid", session_id)  # type: ignore[attr-defined]
        return _live_session(row) if row else None

    async def mark_live_view_open(self, session_id: str, user_id: int, open_seconds: int) -> LiveSession | None:
        updated = await self._pool().fetchval(  # type: ignore[attr-defined]
            """update public.live_view_sessions
                  set state='open', opened_by=$2, opened_at=now(), expires_at=now() + ($3 * interval '1 second')
                where id=$1::uuid and state='requested' and expires_at > now() returning id""",
            session_id, user_id, open_seconds,
        )
        return await self.get_live_view(session_id) if updated else None

    async def close_live_view(self, session_id: str, state: str, actor: str, error_code: str | None = None) -> LiveSession | None:
        updated = await self._pool().fetchval(  # type: ignore[attr-defined]
            """update public.live_view_sessions set state=$2, closed_by=$3, error_code=$4, closed_at=now()
                where id=$1::uuid and state in ('requested', 'open') returning id""",
            session_id, state, actor, error_code,
        )
        return await self.get_live_view(session_id) if updated else None

    async def complete_verification(self, profile: LiveProfile, actor: str) -> None:
        # Same effect as scripts/browser_login.sh confirming a session.
        async with self._pool().acquire() as conn, conn.transaction():  # type: ignore[attr-defined]
            await conn.execute("select set_config('app.actor', $1, true)", actor)
            await conn.execute(
                """update public.browser_profiles set state='ready', last_verified_at=now()
                    where id=$1::uuid and state in ('provisioned', 'human_verification_required', 'quarantined', 'ready')""",
                profile.id,
            )
            await conn.execute(
                """update public.verification_jobs j set state='active'
                     from public.monitoring_sources s
                    where s.id=j.source_id and s.platform=$1 and j.state='requested'""",
                profile.platform,
            )
            await conn.execute(
                """update public.verification_jobs j set state='verified', resolved_at=now(), resolved_by=$2
                     from public.monitoring_sources s
                    where s.id=j.source_id and s.platform=$1 and j.state='active'""",
                profile.platform, actor,
            )
            await conn.execute(
                "update public.monitoring_sources set state='active' where platform=$1 and state='human_verification_required'",
                profile.platform,
            )

    async def profiles_needing_human(self, cooldown_seconds: int) -> list[LiveProfile]:
        rows = await self._pool().fetch(  # type: ignore[attr-defined]
            """select p.id::text, p.profile_name, p.platform, p.state from public.browser_profiles p
                where p.deleted_at is null and p.state = 'human_verification_required'
                  and not exists (select 1 from public.live_view_sessions s where s.browser_profile_id = p.id
                                   and (s.state in ('requested', 'open')
                                        -- A request someone closed or let lapse is not repeated at once;
                                        -- a completed login never delays the next checkpoint.
                                        or (s.state in ('cancelled', 'expired')
                                            and s.closed_at > now() - ($1 * interval '1 second'))))
                  -- The Tailscale verification service owns announced jobs; one message per checkpoint.
                  and not exists (select 1 from public.verification_jobs vj
                                   where vj.browser_profile_id = p.id and vj.state in ('requested', 'active')
                                     and vj.notified_at is not null)
                order by p.profile_name""",
            cooldown_seconds,
        )
        return [LiveProfile(r["id"], r["profile_name"], r["platform"], r["state"]) for r in rows]

    async def expired_live_views(self) -> list[LiveSession]:
        rows = await self._pool().fetch(  # type: ignore[attr-defined]
            _LIVE_SELECT + " where s.state in ('requested', 'open') and s.expires_at <= now()"
        )
        return [_live_session(r) for r in rows]


class MemoryLiveViewStore:
    """Test double with the same rules as the Postgres store."""

    def __init__(self) -> None:
        self.profiles: dict[str, LiveProfile] = {}
        self.sessions: dict[str, LiveSession] = {}
        self.created_at: dict[str, datetime] = {}
        self.closed_at: dict[str, datetime] = {}
        self.completed: list[str] = []

    async def ensure_profile(self, platform: str, name: str, actor: str) -> LiveProfile:
        existing = next((p for p in self.profiles.values() if p.name == name), None)
        if existing is None:
            existing = LiveProfile(str(uuid.uuid4()), name, platform, "provisioned")
            self.profiles[existing.id] = existing
        return existing

    async def request_live_view(self, profile: LiveProfile, reason: str, requested_by: str, ttl_seconds: int) -> tuple[LiveSession, bool]:
        active = next((s for s in self.sessions.values() if s.profile.id == profile.id and s.state in {"requested", "open"}), None)
        if active:
            return active, False
        session = LiveSession(str(uuid.uuid4()), profile, reason, "requested", datetime.now(UTC) + timedelta(seconds=ttl_seconds))
        self.sessions[session.id] = session
        self.created_at[session.id] = datetime.now(UTC)
        return session, True

    async def get_live_view(self, session_id: str) -> LiveSession | None:
        return self.sessions.get(session_id)

    async def mark_live_view_open(self, session_id: str, user_id: int, open_seconds: int) -> LiveSession | None:
        session = self.sessions.get(session_id)
        if session is None or session.state != "requested" or session.expires_at <= datetime.now(UTC):
            return None
        self.sessions[session_id] = replace(session, state="open", opened_by=user_id, expires_at=datetime.now(UTC) + timedelta(seconds=open_seconds))
        return self.sessions[session_id]

    async def close_live_view(self, session_id: str, state: str, actor: str, error_code: str | None = None) -> LiveSession | None:
        session = self.sessions.get(session_id)
        if session is None or session.state not in {"requested", "open"}:
            return None
        self.sessions[session_id] = replace(session, state=state)
        self.closed_at[session_id] = datetime.now(UTC)
        return self.sessions[session_id]

    async def complete_verification(self, profile: LiveProfile, actor: str) -> None:
        self.profiles[profile.id] = replace(self.profiles.get(profile.id, profile), state="ready")
        self.completed.append(profile.id)

    async def profiles_needing_human(self, cooldown_seconds: int) -> list[LiveProfile]:
        cutoff = datetime.now(UTC) - timedelta(seconds=cooldown_seconds)
        busy = {
            s.profile.id for s in self.sessions.values()
            if s.state in {"requested", "open"}
            or (s.state in {"cancelled", "expired"} and self.closed_at.get(s.id, cutoff) > cutoff)
        }
        return [p for p in self.profiles.values() if p.state == "human_verification_required" and p.id not in busy]

    async def expired_live_views(self) -> list[LiveSession]:
        now = datetime.now(UTC)
        return [s for s in self.sessions.values() if s.state in {"requested", "open"} and s.expires_at <= now]
