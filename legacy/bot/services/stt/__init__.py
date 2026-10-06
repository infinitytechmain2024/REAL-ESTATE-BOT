"""Speech-to-text abstraction.

Importing this package registers every bundled provider. To add one: subclass
:class:`~bot.services.stt.base.STTProvider` (or
:class:`~bot.services.stt.openai_whisper.OpenAIWhisperProvider` for anything
speaking the OpenAI audio protocol), decorate with ``@register_stt(...)`` and
import it below.
"""

from bot.services.stt import openai_whisper  # noqa: F401
from bot.services.stt.base import AudioFile, STTProvider, Transcript
from bot.services.stt.manager import STTManager
from bot.services.stt.registry import available_providers, create_provider, register_stt

__all__ = [
    "AudioFile",
    "STTManager",
    "STTProvider",
    "Transcript",
    "available_providers",
    "create_provider",
    "register_stt",
]
