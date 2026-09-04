"""Provider registry.

The whole point of this module is that ``LLM_PROVIDER=<name>`` is enough to
switch backends. A provider registers itself with :func:`register_llm` and is
built by :func:`create_provider` from the same :class:`LLMSettings` every other
provider sees -- no branching anywhere else in the codebase.
"""

from __future__ import annotations

from collections.abc import Callable

from bot.config import LLMSettings
from bot.exceptions import ConfigurationError
from bot.services.llm.base import LLMProvider

ProviderFactory = Callable[[LLMSettings], LLMProvider]

_REGISTRY: dict[str, ProviderFactory] = {}


def register_llm(*names: str) -> Callable[[type[LLMProvider]], type[LLMProvider]]:
    """Class decorator registering a provider under one or more names.

    Aliases exist so that ``LLM_PROVIDER=openrouter`` and
    ``LLM_PROVIDER=groq`` can both resolve to the OpenAI-compatible client
    while still reading naturally in a ``.env`` file.
    """

    def decorator(cls: type[LLMProvider]) -> type[LLMProvider]:
        for name in names:
            key = name.strip().lower()
            if key in _REGISTRY:
                raise ConfigurationError(f"LLM provider {key!r} is already registered")
            _REGISTRY[key] = cls  # type: ignore[assignment]
        return cls

    return decorator


def available_providers() -> list[str]:
    """Names accepted by ``LLM_PROVIDER``, sorted."""
    return sorted(_REGISTRY)


def create_provider(name: str, settings: LLMSettings) -> LLMProvider:
    """Instantiate the provider registered under *name*."""
    key = (name or "").strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise ConfigurationError(
            f"Неизвестный LLM-провайдер в LLM_PROVIDER: '{name}'.\n"
            f"  Доступные значения: {', '.join(available_providers())}"
        )
    return factory(settings)
