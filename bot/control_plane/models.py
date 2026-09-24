"""Transport-neutral control-plane messages and replies."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    chat_id: int
    user_id: int | None
    message_id: int
    text: str | None = None
    voice_file_id: str | None = None
    voice_size: int | None = None
    voice_duration_seconds: int | None = None

    @property
    def kind(self) -> str:
        return "voice" if self.voice_file_id else "text"


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    text: str
    language: str | None
    confidence: float | None
    model: str
    provider: str = "openrouter"
    # Exactly what the provider billed, as returned; None when it did not say.
    cost_usd: float | None = None
    audio_seconds: float | None = None
    request_status: int | None = None


@dataclass(frozen=True, slots=True)
class TranscriptionFailure:
    """Audit record for a transcription that produced no usable transcript."""

    error_code: str
    model: str | None
    provider: str | None = None
    request_status: int | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    text: str


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    """A confirmed request for the durable Orchestra command consumer."""

    command: str
    arguments: str
    chat_id: int
    user_id: int
    message_id: int
    confirmation_id: str | None = None
