"""OpenRouter voice transcription: one bounded request, audited, fail-safe.

No test here reaches the network: the OpenRouter client runs over an
``httpx.MockTransport`` that records every request it would have sent.
"""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import httpx
import pytest

from bot.control_plane.models import IncomingMessage
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore
from bot.control_plane.stt import OpenRouterTranscriber, TranscriptionError

OGG = b"OggS\x00\x02" + b"\x01" * 64
MODEL = "openai/whisper-large-v3-turbo"


class Recorder:
    """A mock OpenRouter that answers with ``handler`` and counts requests."""

    def __init__(self, handler) -> None:
        self.requests: list[httpx.Request] = []
        self.handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def ok(text: str, language: str | None, cost: float = 0.000045, seconds: float = 15.0):
    def handler(_: httpx.Request) -> httpx.Response:
        body = {"text": text, "usage": {"seconds": seconds, "cost": cost}}
        if language:
            body["language"] = language
        return httpx.Response(200, json=body)

    return handler


def transcriber(recorder: Recorder, *, max_audio_bytes: int = 1_000_000) -> OpenRouterTranscriber:
    return OpenRouterTranscriber(
        api_key="sk-or-test",
        model=MODEL,
        timeout_seconds=5,
        max_audio_bytes=max_audio_bytes,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )


def settings(**changes: object) -> ControlPlaneSettings:
    base = ControlPlaneSettings(
        telegram_token="123:test",
        database_url="postgresql://example",
        operator_user_ids=frozenset({11}),
        stt_max_audio_bytes=1_000_000,
        stt_max_audio_seconds=120,
    )
    return replace(base, **changes)


def voice(message_id: int = 1, *, user_id: int | None = 11, size: int | None = len(OGG), duration: int | None = 5) -> IncomingMessage:
    return IncomingMessage(
        chat_id=22,
        user_id=user_id,
        message_id=message_id,
        voice_file_id="file-id",
        voice_size=size,
        voice_duration_seconds=duration,
    )


class Download:
    def __init__(self, payload: bytes = OGG) -> None:
        self.payload, self.calls = payload, 0

    async def __call__(self) -> bytes:
        self.calls += 1
        return self.payload


# --- the OpenRouter client ---------------------------------------------------


@pytest.mark.asyncio
async def test_ogg_is_sent_once_as_base64_and_cost_is_parsed() -> None:
    recorder = Recorder(ok("/status", "english", cost=0.000045, seconds=15))
    result = await transcriber(recorder).transcribe(OGG, filename="voice.ogg")

    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert request.headers["Authorization"] == "Bearer sk-or-test"
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["input_audio"]["format"] == "ogg"
    assert base64.b64decode(body["input_audio"]["data"]) == OGG
    assert result.text == "/status"
    assert result.language == "english"
    assert result.cost_usd == pytest.approx(0.000045)
    assert result.audio_seconds == 15
    assert result.request_status == 200
    assert result.model == MODEL and result.provider == "openrouter"


@pytest.mark.asyncio
async def test_missing_usage_is_stored_as_unknown_not_zero() -> None:
    recorder = Recorder(lambda _: httpx.Response(200, json={"text": "hola"}))
    result = await transcriber(recorder).transcribe(OGG, filename="voice.ogg")
    assert result.cost_usd is None and result.audio_seconds is None and result.language is None


@pytest.mark.asyncio
async def test_timeout_is_a_single_attempt() -> None:
    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    recorder = Recorder(hang)
    with pytest.raises(TranscriptionError) as caught:
        await transcriber(recorder).transcribe(OGG, filename="voice.ogg")
    assert caught.value.code == "timeout"
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 402, 429, 500, 503])
async def test_http_errors_are_not_retried(status: int) -> None:
    recorder = Recorder(lambda _: httpx.Response(status, json={"error": {"message": "nope"}}))
    with pytest.raises(TranscriptionError) as caught:
        await transcriber(recorder).transcribe(OGG, filename="voice.ogg")
    assert caught.value.code == "http_error" and caught.value.status == status
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_malformed_and_empty_responses_fail_safely() -> None:
    for handler, code in (
        (lambda _: httpx.Response(200, content=b"<html>"), "bad_response"),
        (lambda _: httpx.Response(200, json=["text"]), "bad_response"),
        (lambda _: httpx.Response(200, json={"text": "   "}), "empty_transcript"),
    ):
        with pytest.raises(TranscriptionError) as caught:
            await transcriber(Recorder(handler)).transcribe(OGG, filename="voice.ogg")
        assert caught.value.code == code


@pytest.mark.asyncio
async def test_client_refuses_oversized_audio_without_a_request() -> None:
    recorder = Recorder(ok("x", "en"))
    with pytest.raises(TranscriptionError) as caught:
        await transcriber(recorder, max_audio_bytes=10).transcribe(OGG, filename="voice.ogg")
    assert caught.value.code == "too_large" and recorder.requests == []


def test_api_key_is_required_and_never_in_settings_repr() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterTranscriber(api_key="", model=MODEL, timeout_seconds=5, max_audio_bytes=10)
    assert "sk-or-secret" not in repr(settings(openrouter_api_key="sk-or-secret"))


# --- the Telegram voice path ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Покажи статус", "ukrainian"),
        ("Покажи статус, пожалуйста", "russian"),
        ("Muéstrame el estado, por favor", "spanish"),
        ("Show me the status", "english"),
    ],
)
async def test_multilingual_transcripts_are_stored_with_language_and_cost(text: str, language: str) -> None:
    store = MemoryControlPlaneStore()
    recorder = Recorder(ok(text, language, cost=0.00003, seconds=10))
    control = ControlPlane(settings(), store, transcriber(recorder), lambda _: None)

    reply = await control.handle_voice(voice(), Download())

    assert reply and f"Transcript ({language}):\n{text}" in reply.text
    saved = store.transcripts[(22, 1)]
    assert saved.text == text and saved.language == language
    assert saved.cost_usd == pytest.approx(0.00003) and saved.model == MODEL
    assert saved.request_status == 200
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_transcribed_command_still_needs_confirmation() -> None:
    received: list[object] = []

    async def sink(command: object) -> None:
        received.append(command)

    control = ControlPlane(settings(), MemoryControlPlaneStore(), transcriber(Recorder(ok("/run all", "en"))), sink)
    reply = await control.handle_voice(voice(), Download())
    assert reply and "Confirmation required for /run" in reply.text
    assert received == []


@pytest.mark.asyncio
async def test_duplicate_voice_update_makes_no_second_call() -> None:
    recorder = Recorder(ok("/status", "en"))
    download = Download()
    control = ControlPlane(settings(), MemoryControlPlaneStore(), transcriber(recorder), lambda _: None)

    first = await control.handle_voice(voice(5), download)
    second = await control.handle_voice(voice(5), download)

    assert first and "Transcript" in first.text
    assert second and second.text == "Duplicate update ignored."
    assert len(recorder.requests) == 1 and download.calls == 1


@pytest.mark.asyncio
async def test_an_already_processed_text_message_id_is_not_transcribed() -> None:
    recorder = Recorder(ok("/status", "en"))
    control = ControlPlane(settings(), MemoryControlPlaneStore(), transcriber(recorder), lambda _: None)
    await control.handle_text(IncomingMessage(chat_id=22, user_id=11, message_id=9, text="/status"))
    reply = await control.handle_voice(voice(9), Download())
    assert reply and reply.text == "Duplicate update ignored."
    assert recorder.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [999, None])
async def test_unauthorized_voice_is_neither_downloaded_nor_sent(user_id: int | None) -> None:
    store = MemoryControlPlaneStore()
    recorder = Recorder(ok("/status", "en"))
    download = Download()
    control = ControlPlane(settings(), store, transcriber(recorder), lambda _: None)

    reply = await control.handle_voice(voice(user_id=user_id), download)

    assert reply and "operators only" in reply.text
    assert recorder.requests == [] and download.calls == 0
    assert store.failures[(22, 1)].error_code == "not_operator"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "payload", "code", "phrase"),
    [
        (voice(size=2_000_000), OGG, "too_large", "too large"),
        (voice(duration=121), OGG, "too_long", "too long"),
        # Telegram's declared size can be absent or wrong; the bytes decide.
        (voice(size=None), b"x" * 1_000_001, "too_large", "too large"),
    ],
)
async def test_over_limit_voice_is_refused_before_openrouter(message: IncomingMessage, payload: bytes, code: str, phrase: str) -> None:
    store = MemoryControlPlaneStore()
    recorder = Recorder(ok("/status", "en"))
    control = ControlPlane(settings(), store, transcriber(recorder, max_audio_bytes=10_000_000), lambda _: None)

    reply = await control.handle_voice(message, Download(payload))

    assert reply and phrase in reply.text
    assert recorder.requests == []
    assert store.failures[(22, 1)].error_code == code


@pytest.mark.asyncio
async def test_openrouter_failure_is_reported_and_audited_once() -> None:
    store = MemoryControlPlaneStore()
    recorder = Recorder(lambda _: httpx.Response(502, json={"error": "upstream"}))
    control = ControlPlane(settings(), store, transcriber(recorder), lambda _: None)

    reply = await control.handle_voice(voice(), Download())

    assert reply and "could not transcribe" in reply.text and "not retried" in reply.text
    failure = store.failures[(22, 1)]
    assert (failure.error_code, failure.request_status, failure.model, failure.provider) == ("http_error", 502, MODEL, "openrouter")
    assert (22, 1) not in store.transcripts
    assert len(recorder.requests) == 1


@pytest.mark.asyncio
async def test_timeout_reaches_telegram_as_a_clear_message() -> None:
    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    store = MemoryControlPlaneStore()
    control = ControlPlane(settings(), store, transcriber(Recorder(hang)), lambda _: None)
    reply = await control.handle_voice(voice(), Download())
    assert reply and "timed out" in reply.text
    assert store.failures[(22, 1)].error_code == "timeout"


@pytest.mark.asyncio
async def test_download_failure_is_reported_without_a_provider_call() -> None:
    async def broken() -> bytes:
        raise TimeoutError

    store = MemoryControlPlaneStore()
    recorder = Recorder(ok("/status", "en"))
    control = ControlPlane(settings(), store, transcriber(recorder), lambda _: None)
    reply = await control.handle_voice(voice(), broken)
    assert reply and "could not download" in reply.text
    assert recorder.requests == [] and store.failures[(22, 1)].error_code == "download_failed"


@pytest.mark.asyncio
async def test_missing_key_refuses_voice_but_keeps_text_working() -> None:
    store = MemoryControlPlaneStore()
    control = ControlPlane(settings(), store, None, lambda _: None)
    reply = await control.handle_voice(voice(), Download())
    assert reply and "not configured" in reply.text
    status = await control.handle_text(IncomingMessage(chat_id=22, user_id=11, message_id=2, text="/status"))
    assert status and "online" in status.text


# --- configuration -----------------------------------------------------------


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:test")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    for name in ("STT_PROVIDER", "STT_MODEL", "STT_MAX_AUDIO_BYTES", "STT_MAX_AUDIO_SECONDS", "STT_TIMEOUT_SECONDS", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_stt_settings_read_the_documented_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("STT_PROVIDER", "openrouter")
    monkeypatch.setenv("STT_MODEL", MODEL)
    monkeypatch.setenv("STT_MAX_AUDIO_BYTES", "20971520")
    monkeypatch.setenv("STT_TIMEOUT_SECONDS", "60")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    configured = ControlPlaneSettings.from_env()
    assert (configured.stt_provider, configured.stt_model) == ("openrouter", MODEL)
    assert configured.stt_max_audio_bytes == 20_971_520
    assert configured.stt_timeout_seconds == 60
    assert configured.openrouter_api_key == "sk-or-test"


def test_defaults_apply_without_any_stt_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    configured = ControlPlaneSettings.from_env()
    assert configured.stt_model == MODEL and configured.openrouter_api_key == ""


@pytest.mark.parametrize(
    ("name", "value", "match"),
    [
        ("STT_PROVIDER", "faster_whisper", "STT_PROVIDER"),
        ("STT_MAX_AUDIO_BYTES", "999999999", "between"),
        ("STT_MAX_AUDIO_BYTES", "20MB", "integer"),
        ("STT_TIMEOUT_SECONDS", "0", "between"),
        ("STT_TIMEOUT_SECONDS", "3600", "between"),
    ],
)
def test_out_of_bounds_stt_configuration_stops_startup(monkeypatch: pytest.MonkeyPatch, name: str, value: str, match: str) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=match):
        ControlPlaneSettings.from_env()
