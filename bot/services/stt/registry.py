"""Registry for speech-to-text providers -- same contract as the LLM one."""

from __future__ import annotations

from collections.abc import Callable

from bot.config import STTSettings
from bot.exceptions import ConfigurationError
from bot.services.stt.base import STTProvider

ProviderFactory = Callable[[STTSettings], STTProvider]

_REGISTRY: dict[str, ProviderFactory] = {}


def register_stt(*names: str) -> Callable[[type[STTProvider]], type[STTProvider]]:
    """Class decorator registering a provider under one or more names."""

    def decorator(cls: type[STTProvider]) -> type[STTProvider]:
        for name in names:
            key = name.strip().lower()
            if key in _REGISTRY:
                raise ConfigurationError(f"STT provider {key!r} is already registered")
            _REGISTRY[key] = cls  # type: ignore[assignment]
        return cls

    return decorator


def available_providers() -> list[str]:
    """Names accepted by ``STT_PROVIDER``, sorted."""
    return sorted(_REGISTRY)


def create_provider(name: str, settings: STTSettings) -> STTProvider:
    """Instantiate the provider registered under *name*."""
    key = (name or "").strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        raise ConfigurationError(
            f"unknown STT provider {name!r}; available: {', '.join(available_providers())}"
        )
    return factory(settings)
