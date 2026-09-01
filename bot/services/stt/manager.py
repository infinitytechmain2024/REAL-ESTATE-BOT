"""Provider selection and fallback for transcription."""

from __future__ import annotations

import asyncio
import time

from bot.config import STTSettings
from bot.exceptions import ConfigurationError, STTError
from bot.logging_conf import get_logger
from bot.services.stt.base import AudioFile, STTProvider, Transcript
from bot.services.stt.registry import create_provider

log = get_logger(__name__)

_BACKOFF_BASE_SECONDS = 1.5
_MAX_RETRIES = 1
"""Audio uploads are expensive; one retry per provider, then move on."""


class STTManager:
    """Facade over the configured transcription providers."""

    def __init__(self, settings: STTSettings) -> None:
        self.settings = settings
        chain: list[str] = []
        for name in (settings.provider, *settings.fallback_providers):
            key = name.strip().lower()
            if key and key not in chain:
                chain.append(key)
        if not chain:
            raise ConfigurationError("STT_PROVIDER is empty")
        self.chain = chain
        self._providers: dict[str, STTProvider] = {}
        self._lock = asyncio.Lock()

    async def _provider(self, name: str) -> STTProvider:
        provider = self._providers.get(name)
        if provider is not None:
            return provider
        async with self._lock:
            provider = self._providers.get(name)
            if provider is None:
                provider = create_provider(name, self.settings)
                self._providers[name] = provider
            return provider

    async def transcribe(self, audio: AudioFile, *, language: str | None = None) -> Transcript:
        """Transcribe *audio*, walking the provider chain until one succeeds."""
        if not self.settings.enabled:
            raise STTError(
                "STT is disabled by configuration",
                user_message="Распознавание голоса сейчас отключено. Напишите запрос текстом, пожалуйста.",
            )

        errors: list[str] = []
        last: STTError | None = None

        for provider_name in self.chain:
            try:
                provider = await self._provider(provider_name)
            except ConfigurationError as exc:
                log.warning("stt.provider.unavailable", provider=provider_name, error=str(exc))
                errors.append(f"{provider_name}: {exc}")
                continue

            for attempt in range(_MAX_RETRIES + 1):
                started = time.monotonic()
                try:
                    transcript = await provider.transcribe(
                        audio,
                        model=self.settings.model,
                        language=language or self.settings.language,
                        timeout=self.settings.timeout_seconds,
                    )
                except STTError as exc:
                    last = exc
                    errors.append(f"{provider_name}: {exc}")
                    is_last_attempt = attempt == _MAX_RETRIES
                    log.warning(
                        "stt.call.failed",
                        provider=provider_name,
                        attempt=attempt + 1,
                        will_retry=not is_last_attempt,
                        error=str(exc),
                    )
                    if is_last_attempt:
                        break
                    await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                else:
                    log.info(
                        "stt.call.ok",
                        provider=provider_name,
                        model=self.settings.model,
                        chars=len(transcript.text),
                        language=transcript.language,
                        elapsed_ms=round((time.monotonic() - started) * 1000),
                    )
                    return transcript

        # Prefer the last provider's own wording (e.g. "audio too long"), since
        # it usually tells the user something actionable.
        raise STTError(
            "all STT providers failed: " + "; ".join(errors[-3:]),
            user_message=last.user_message if last else None,
        )

    async def aclose(self) -> None:
        for name, provider in self._providers.items():
            try:
                await provider.aclose()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                log.warning("stt.provider.close_failed", provider=name, exc_info=True)
        self._providers.clear()
