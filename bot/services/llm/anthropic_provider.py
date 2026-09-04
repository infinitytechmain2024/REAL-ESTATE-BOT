"""Anthropic (Claude) provider.

Anthropic's Messages API differs from OpenAI's in two ways that matter here:
the system prompt is a top-level parameter rather than a message, and there is
no JSON response format -- structured output is steered through the prompt,
which :meth:`LLMProvider.chat_structured` already does for providers that
report ``supports_json_mode = False``.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

import httpx
from anthropic import (
    AnthropicError,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    RateLimitError,
)

from bot.config import LLMSettings
from bot.exceptions import ConfigurationError, LLMError
from bot.logging_conf import get_logger
from bot.services.llm.base import ChatMessage, LLMProvider, LLMResponse, Usage
from bot.services.llm.registry import register_llm

log = get_logger(__name__)


@register_llm("anthropic", "claude")
class AnthropicProvider(LLMProvider):
    """Claude via the Messages API."""

    name: ClassVar[str] = "anthropic"
    supports_json_mode: ClassVar[bool] = False

    api_key_env_vars: ClassVar[tuple[str, ...]] = ("ANTHROPIC_API_KEY",)

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        self._client = AsyncAnthropic(
            api_key=self._resolve_api_key(settings),
            base_url=settings.base_url or None,
            timeout=httpx.Timeout(settings.timeout_seconds),
            max_retries=0,  # LLMManager owns retry/fallback
        )
        log.debug("llm.provider.init", provider=self.name)

    def _resolve_api_key(self, settings: LLMSettings) -> str:
        if settings.api_key:
            return settings.api_key.get_secret_value()
        for var in self.api_key_env_vars:
            value = os.getenv(var)
            if value:
                return value
        raise ConfigurationError(
            f"Не задан API-ключ для LLM-провайдера '{self.name}'.\n"
            f"  Задайте одну из переменных: LLM_API_KEY, "
            f"{', '.join(self.api_key_env_vars)}"
        )

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_schema: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> LLMResponse:
        # Anthropic takes the system prompt out of band; several system turns
        # are merged so callers do not have to care about the difference.
        system_parts = [m.content for m in messages if m.role == "system"]
        turns = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        if not turns:
            raise LLMError(f"{self.name}: at least one user message is required")

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": turns,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)
        if timeout is not None:
            kwargs["timeout"] = timeout

        try:
            message = await self._client.messages.create(**kwargs)
        except APITimeoutError as exc:
            raise LLMError(f"{self.name}: request timed out") from exc
        except RateLimitError as exc:
            raise LLMError(f"{self.name}: rate limited ({exc})") from exc
        except APIConnectionError as exc:
            raise LLMError(f"{self.name}: cannot reach endpoint ({exc})") from exc
        except APIStatusError as exc:
            raise LLMError(f"{self.name}: HTTP {exc.status_code} ({exc.message})") from exc
        except AnthropicError as exc:
            raise LLMError(f"{self.name}: {exc}") from exc

        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        return LLMResponse(
            text=text,
            model=message.model or model,
            provider=self.name,
            usage=Usage(
                prompt_tokens=message.usage.input_tokens or 0,
                completion_tokens=message.usage.output_tokens or 0,
            ),
            finish_reason=message.stop_reason,
        )

    async def aclose(self) -> None:
        await self._client.close()
