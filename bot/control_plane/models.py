"""Transport-neutral control-plane messages and replies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    chat_id: int
    user_id: int | None
    message_id: int
    text: str | None = None
    voice_file_id: str | None = None
    voice_size: int | None = None
    voice_duration_seconds: int | None = None
    # The cleaned transcript of a voice note, set by the control plane before the text is handled as a task.
    transcript: str | None = None

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
class StatusSnapshot:
    """Counts behind /status; read in one query, no row content."""

    commands_queued: int = 0
    commands_running: int = 0
    batches_active: int = 0
    batches_need_verification: int = 0
    last_batch_finished_at: datetime | None = None
    runs_running: int = 0
    runs_need_verification: int = 0
    sources_active: int = 0
    sources_paused: int = 0
    sources_need_verification: int = 0
    profiles_ready: int = 0
    profiles_in_use: int = 0
    profiles_need_attention: int = 0
    verification_jobs_open: int = 0
    posts_last_24h: int = 0
    voice_notes_30d: int = 0
    voice_cost_usd_30d: Decimal | None = None


@dataclass(frozen=True, slots=True)
class Button:
    """An inline button: a Telegram Mini App (web_app_url) or a callback."""

    text: str
    web_app_url: str | None = None
    callback_data: str | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    """``buttons``: inline buttons under the message. ``keyboard``: rows of the reply keyboard shown at the
    bottom of the chat instead of the letter keyboard (None: leave it as it is; ``()``: remove it)."""

    text: str
    buttons: tuple[Button, ...] = ()
    keyboard: tuple[tuple[str, ...], ...] | None = None


@dataclass(frozen=True, slots=True)
class LiveProfile:
    id: str
    name: str
    platform: str
    state: str


@dataclass(frozen=True, slots=True)
class LiveSession:
    id: str
    profile: LiveProfile
    reason: str
    state: str
    expires_at: datetime
    opened_by: int | None = None


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    """A confirmed request for the durable Orchestra command consumer."""

    command: str
    arguments: str
    chat_id: int
    user_id: int
    message_id: int
    confirmation_id: str | None = None
    # Queued by auto mode without confirmation; audited as telegram:<id>:auto.
    auto: bool = False
