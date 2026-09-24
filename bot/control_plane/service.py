"""Safe command routing, deduplication and confirmation for an open bot.

Anyone may read status; only allowlisted operators may change acquisition state.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from bot.control_plane.models import CommandEnvelope, IncomingMessage, Reply, TranscriptionFailure
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import ControlPlaneStore
from bot.control_plane.stt import Transcriber, TranscriptionError
from bot.control_plane.voice_commands import clean_transcript, spoken_command

log = logging.getLogger(__name__)
CommandSink = Callable[[CommandEnvelope], Awaitable[object]]
AudioDownload = Callable[[], Awaitable[bytes]]
TRANSCRIPTION_ERROR_REPLIES = {
    "timeout": "Transcription timed out. Please send text or a shorter voice message.",
    "too_large": "Voice message is too large to transcribe.",
    "empty_audio": "The voice message was empty.",
    "empty_transcript": "No speech was detected in that voice message.",
}
STATE_CHANGING = frozenset({"run", "pause", "resume", "cancel"})


class ControlPlane:
    def __init__(self, settings: ControlPlaneSettings, store: ControlPlaneStore, transcriber: Transcriber | None, command_sink: CommandSink) -> None:
        self.settings, self.store, self.transcriber, self.command_sink = settings, store, transcriber, command_sink

    async def handle_text(self, message: IncomingMessage) -> Reply | None:
        if not await self.store.claim_message(message):
            return Reply("Duplicate update ignored.")
        return await self._handle_command(message, message.text or "")

    async def handle_voice(self, message: IncomingMessage, download: AudioDownload) -> Reply | None:
        """Transcribe one voice note with a single, bounded, paid provider call.

        Every check that can refuse the message runs before the download and
        before any provider request: duplicates, non-operators, missing
        configuration and Telegram's declared size/duration never cost money.
        """
        if not await self.store.claim_message(message):
            return Reply("Duplicate update ignored.")
        # Voice costs money per second, so it is limited to operators; text
        # commands such as /status stay open to everyone.
        if message.user_id is None or message.user_id not in self.settings.operator_user_ids:
            await self._voice_refused(message, "not_operator")
            identity = f" Your Telegram user ID is {message.user_id}." if message.user_id is not None else ""
            return Reply(f"Voice messages are transcribed for operators only; please send text instead.{identity}")
        if self.transcriber is None:
            await self._voice_refused(message, "stt_not_configured")
            return Reply("Voice transcription is not configured. Please send text.")
        max_bytes, max_seconds = self.settings.stt_max_audio_bytes, self.settings.stt_max_audio_seconds
        if (message.voice_size or 0) > max_bytes:
            await self._voice_refused(message, "too_large")
            return Reply(f"Voice message is too large; maximum is {max_bytes / 1_048_576:g} MB.")
        if (message.voice_duration_seconds or 0) > max_seconds:
            await self._voice_refused(message, "too_long")
            return Reply(f"Voice message is too long; maximum is {max_seconds} seconds.")
        try:
            audio = await download()
        except Exception as exc:  # noqa: BLE001 - any download failure ends this message
            log.warning("telegram.control.voice_download_failed", extra={"error": type(exc).__name__})
            await self._voice_refused(message, "download_failed")
            return Reply("I could not download that voice message from Telegram. Please try again or send text.")
        if len(audio) > max_bytes:
            await self._voice_refused(message, "too_large")
            return Reply(f"Voice message is too large; maximum is {max_bytes / 1_048_576:g} MB.")
        try:
            transcript = await self.transcriber.transcribe(audio, filename="voice.ogg")
        except TranscriptionError as exc:
            log.warning("telegram.control.transcription_failed", extra={"error_code": exc.code, "status": exc.status})
            await self._voice_refused(message, exc.code, status=exc.status)
            detail = TRANSCRIPTION_ERROR_REPLIES.get(exc.code, "The transcription service returned an error.")
            return Reply(f"I could not transcribe that voice message. {detail} It was not retried; please send text or try again.")
        await self.store.save_transcript(message, transcript)
        log.info(
            "telegram.control.transcribed",
            extra={"model": transcript.model, "language": transcript.language, "cost_usd": transcript.cost_usd, "audio_seconds": transcript.audio_seconds},
        )
        confidence = f", confidence {transcript.confidence:.0%}" if transcript.confidence is not None else ""
        # The stored transcript stays exactly as returned; only the reply and
        # the command see the cleaned text.
        text = clean_transcript(transcript.text)
        command = spoken_command(text)
        command_reply = await self._handle_command(message, command or text)
        heard = f"\nUnderstood as: {command}" if command else ""
        return Reply(f"Transcript ({transcript.language or 'unknown'}{confidence}):\n{text}{heard}\n\n{command_reply.text}")

    async def _voice_refused(self, message: IncomingMessage, code: str, *, status: int | None = None) -> None:
        transcriber = self.transcriber
        await self.store.record_transcription_failure(
            message,
            TranscriptionFailure(code, transcriber.model if transcriber else None, transcriber.provider if transcriber else None, status),
        )

    async def _handle_command(self, message: IncomingMessage, raw: str) -> Reply:
        text = raw.strip()
        if text.lower().startswith("confirm "):
            # Re-checked at confirmation so removing an operator also voids
            # the tokens they were already issued.
            if (refusal := self._operator_refusal(message)) is not None:
                return refusal
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
        if (refusal := self._operator_refusal(message)) is not None:
            return refusal
        token = await self.store.create_confirmation(message, command, arguments, ttl_seconds=self.settings.confirmation_ttl_seconds)
        return Reply(f"Confirmation required for /{command}. Reply exactly: confirm {token} (expires in {self.settings.confirmation_ttl_seconds // 60} minutes).")

    def _operator_refusal(self, message: IncomingMessage) -> Reply | None:
        if message.user_id is not None and message.user_id in self.settings.operator_user_ids:
            return None
        log.warning("telegram.control.not_operator", extra={"chat_id": message.chat_id, "user_id": message.user_id})
        identity = f" Your Telegram user ID is {message.user_id}." if message.user_id is not None else ""
        return Reply(f"Only operators can run, pause, resume, or cancel acquisition; /status and /help are open to everyone.{identity}")
