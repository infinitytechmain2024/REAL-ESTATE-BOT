"""Unit tests for the isolated Telegram control-plane safety boundary."""

from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from bot.control_plane.models import CommandEnvelope, IncomingMessage, TranscriptResult
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore
from bot.control_plane.stt import TranscriptionError


@dataclass
class FakeTranscriber:
    result: TranscriptResult | Exception
    model: str = "openai/whisper-large-v3-turbo"
    provider: str = "openrouter"
    calls: int = 0

    async def transcribe(self, audio: bytes, *, filename: str) -> TranscriptResult:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def audio(payload: bytes = b"OggS-opus"):
    async def download() -> bytes:
        return payload

    return download


def settings() -> ControlPlaneSettings:
    return ControlPlaneSettings(
        telegram_token="123:test",
        database_url="postgresql://example",
        confirmation_ttl_seconds=300,
        operator_user_ids=frozenset({11}),
    )


def message(
    message_id: int = 1,
    text: str | None = "/status",
    *,
    user_id: int | None = 11,
    chat_id: int = 22,
    voice: bool = False,
) -> IncomingMessage:
    return IncomingMessage(
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
        text=text,
        voice_file_id="voice-id" if voice else None,
    )


@pytest.mark.asyncio
async def test_any_user_and_chat_can_use_the_open_control_plane() -> None:
    control = ControlPlane(
        settings(),
        MemoryControlPlaneStore(),
        FakeTranscriber(TranscriptResult("", None, None, "small")),
        lambda _: None,
    )
    first = await control.handle_text(message(message_id=10, user_id=999, chat_id=999))
    second = await control.handle_text(message(message_id=11, user_id=None, chat_id=-100123))
    assert first and "online" in first.text
    assert second and "online" in second.text


@pytest.mark.asyncio
async def test_text_is_handled_and_message_id_is_deduplicated() -> None:
    control = ControlPlane(
        settings(),
        MemoryControlPlaneStore(),
        FakeTranscriber(TranscriptResult("", None, None, "small")),
        lambda _: None,
    )
    response = await control.handle_text(message())
    duplicate = await control.handle_text(message())
    assert response and "online" in response.text
    assert duplicate and duplicate.text == "Duplicate update ignored."


@pytest.mark.asyncio
async def test_voice_transcription_is_saved_and_echoed() -> None:
    store = MemoryControlPlaneStore()
    transcript = TranscriptResult("/status", "uk", 0.91, "small")
    control = ControlPlane(settings(), store, FakeTranscriber(transcript), lambda _: None)
    response = await control.handle_voice(message(3, None, voice=True), audio(b"opus"))
    assert response and "Transcript (uk, confidence 91%)" in response.text
    assert "Control plane is online" in response.text
    assert store.transcripts[(22, 3)] == transcript


@pytest.mark.asyncio
async def test_state_change_requires_one_time_confirmation() -> None:
    received: list[CommandEnvelope] = []

    async def sink(command: CommandEnvelope) -> None:
        received.append(command)

    control = ControlPlane(
        settings(),
        MemoryControlPlaneStore(),
        FakeTranscriber(TranscriptResult("", None, None, "small")),
        sink,
    )
    prompt = await control.handle_text(message(4, "/run facebook-batch-a"))
    assert prompt and "Confirmation required" in prompt.text
    token = prompt.text.split("confirm ")[1].split()[0]
    completed = await control.handle_text(message(5, f"confirm {token}"))
    assert completed and "Confirmed: /run" in completed.text
    assert received == [CommandEnvelope("run", "facebook-batch-a", 22, 11, 5)]
    reuse = await control.handle_text(message(6, f"confirm {token}"))
    assert reuse and "invalid, expired" in reuse.text


@pytest.mark.asyncio
async def test_transcription_and_voice_size_errors_are_clear() -> None:
    failed = ControlPlane(
        settings(),
        MemoryControlPlaneStore(),
        FakeTranscriber(TranscriptionError("empty_transcript", "no speech")),
        lambda _: None,
    )
    response = await failed.handle_voice(message(7, None, voice=True), audio(b"opus"))
    assert response and "could not transcribe" in response.text and "No speech" in response.text

    too_small = replace(settings(), stt_max_audio_bytes=1)
    oversized = ControlPlane(
        too_small,
        MemoryControlPlaneStore(),
        FakeTranscriber(TranscriptResult("ok", "en", 1, "small")),
        lambda _: None,
    )
    response = await oversized.handle_voice(message(8, None, voice=True), audio(b"longer-than-one-byte"))
    assert response and "too large" in response.text


def test_settings_load_required_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    configured = ControlPlaneSettings.from_env()
    assert configured.telegram_token == "123:test"
    assert configured.database_url == "postgresql://example"


@pytest.mark.asyncio
async def test_state_change_without_sending_user_is_rejected_safely() -> None:
    control = ControlPlane(
        settings(),
        MemoryControlPlaneStore(),
        FakeTranscriber(TranscriptResult("", None, None, "small")),
        lambda _: None,
    )
    response = await control.handle_text(message(12, "/run group-a", user_id=None, chat_id=-100123))
    assert response and "require a Telegram user identity" in response.text


@pytest.mark.asyncio
async def test_non_operators_can_read_status_but_not_change_state() -> None:
    store = MemoryControlPlaneStore()
    received: list[CommandEnvelope] = []

    async def sink(command: CommandEnvelope) -> None:
        received.append(command)

    control = ControlPlane(settings(), store, FakeTranscriber(TranscriptResult("", None, None, "small")), sink)
    status = await control.handle_text(message(20, "/status", user_id=999))
    assert status and "online" in status.text
    for number, command in enumerate(("/run website https://example.org", "/pause all", "/resume all", "/cancel all", "confirm 0123456789")):
        refused = await control.handle_text(message(21 + number, command, user_id=999))
        assert refused and "Only operators" in refused.text and "user ID is 999" in refused.text
    assert store.confirmations == {} and received == []


@pytest.mark.asyncio
async def test_a_removed_operator_cannot_use_an_already_issued_token() -> None:
    store = MemoryControlPlaneStore()
    transcriber = FakeTranscriber(TranscriptResult("", None, None, "small"))
    prompt = await ControlPlane(settings(), store, transcriber, lambda _: None).handle_text(message(30, "/cancel all"))
    assert prompt and "Confirmation required" in prompt.text
    token = prompt.text.split("confirm ")[1].split()[0]

    demoted = ControlPlaneSettings(telegram_token="123:test", database_url="postgresql://example")
    refused = await ControlPlane(demoted, store, transcriber, lambda _: None).handle_text(message(31, f"confirm {token}"))
    assert refused and "Only operators" in refused.text
    assert token in store.confirmations


def test_operator_ids_are_parsed_strictly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("TELEGRAM_OPERATOR_IDS", "123, 456 789")
    assert ControlPlaneSettings.from_env().operator_user_ids == frozenset({123, 456, 789})
    monkeypatch.setenv("TELEGRAM_OPERATOR_IDS", "")
    assert ControlPlaneSettings.from_env().operator_user_ids == frozenset()
    monkeypatch.setenv("TELEGRAM_OPERATOR_IDS", "@owner")
    with pytest.raises(ValueError, match="numeric"):
        ControlPlaneSettings.from_env()
