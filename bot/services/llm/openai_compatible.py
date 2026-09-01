"""OpenAI-compatible chat completions.

One client covers OpenAI itself and every service that speaks its wire format:
OpenRouter, Groq, Together, Fireworks, DeepInfra, Perplexity, local vLLM or
Ollama, and NVIDIA NIM (which gets its own thin subclass in ``nvidia.py`` only
so that the endpoint default is right).

Which one you get is decided by ``LLM_BASE_URL`` and ``LLM_API_KEY``; the alias
names registered below exist purely so ``.env`` reads well.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

import httpx
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    OpenAIError,
    RateLimitError,
)

from bot.config import LLMSettings
from bot.exceptions import ConfigurationError, LLMError
from bot.logging_conf import get_logger
from bot.services.llm.base import ChatMessage, LLMProvider, LLMResponse, Usage
from bot.services.llm.registry import register_llm

log = get_logger(__name__)


@register_llm("openai_compatible", "openai")
class OpenAICompatibleProvider(LLMProvider):
    """Chat completions over the OpenAI protocol."""

    name: ClassVar[str] = "openai_compatible"
    supports_json_mode: ClassVar[bool] = True

    #: Used when ``LLM_BASE_URL`` is unset. ``None`` means "the SDK default",
    #: i.e. api.openai.com.
    default_base_url: ClassVar[str | None] = None

    #: Environment variables consulted for a key, in order, when
    #: ``LLM_API_KEY`` is not set. Lets a user keep OPENROUTER_API_KEY and
    #: GROQ_API_KEY side by side and switch with LLM_PROVIDER alone.
    api_key_env_vars: ClassVar[tuple[str, ...]] = ("OPENAI_API_KEY",)

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        api_key = self._resolve_api_key(settings)
        base_url = settings.base_url or self.default_base_url

        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=httpx.Timeout(settings.timeout_seconds),
            # Retries are handled by LLMManager so that a persistent failure
            # reaches the fallback provider quickly instead of being retried
            # twice over at two levels.
            max_retries=0,
        )
        log.debug("llm.provider.init", provider=self.name, base_url=base_url or "openai-default")

    def _resolve_api_key(self, settings: LLMSettings) -> str:
        if settings.api_key:
            return settings.api_key.get_secret_value()
        for var in self.api_key_env_vars:
            value = os.getenv(var)
            if value:
                log.debug("llm.provider.key_from_env", provider=self.name, env_var=var)
                return value
        raise ConfigurationError(
            f"no API key for LLM provider {self.name!r}: set LLM_API_KEY or one of "
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
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            # `json_object` rather than `json_schema`: strict schema mode is not
            # supported by every OpenAI-compatible gateway, and the schema is
            # already spelled out in the prompt by chat_structured.
            payload["response_format"] = {"type": "json_object"}
        if timeout is not None:
            payload["timeout"] = timeout

        try:
            completion = await self._client.chat.completions.create(**payload)
        except APITimeoutError as exc:
            raise LLMError(f"{self.name}: request timed out after {timeout or self.settings.timeout_seconds}s") from exc
        except RateLimitError as exc:
            raise LLMError(f"{self.name}: rate limited ({exc})") from exc
        except APIConnectionError as exc:
            raise LLMError(f"{self.name}: cannot reach endpoint ({exc})") from exc
        except APIStatusError as exc:
            raise LLMError(f"{self.name}: HTTP {exc.status_code} from endpoint ({exc.message})") from exc
        except OpenAIError as exc:
            raise LLMError(f"{self.name}: {exc}") from exc

        if not completion.choices:
            raise LLMError(f"{self.name}: response contained no choices")

        choice = completion.choices[0]
        usage = Usage(
            prompt_tokens=getattr(completion.usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(completion.usage, "completion_tokens", 0) or 0,
        )
        return LLMResponse(
            text=choice.message.content or "",
            model=completion.model or model,
            provider=self.name,
            usage=usage,
            finish_reason=choice.finish_reason,
        )

    async def aclose(self) -> None:
        await self._client.close()


# ---------------------------------------------------------------------------
# Named gateways.
#
# Each is the generic client with the right endpoint and key variable baked in,
# so `LLM_PROVIDER=groq` plus `GROQ_API_KEY` is a complete configuration. This
# is also the template for any future provider: subclass, set three class
# attributes, register. Nothing else in the codebase changes.
# ---------------------------------------------------------------------------


@register_llm("openrouter")
class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter -- one key, most models. https://openrouter.ai/docs"""

    name: ClassVar[str] = "openrouter"
    default_base_url: ClassVar[str | None] = "https://openrouter.ai/api/v1"
    api_key_env_vars: ClassVar[tuple[str, ...]] = ("OPENROUTER_API_KEY", "OPENAI_API_KEY")


@register_llm("groq")
class GroqProvider(OpenAICompatibleProvider):
    """Groq -- very fast inference for open-weight models."""

    name: ClassVar[str] = "groq"
    default_base_url: ClassVar[str | None] = "https://api.groq.com/openai/v1"
    api_key_env_vars: ClassVar[tuple[str, ...]] = ("GROQ_API_KEY",)


@register_llm("together")
class TogetherProvider(OpenAICompatibleProvider):
    """Together AI."""

    name: ClassVar[str] = "together"
    default_base_url: ClassVar[str | None] = "https://api.together.xyz/v1"
    api_key_env_vars: ClassVar[tuple[str, ...]] = ("TOGETHER_API_KEY",)


@register_llm("fireworks")
class FireworksProvider(OpenAICompatibleProvider):
    """Fireworks AI."""

    name: ClassVar[str] = "fireworks"
    default_base_url: ClassVar[str | None] = "https://api.fireworks.ai/inference/v1"
    api_key_env_vars: ClassVar[tuple[str, ...]] = ("FIREWORKS_API_KEY",)
