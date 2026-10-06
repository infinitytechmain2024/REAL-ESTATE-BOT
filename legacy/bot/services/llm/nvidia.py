"""NVIDIA inference providers.

NVIDIA's hosted endpoints (``integrate.api.nvidia.com``, what NGC calls the
NIM API catalogue) and self-hosted NIM containers both speak the OpenAI chat
protocol, so this is the generic client with NVIDIA's endpoint and key
variable filled in.

Point ``LLM_BASE_URL`` at a self-hosted container to use one instead::

    LLM_PROVIDER=nvidia
    LLM_BASE_URL=http://nim-container:8000/v1
    LLM_MODEL=meta/llama-3.3-70b-instruct
"""

from __future__ import annotations

from typing import ClassVar

from bot.services.llm.openai_compatible import OpenAICompatibleProvider
from bot.services.llm.registry import register_llm


@register_llm("nvidia", "nim")
class NvidiaProvider(OpenAICompatibleProvider):
    """NVIDIA NIM / NGC catalogue endpoints."""

    name: ClassVar[str] = "nvidia"
    default_base_url: ClassVar[str | None] = "https://integrate.api.nvidia.com/v1"
    api_key_env_vars: ClassVar[tuple[str, ...]] = ("NVIDIA_API_KEY", "NGC_API_KEY")
