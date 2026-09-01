"""LLM abstraction.

Importing this package registers every bundled provider, so
``create_provider`` and :class:`LLMManager` can resolve any name that
``LLM_PROVIDER`` accepts.

To add a provider: create a module here, subclass
:class:`~bot.services.llm.base.LLMProvider` (or
:class:`~bot.services.llm.openai_compatible.OpenAICompatibleProvider` if the
backend speaks the OpenAI protocol), decorate it with ``@register_llm(...)``
and import it below. Nothing else changes.
"""

from bot.services.llm import anthropic_provider, nvidia, openai_compatible  # noqa: F401
from bot.services.llm.base import ChatMessage, LLMProvider, LLMResponse
from bot.services.llm.manager import LLMManager
from bot.services.llm.registry import available_providers, create_provider, register_llm

__all__ = [
    "ChatMessage",
    "LLMManager",
    "LLMProvider",
    "LLMResponse",
    "available_providers",
    "create_provider",
    "register_llm",
]
