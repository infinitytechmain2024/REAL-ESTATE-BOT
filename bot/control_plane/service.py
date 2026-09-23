"""Safe command routing, deduplication and confirmation for an open bot."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from bot.control_plane.models import CommandEnvelope, IncomingMessage, Reply
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import ControlPlaneStore
from bot.control_plane.stt import Transcriber

log = logging.getLogger(__name__)
CommandSink = Callable[[CommandEnvelope], Awaitable[object]]
STATE_CHANGING = frozenset({"run", "pause", "resume", "cancel"})


class ControlPlane:
    def __init__(self, settings: ControlPlaneSettings, store: ControlPlaneStore, transcriber: Transcriber, command_sink: CommandSink) -> None:
        self.settings, self.store, self.transcriber, self.command_sink = settings, store, transcriber, command_sink

    async def handle_text(self, message: IncomingMessage) -> Reply | None:
        if not await self.store.claim_message(message):
            return Reply("Duplicate update ignored.")
        return await self._handle_command(message, message.text or "")

    async def handle_voice(self, message: IncomingMessage, audio: bytes) -> Reply | None:
        if not await self.store.claim_message(message):
            return Reply("Duplicate update ignored.")
        if len(audio) > self.settings.max_voice_mb * 1_048_576:
            return Reply(f"Voice message is too large; maximum is {self.settings.max_voice_mb:g} MB.")
        try:
            transcript = await self.transcriber.transcribe(audio, filename="voice.ogg")
        except (RuntimeError, ValueError) as exc:
            log.warning("telegram.control.transcription_failed", extra={"error": str(exc)})
            return Reply("I could not transcribe that voice message. Please send text or try again.")
        await self.store.save_transcript(message, transcript)
        confidence = f"{transcript.confidence:.0%}" if transcript.confidence is not None else "n/a"
        command_reply = await self._handle_command(message, transcript.text)
        return Reply(f"Transcript ({transcript.language or 'unknown'}, confidence {confidence}):\n{transcript.text}\n\n{command_reply.text}")

    async def _handle_command(self, message: IncomingMessage, raw: str) -> Reply:
        text = raw.strip()
        if text.lower().startswith("confirm "):
            token = text.split(maxsplit=1)[1].strip()
            confirmed = await self.store.consume_confirmation(message, token)
            if confirmed is None:
                return Reply("Confirmation is invalid, expired, or belongs to another operator.")
            command, arguments, confirmation_id = confirmed
            receipt = await self.command_sink(CommandEnvelope(command, arguments, message.chat_id, message.user_id or 0, message.message_id, confirmation_id))
            command_id = getattr(receipt, "command_id", None)
            suffix = f" Queue id: {command_id}." if command_id else ""
            return Reply(f"Confirmed: /{command}. Safely queued for bounded orchestration.{suffix}")
        if not text.startswith("/"):
            return Reply("Send /help for control-plane commands. State-changing commands require confirmation.")
        command_line = text[1:].split(maxsplit=1)
        command = command_line[0].split("@", 1)[0].lower()
        arguments = command_line[1] if len(command_line) > 1 else ""
        if command in {"help", "start"}:
            return Reply("Commands: /status, /run <scope>, /pause <scope>, /resume <scope>, /cancel <scope>. Confirm changes with: confirm <token>.")
        if command == "status":
            return Reply("Control plane is online. Acquisition is not implemented in this service.")
        if command not in STATE_CHANGING:
            return Reply("Unknown command. Send /help.")
        # Telegram channel posts may not have a sending user. Confirmation
        # tokens must stay bound to a concrete Telegram identity.
        if message.user_id is None:
            return Reply("State-changing commands require a Telegram user identity.")
        token = await self.store.create_confirmation(message, command, arguments, ttl_seconds=self.settings.confirmation_ttl_seconds)
        return Reply(f"Confirmation required for /{command}. Reply exactly: confirm {token} (expires in {self.settings.confirmation_ttl_seconds // 60} minutes).")
