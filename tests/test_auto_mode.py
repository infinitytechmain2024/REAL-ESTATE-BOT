"""Auto mode: who may skip confirmation, for which commands, and the /auto switch."""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from bot.control_plane.access import AccessDesk, MemoryAccessStore
from bot.control_plane.auto import MemorySettingsStore
from bot.control_plane.models import CommandEnvelope, IncomingMessage, TranscriptResult
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore
from bot.operators import OperatorSet

OWNER, LISTED, UNLISTED, HELPER, STRANGER = 11, 21, 22, 31, 99
GOAL = "квартиры в аренду в Мадриде"
_ids = iter(range(1, 100_000))


@dataclass
class Receipt:
    command_id: str


class Sink:
    def __init__(self) -> None:
        self.envelopes: list[CommandEnvelope] = []

    async def __call__(self, envelope: CommandEnvelope) -> Receipt:
        self.envelopes.append(envelope)
        return Receipt(f"cmd-{len(self.envelopes)}")


class FailingSettings(MemorySettingsStore):
    async def get(self, key: str):
        raise ConnectionError("database down")


class Transcriber:
    model, provider = "openai/whisper-large-v3-turbo", "openrouter"

    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, audio: bytes, *, filename: str) -> TranscriptResult:
        return TranscriptResult(self.text, "ru", 0.9, self.model)


def plane(*, auto: bool = True, store: MemorySettingsStore | None = None, transcript: str = "") -> tuple[ControlPlane, Sink]:
    sink = Sink()
    # Helpers and strangers listed as auto-operators must still never be eligible.
    operators = OperatorSet({OWNER}, {LISTED: "operator", UNLISTED: "operator", HELPER: "helper"})
    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x", operator_user_ids=frozenset({OWNER}),
                                    auto_mode=auto, auto_operator_user_ids=frozenset({LISTED, HELPER, STRANGER}))
    desk = AccessDesk(MemoryAccessStore(), operators)
    control = ControlPlane(settings, MemoryControlPlaneStore(), Transcriber(transcript), sink, access=desk,
                           settings_store=store if store is not None else MemorySettingsStore())
    return control, sink


def text(user: int, body: str) -> IncomingMessage:
    return IncomingMessage(chat_id=user, user_id=user, message_id=next(_ids), text=body)


def voice(user: int) -> IncomingMessage:
    return IncomingMessage(chat_id=user, user_id=user, message_id=next(_ids), voice_file_id="v", voice_size=100, voice_duration_seconds=3)


async def download() -> bytes:
    return b"OggS"


async def say(control: ControlPlane, user: int, body: str) -> str:
    reply = await control.handle_text(text(user, body))
    assert reply is not None
    return reply.text


AUTO_COMMANDS = [(f"/campaign {GOAL}", "campaign", GOAL), ("/run website https://example.org", "run", "website https://example.org"),
                 ("/pause all", "pause", "all"), ("/resume all", "resume", "all")]


@pytest.mark.asyncio
@pytest.mark.parametrize("user", [OWNER, LISTED])
async def test_auto_operators_skip_confirmation_for_campaign_run_pause_resume(user: int) -> None:
    control, sink = plane()
    for body, command, arguments in AUTO_COMMANDS:
        reply = await say(control, user, body)
        assert reply.startswith(f"Авто: /{command} поставлена в очередь") and "Queue id: cmd-" in reply, reply
        envelope = sink.envelopes[-1]
        assert (envelope.command, envelope.arguments, envelope.user_id, envelope.confirmation_id, envelope.auto) == (command, arguments, user, None, True)
    assert len(sink.envelopes) == 4


@pytest.mark.asyncio
async def test_everyone_else_keeps_the_confirm_flow() -> None:
    control, sink = plane()
    for body, _, _ in AUTO_COMMANDS:
        assert "Confirmation required" in await say(control, UNLISTED, body)  # operator, but not listed
        assert "Only operators" in await say(control, HELPER, body)  # listed, but a helper
        assert "Only operators" in await say(control, STRANGER, body)  # listed, but a stranger
    assert sink.envelopes == []
    # Auto mode off (the .env default): owners confirm like everybody else.
    control, sink = plane(auto=False)
    reply = await say(control, OWNER, "/run website https://example.org")
    assert "Confirmation required for /run" in reply
    token = re.search(r"confirm (\S+)", reply).group(1)  # type: ignore[union-attr]
    assert (await say(control, OWNER, f"confirm {token}")).startswith("Confirmed: /run")
    assert sink.envelopes[-1].auto is False


@pytest.mark.asyncio
async def test_cancel_login_and_admin_stay_manual_in_auto_mode() -> None:
    control, sink = plane()
    for body in ("/cancel all", "/cancel batch 0b7a1f5e-7c56-4c3b-9d0e-1a2b3c4d5e6f", "/campaign cancel 0b7a1f5e-7c56-4c3b-9d0e-1a2b3c4d5e6f"):
        assert "Confirmation required" in await say(control, OWNER, body), body
    assert sink.envelopes == []
    assert await say(control, OWNER, "/campaign status") == "Campaign status requested."
    assert sink.envelopes[-1].auto is False
    assert "live browser is not available" in await say(control, OWNER, "/login")
    assert "Only an owner" in await say(control, LISTED, "/revoke 22")
    assert "helper" in await say(control, OWNER, "/operators")


@pytest.mark.asyncio
async def test_auto_switch_is_owner_only_and_survives_a_restart() -> None:
    store = MemorySettingsStore()
    control, sink = plane(auto=False, store=store)
    assert "off (AUTO_MODE default" in await say(control, OWNER, "/auto status")
    for user in (LISTED, UNLISTED, HELPER, STRANGER):
        assert await say(control, user, "/auto on") == "Only an owner can switch auto mode."
    assert store.values == {}
    assert (await say(control, OWNER, "/auto on")).startswith("Auto mode is on")
    assert store.values["auto_mode"][:2] == ("on", OWNER)
    status = await say(control, OWNER, "/auto")
    assert f"on (set by {OWNER} at" in status and "Auto-operators: owners, 21." in status
    assert "Ignored (not an owner or operator): 31, 99." in status
    assert "Use /auto on" in await say(control, OWNER, "/auto maybe")

    restarted, sink = plane(auto=False, store=store)  # the stored switch beats the .env default
    assert (await say(restarted, LISTED, "/pause all")).startswith("Авто:")
    assert (await say(restarted, OWNER, "/auto off")).startswith("Auto mode is off")
    assert "Confirmation required" in await say(restarted, LISTED, "/pause all")
    # An unreadable switch falls back to confirmation.
    broken, sink = plane(auto=True, store=FailingSettings())
    assert "Confirmation required" in await say(broken, OWNER, "/pause all") and sink.envelopes == []


@pytest.mark.asyncio
async def test_free_text_from_an_auto_operator_becomes_a_campaign() -> None:
    control, sink = plane()
    assert (await say(control, LISTED, GOAL)).startswith("Авто: /campaign")
    assert (sink.envelopes[-1].command, sink.envelopes[-1].arguments, sink.envelopes[-1].auto) == ("campaign", GOAL, True)
    unclear = await say(control, LISTED, "hello there")
    assert "Не понял город" in unclear and "например «квартиры в аренду в Мадриде»" in unclear
    assert "/campaign cancel" in await say(control, OWNER, f"cancel {GOAL}")
    assert len(sink.envelopes) == 1
    for user in (UNLISTED, HELPER, STRANGER):
        assert (await say(control, user, GOAL)).startswith("Send /help for control-plane commands")
    off, sink = plane(auto=False)
    assert (await say(off, OWNER, GOAL)).startswith("Send /help for control-plane commands") and sink.envelopes == []


@pytest.mark.asyncio
async def test_voice_goal_and_spoken_command_from_an_auto_operator() -> None:
    control, sink = plane(transcript="Квартиры в аренду в Мадриде.")
    reply = await control.handle_voice(voice(OWNER), download)
    assert reply and "Авто: /campaign поставлена в очередь" in reply.text
    assert sink.envelopes[-1].arguments == "Квартиры в аренду в Мадриде." and sink.envelopes[-1].auto
    control, sink = plane(transcript="Pause everything.")
    reply = await control.handle_voice(voice(LISTED), download)
    assert reply and "Understood as: /pause all" in reply.text and "Авто: /pause" in reply.text
    control, sink = plane(transcript="Какая сегодня погода?")
    reply = await control.handle_voice(voice(OWNER), download)
    assert reply and "Не понял" in reply.text and sink.envelopes == []
    # Voice keeps its own checks: a helper is refused before any transcription.
    reply = await control.handle_voice(voice(HELPER), download)
    assert reply and "operators only" in reply.text


@pytest.mark.asyncio
async def test_help_mentions_auto_mode() -> None:
    control, _ = plane()
    assert "/auto on|off|status" in await say(control, OWNER, "/help")
    listed = await say(control, LISTED, "/help")
    assert "just write or say the goal" in listed and "/auto on" not in listed
    assert "just write or say the goal" not in await say(control, UNLISTED, "/help")


def test_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:x")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("AUTO_MODE", raising=False)
    monkeypatch.delenv("TELEGRAM_AUTO_OPERATOR_IDS", raising=False)
    default = ControlPlaneSettings.from_env()
    assert default.auto_mode is False and default.auto_operator_user_ids == frozenset()
    monkeypatch.setenv("AUTO_MODE", " ON ")
    monkeypatch.setenv("TELEGRAM_AUTO_OPERATOR_IDS", "21, 22")
    loaded = ControlPlaneSettings.from_env()
    assert loaded.auto_mode is True and loaded.auto_operator_user_ids == frozenset({21, 22})
    monkeypatch.setenv("AUTO_MODE", "yes")
    with pytest.raises(ValueError, match="AUTO_MODE must be on or off"):
        ControlPlaneSettings.from_env()
    monkeypatch.setenv("AUTO_MODE", "off")
    monkeypatch.setenv("TELEGRAM_AUTO_OPERATOR_IDS", "@helper")
    with pytest.raises(ValueError, match="TELEGRAM_AUTO_OPERATOR_IDS must be numeric"):
        ControlPlaneSettings.from_env()
