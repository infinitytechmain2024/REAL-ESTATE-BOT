"""Spoken commands and Whisper sign-off cleanup for the Telegram voice path."""

from __future__ import annotations

import pytest

from bot.control_plane.models import CommandEnvelope, IncomingMessage, TranscriptResult
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore
from bot.control_plane.voice_commands import clean_transcript, spoken_command


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        # The transcript from the first live test on the VPS.
        ('Thank you.  "Show status"', "Show status"),
        ("Покажи статус. Спасибо за просмотр!", "Покажи статус."),
        ("Субтитры сделал DimaTorzok. Пауза всё. Продолжение следует...", "Пауза всё."),
        ("Дякую за перегляд. Статус", "Статус"),
        ("Gracias por ver. Estado", "Estado"),
        # A sign-off alone may be what was actually said: it is kept.
        ("Thank you.", "Thank you."),
        # Thanks inside a sentence is speech, not a sign-off.
        ("Thank you for the status update", "Thank you for the status update"),
    ],
)
def test_whisper_sign_offs_are_trimmed_only_at_the_edges(raw: str, cleaned: str) -> None:
    assert clean_transcript(raw) == cleaned


@pytest.mark.parametrize(
    ("spoken", "command"),
    [
        ("Show status", "/status"),
        ("Покажи статус", "/status"),
        ("Покажи стан", "/status"),
        ("Muéstrame el estado", "/status"),
        ("Help", "/help"),
        ("Какие есть команды?", "/help"),
        ("Допомога", "/help"),
        ("Ayuda, por favor", "/help"),
        ("Pause all", "/pause all"),
        ("Поставь всё на паузу", "/pause all"),
        ("Призупини все", "/pause all"),
        ("Pausa todo", "/pause all"),
        ("Resume everything", "/resume all"),
        ("Продолжи всё", "/resume all"),
        ("Відновити все", "/resume all"),
        ("Reanuda todo", "/resume all"),
        ("Cancel all", "/cancel all"),
        ("Отмени всё", "/cancel all"),
        ("Скасуй усі", "/cancel all"),
        ("Cancela todos", "/cancel all"),
    ],
)
def test_short_phrases_map_to_commands(spoken: str, command: str) -> None:
    assert spoken_command(spoken) == command


@pytest.mark.parametrize(
    "spoken",
    [
        "Pause",  # no spoken scope: never guess `all`
        "Отмени",
        "Cancel the pause",  # two intents
        "What is the status of the Madrid plot and when will the owner call me back about it",  # conversation
        "/status",  # already a command
        "confirm 0123456789",
        "Run facebook groups",  # /run needs URLs
        "Hello there",
        "",
    ],
)
def test_unclear_phrases_are_not_mapped(spoken: str) -> None:
    assert spoken_command(spoken) is None


class FakeTranscriber:
    model = "openai/whisper-large-v3-turbo"
    provider = "openrouter"

    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, audio: bytes, *, filename: str) -> TranscriptResult:
        return TranscriptResult(self.text, "en", 0.44, self.model)


def settings() -> ControlPlaneSettings:
    return ControlPlaneSettings(telegram_token="t", database_url="postgresql://x", operator_user_ids=frozenset({11}))


def voice(message_id: int = 1) -> IncomingMessage:
    return IncomingMessage(chat_id=22, user_id=11, message_id=message_id, voice_file_id="f", voice_size=10, voice_duration_seconds=2)


async def ogg() -> bytes:
    return b"OggS"


@pytest.mark.asyncio
async def test_the_live_transcript_now_runs_status() -> None:
    store = MemoryControlPlaneStore()
    raw = 'Thank you.  "Show status"'
    control = ControlPlane(settings(), store, FakeTranscriber(raw), lambda _: None)

    reply = await control.handle_voice(voice(), ogg)

    assert reply is not None
    assert reply.text.startswith("Transcript (en, confidence 44%):\nShow status\nUnderstood as: /status")
    assert "Control plane is online" in reply.text
    # The audit keeps exactly what the provider returned.
    assert store.transcripts[(22, 1)].text == raw


@pytest.mark.asyncio
async def test_a_spoken_pause_still_needs_typed_confirmation() -> None:
    received: list[CommandEnvelope] = []

    async def sink(command: CommandEnvelope) -> None:
        received.append(command)

    store = MemoryControlPlaneStore()
    control = ControlPlane(settings(), store, FakeTranscriber("Поставь всё на паузу."), sink)

    prompt = await control.handle_voice(voice(1), ogg)
    assert prompt and "Understood as: /pause all" in prompt.text
    assert "Confirmation required for /pause" in prompt.text
    assert received == []

    token = prompt.text.split("confirm ")[1].split()[0]
    confirmed = await control.handle_text(IncomingMessage(chat_id=22, user_id=11, message_id=2, text=f"confirm {token}"))
    assert confirmed and "Confirmed: /pause" in confirmed.text
    assert received == [CommandEnvelope("pause", "all", 22, 11, 2)]


@pytest.mark.asyncio
async def test_unmapped_speech_is_shown_without_a_command_line() -> None:
    control = ControlPlane(settings(), MemoryControlPlaneStore(), FakeTranscriber("Hello there"), lambda _: None)
    reply = await control.handle_voice(voice(), ogg)
    assert reply and "Understood as" not in reply.text and "Send /help" in reply.text
