"""Durable deduplication, transcript and confirmation persistence."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol

from bot.control_plane.models import IncomingMessage, TranscriptionFailure, TranscriptResult


class ControlPlaneStore(Protocol):
    async def claim_message(self, message: IncomingMessage) -> bool: ...
    async def save_transcript(self, message: IncomingMessage, transcript: TranscriptResult) -> None: ...
    async def record_transcription_failure(self, message: IncomingMessage, failure: TranscriptionFailure) -> None: ...
    async def create_confirmation(self, message: IncomingMessage, command: str, arguments: str, *, ttl_seconds: int) -> str: ...
    async def consume_confirmation(self, message: IncomingMessage, token: str) -> tuple[str, str, str | None] | None: ...


class MemoryControlPlaneStore:
    """Test/local fallback. Production uses Postgres and survives restarts."""

    def __init__(self) -> None:
        self.messages: set[tuple[int, int]] = set()
        self.transcripts: dict[tuple[int, int], TranscriptResult] = {}
        self.failures: dict[tuple[int, int], TranscriptionFailure] = {}
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
