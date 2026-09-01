"""Transcription over the OpenAI ``/audio/transcriptions`` endpoint.

The same request shape is served by OpenAI (``whisper-1``,
``gpt-4o-transcribe``), Groq (``whisper-large-v3-turbo``, considerably faster
and cheaper), Deepinfra, and any self-hosted ``faster-whisper-server``. Only
the base URL and the key differ, which is what the subclasses at the bottom
set.
"""

from __future__ import annotations

import os
from typing import ClassVar

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
    RateLimitError,
)

from bot.config import STTSettings
from bot.exceptions import ConfigurationError, STTError
from bot.logging_conf import get_logger
from bot.services.stt.base import AudioFile, STTProvider, Transcript
from bot.services.stt.registry import register_stt

log = get_logger(__name__)


@register_stt("openai_whisper", "openai")
class OpenAIWhisperProvider(STTProvider):
    """OpenAI-compatible audio transcription."""

    name: ClassVar[str] = "openai_whisper"

    default_base_url: ClassVar[str | None] = None
    api_key_env_vars: ClassVar[tuple[str, ...]] = ("OPENAI_API_KEY",)

    def __init__(self, settings: STTSettings) -> None:
        self.settings = settings
        base_url = settings.base_url or self.default_base_url
        self._client = AsyncOpenAI(
            api_key=self._resolve_api_key(settings),
            base_url=base_url,
            timeout=httpx.Timeout(settings.timeout_seconds),
            max_retries=0,  # STTManager owns retry/fallback
        )
        log.debug("stt.provider.init", provider=self.name, base_url=base_url or "openai-default")

    def _resolve_api_key(self, settings: STTSettings) -> str:
        if settings.api_key:
            return settings.api_key.get_secret_value()
        for var in self.api_key_env_vars:
            value = os.getenv(var)
            if value:
                return value
        raise ConfigurationError(
            f"no API key for STT provider {self.name!r}: set STT_API_KEY or one of "
            f"{', '.join(self.api_key_env_vars)}"
        )

    async def transcribe(
        self,
        audio: AudioFile,
        *,
        model: str,
        language: str | None = None,
        timeout: float | None = None,
    ) -> Transcript:
        if not audio.data:
            raise STTError(f"{self.name}: empty audio payload")
        if audio.size_mb > self.settings.max_audio_mb:
            raise STTError(
                f"{self.name}: audio is {audio.size_mb:.1f} MB, limit is "
                f"{self.settings.max_audio_mb} MB",
                user_message="Голосовое сообщение слишком длинное. Запишите покороче или напишите текстом.",
            )

        kwargs: dict[str, object] = {
            "model": model,
            # The SDK accepts a (filename, bytes, mime) tuple; the filename
            # extension is what several gateways use to pick a decoder.
            "file": (audio.filename, audio.data, audio.mime_type),
            "response_format": "json",
        }
        if language:
            kwargs["language"] = language
        if timeout is not None:
            kwargs["timeout"] = timeout

        try:
            result = await self._client.audio.transcriptions.create(**kwargs)  # type: ignore[arg-type]
        except APITimeoutError as exc:
            raise STTError(f"{self.name}: transcription timed out") from exc
        except RateLimitError as exc:
            raise STTError(f"{self.name}: rate limited ({exc})") from exc
        except APIConnectionError as exc:
            raise STTError(f"{self.name}: cannot reach endpoint ({exc})") from exc
        except APIStatusError as exc:
            raise STTError(f"{self.name}: HTTP {exc.status_code} ({exc.message})") from exc
        except OpenAIError as exc:
            raise STTError(f"{self.name}: {exc}") from exc

        text = (getattr(result, "text", "") or "").strip()
        if not text:
            raise STTError(
                f"{self.name}: transcription came back empty",
                user_message="В голосовом сообщении не распознана речь. Попробуйте записать ещё раз.",
            )

        return Transcript(
            text=text,
            language=getattr(result, "language", None) or language,
            provider=self.name,
            model=model,
            duration_seconds=getattr(result, "duration", None),
        )

    async def aclose(self) -> None:
        await self._client.close()


# ---------------------------------------------------------------------------
# Named gateways -- see the LLM providers for the same pattern.
# ---------------------------------------------------------------------------


@register_stt("groq_whisper", "groq")
class GroqWhisperProvider(OpenAIWhisperProvider):
    """Groq's Whisper endpoints; use model `whisper-large-v3-turbo`."""

    name: ClassVar[str] = "groq_whisper"
    default_base_url: ClassVar[str | None] = "https://api.groq.com/openai/v1"
    api_key_env_vars: ClassVar[tuple[str, ...]] = ("GROQ_API_KEY",)


@register_stt("nvidia", "riva")
class NvidiaSTTProvider(OpenAIWhisperProvider):
    """NVIDIA NIM ASR.

    NVIDIA's hosted ASR NIMs expose an OpenAI-compatible transcription route,
    so this is the generic client pointed at their endpoint. For a self-hosted
    Riva container set ``STT_BASE_URL`` to its ``/v1`` address.
    """

    name: ClassVar[str] = "nvidia"
    default_base_url: ClassVar[str | None] = "https://integrate.api.nvidia.com/v1"
    api_key_env_vars: ClassVar[tuple[str, ...]] = ("NVIDIA_API_KEY", "NGC_API_KEY")
