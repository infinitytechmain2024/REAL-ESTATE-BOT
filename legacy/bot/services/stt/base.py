"""Speech-to-text interface.

Mirrors the LLM layer deliberately: one abstract class, a name-keyed registry
and a manager with fallback, so swapping Whisper for something else is an
``.env`` change.
"""

from __future__ import annotations

import abc
from typing import ClassVar

from pydantic import BaseModel, Field


class AudioFile(BaseModel):
    """A voice message downloaded from Telegram."""

    data: bytes
    filename: str = Field(default="voice.ogg", description="Extension matters to some APIs")
    mime_type: str = "audio/ogg"
    duration_seconds: int | None = None

    @property
    def size_mb(self) -> float:
        return len(self.data) / 1_048_576


class Transcript(BaseModel):
    """The recognised text."""

    text: str
    language: str | None = None
    provider: str
    model: str
    duration_seconds: float | None = None

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


class STTProvider(abc.ABC):
    """Base class for transcription backends."""

    name: ClassVar[str]

    @abc.abstractmethod
    async def transcribe(
        self,
        audio: AudioFile,
        *,
        model: str,
        language: str | None = None,
        timeout: float | None = None,
    ) -> Transcript:
        """Transcribe *audio*.

        Must raise :class:`bot.exceptions.STTError` on any failure so the
        manager can fall back.
        """

    @abc.abstractmethod
    async def aclose(self) -> None:
        """Release the underlying HTTP client."""
