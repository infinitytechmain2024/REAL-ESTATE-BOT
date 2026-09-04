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
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel

from bot.config import LLMSettings
from bot.exceptions import ConfigurationError, LLMError
from bot.logging_conf import get_logger
from bot.services.llm.base import ChatMessage, LLMProvider, LLMResponse
from bot.services.llm.registry import create_provider

if TYPE_CHECKING:
    from bot.services.costs import CostGuard

log = get_logger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

_BACKOFF_BASE_SECONDS = 1.5


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
        # Injected by `bot.main` after construction: the guard needs the
        # repository and the Bot, both of which are built after this.
        self.cost_guard: CostGuard | None = None

    def attach_cost_guard(self, guard: CostGuard) -> None:
        """Start metering calls and enforcing the daily budget."""
        self.cost_guard = guard

    async def validate(self) -> None:
        """Build the primary provider now, so a missing key fails at start-up.

        Without this a deployment with no ``*_API_KEY`` looks healthy until the
        first user sends a message and gets a generic failure.
        """
        await self._provider(self.chain[0])

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
        user_id: int | None = None,
    ) -> LLMResponse:
        """Send *messages*, walking the provider chain until one succeeds."""
        collected: list[LLMResponse] = []

        async def call(provider: LLMProvider) -> LLMResponse:
            response = await provider.chat(
                messages,
                model=model or self.settings.model,
                temperature=self.settings.temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.settings.max_tokens,
                json_schema=json_schema,
                timeout=self.settings.timeout_seconds,
            )
            collected.append(response)
            return response

        return await self._run(
            call,
            purpose=purpose,
            model=model or self.settings.model,
            collected=collected,
            user_id=user_id,
        )

    async def chat_structured(
        self,
        messages: list[ChatMessage],
        schema: type[ModelT],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str = "structured",
        user_id: int | None = None,
    ) -> ModelT:
        """Same as :meth:`chat`, but validated into *schema*."""
        collected: list[LLMResponse] = []

        async def call(provider: LLMProvider) -> ModelT:
            return await provider.chat_structured(
                messages,
                schema,
                model=model or self.settings.model,
                temperature=self.settings.temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.settings.max_tokens,
                timeout=self.settings.timeout_seconds,
                # Every underlying call lands here, repairs included, so a
                # structured request is metered as accurately as a plain one.
                usage_sink=collected.append,
            )

        return await self._run(
            call,
            purpose=purpose,
            model=model or self.settings.model,
            collected=collected,
            user_id=user_id,
        )

    async def _run(  # type: ignore[no-untyped-def]
        self,
        call,
        *,
        purpose: str,
        model: str,
        collected: list[LLMResponse],
        user_id: int | None,
    ):
        """Try every provider in the chain, retrying each one.

        The daily budget is checked once before the chain starts and again
        before each retry, so a run that trips the limit mid-way stops instead
        of retrying its way further over it. Whatever *was* spent is always
        metered, including on the failure paths -- a call that timed out after
        the provider generated its tokens still costs money.
        """
        errors: list[str] = []

        try:
            self._ensure_budget()

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
                        is_last_attempt = attempt == self.settings.max_retries
                        log.warning(
                            "llm.call.failed",
                            provider=provider_name,
                            purpose=purpose,
                            attempt=attempt + 1,
                            will_retry=not is_last_attempt,
                            error=str(exc),
                        )
                        if is_last_attempt:
                            break
                        await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2**attempt))
                        # Retrying costs another call; do not start one on an
                        # exhausted budget.
                        self._ensure_budget()
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
                user_message=(
                    "ИИ-провайдер сейчас недоступен. Попробуйте повторить запрос через минуту."
                ),
            )
        finally:
            await self._meter(collected, purpose=purpose, user_id=user_id)

    def _ensure_budget(self) -> None:
        """Stop before spending anything if the day's budget is already gone."""
        if self.cost_guard is not None:
            self.cost_guard.ensure_within_budget()

    async def _meter(
        self, collected: list[LLMResponse], *, purpose: str, user_id: int | None
    ) -> None:
        """Record what every response in this run actually cost."""
        if self.cost_guard is None or not collected:
            return
        for response in collected:
            try:
                await self.cost_guard.record(
                    provider=response.provider,
                    model=response.model,
                    purpose=purpose,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                    user_id=user_id,
                )
            except Exception:  # noqa: BLE001 - accounting must never fail a request
                log.warning("llm.metering_failed", purpose=purpose, exc_info=True)
        collected.clear()

    async def aclose(self) -> None:
        """Close every provider that was actually built."""
        for name, provider in self._providers.items():
            try:
                await provider.aclose()
            except Exception:  # noqa: BLE001 - a failed close must not block shutdown
                log.warning("llm.provider.close_failed", provider=name, exc_info=True)
        self._providers.clear()
