"""Provider selection, retries and fallback.

The pipeline only ever talks to :class:`LLMManager`. It owns the provider
chain -- ``LLM_PROVIDER`` first, then each of ``LLM_FALLBACK_PROVIDERS`` -- and
gives every provider ``LLM_MAX_RETRIES`` attempts with exponential backoff
before moving on. Providers are built lazily, so a fallback whose key is not
configured only fails if it is actually reached.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, TypeVar

from pydantic import BaseModel

from bot.config import LLMSettings
from bot.exceptions import ConfigurationError, LLMError, LLMTimeoutError
from bot.logging_conf import get_logger
from bot.services.llm.base import ChatMessage, LLMProvider, LLMResponse
from bot.services.llm.registry import create_provider

log = get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

_BACKOFF_BASE_SECONDS = 1.5

_MAX_TIMEOUT_ATTEMPTS = 1
"""Timeouts are retried at most once, whatever LLM_MAX_RETRIES says.

Every other failure fails fast; a timeout burns the full LLM_TIMEOUT_SECONDS
first. Three attempts at a 90s timeout is four and a half minutes of the user
staring at a progress message before the fallback even starts -- and a prompt
that was too large, or a model that is too slow, will simply time out again.
"""


class LLMManager:
    """Facade over the configured providers."""

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        # Preserve order, drop duplicates: a fallback equal to the primary
        # would just double the retry count for no benefit.
        chain: list[str] = []
        for name in (settings.provider, *settings.fallback_providers):
            key = name.strip().lower()
            if key and key not in chain:
                chain.append(key)
        if not chain:
            raise ConfigurationError("LLM_PROVIDER is empty")
        self.chain = chain
        self._providers: dict[str, LLMProvider] = {}
        self._lock = asyncio.Lock()

    async def _provider(self, name: str) -> LLMProvider:
        """Build *name* on first use and cache it."""
        provider = self._providers.get(name)
        if provider is not None:
            return provider
        async with self._lock:
            # Re-check: another task may have built it while we waited.
            provider = self._providers.get(name)
            if provider is None:
                provider = create_provider(name, self.settings)
                self._providers[name] = provider
            return provider

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        json_schema: dict[str, Any] | None = None,
        purpose: str = "chat",
    ) -> LLMResponse:
        """Send *messages*, walking the provider chain until one succeeds."""

        async def call(provider: LLMProvider) -> LLMResponse:
            return await provider.chat(
                messages,
                model=model or self.settings.model,
                temperature=self.settings.temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.settings.max_tokens,
                json_schema=json_schema,
                timeout=self.settings.timeout_seconds,
            )

        return await self._run(call, purpose=purpose, model=model or self.settings.model)

    async def chat_structured(
        self,
        messages: list[ChatMessage],
        schema: type[ModelT],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str = "structured",
    ) -> ModelT:
        """Same as :meth:`chat`, but validated into *schema*."""

        async def call(provider: LLMProvider) -> ModelT:
            return await provider.chat_structured(
                messages,
                schema,
                model=model or self.settings.model,
                temperature=self.settings.temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.settings.max_tokens,
                timeout=self.settings.timeout_seconds,
            )

        return await self._run(call, purpose=purpose, model=model or self.settings.model)

    async def _run(self, call, *, purpose: str, model: str):  # type: ignore[no-untyped-def]
        """Try every provider in the chain, retrying each one."""
        errors: list[str] = []

        for provider_name in self.chain:
            try:
                provider = await self._provider(provider_name)
            except ConfigurationError as exc:
                # A fallback that was never configured is not an error worth
                # failing on -- note it and try the next link.
                log.warning("llm.provider.unavailable", provider=provider_name, error=str(exc))
                errors.append(f"{provider_name}: {exc}")
                continue

            for attempt in range(self.settings.max_retries + 1):
                started = time.monotonic()
                try:
                    result = await call(provider)
                except LLMError as exc:
                    errors.append(f"{provider_name}: {exc}")
                    budget = (
                        min(self.settings.max_retries, _MAX_TIMEOUT_ATTEMPTS)
                        if isinstance(exc, LLMTimeoutError)
                        else self.settings.max_retries
                    )
                    is_last_attempt = attempt >= budget
                    log.warning(
                        "llm.call.failed",
                        provider=provider_name,
                        purpose=purpose,
                        attempt=attempt + 1,
                        will_retry=not is_last_attempt,
                        timed_out=isinstance(exc, LLMTimeoutError),
                        error=str(exc),
                    )
                    if is_last_attempt:
                        break
                    await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                else:
                    log.info(
                        "llm.call.ok",
                        provider=provider_name,
                        purpose=purpose,
                        model=model,
                        attempt=attempt + 1,
                        elapsed_ms=round((time.monotonic() - started) * 1000),
                    )
                    return result

        raise LLMError(
            f"all LLM providers failed for {purpose!r}: " + "; ".join(errors[-4:]),
            user_message="ИИ-провайдер сейчас недоступен. Попробуйте повторить запрос через минуту.",
        )

    async def aclose(self) -> None:
        """Close every provider that was actually built."""
        for name, provider in self._providers.items():
            try:
                await provider.aclose()
            except Exception:
                log.warning("llm.provider.close_failed", provider=name, exc_info=True)
        self._providers.clear()
