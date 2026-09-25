"""Safe command routing, deduplication and confirmation for an open bot.

Anyone may check the bot is online; only allowlisted operators see the
detailed status or may change acquisition state.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from bot.campaign.architect import InvalidGoal, plan_campaign
from bot.control_plane.access import AccessDesk, label
from bot.control_plane.auto import AUTO_COMMANDS, AutoMode, MemorySettingsStore, SettingsStore
from bot.control_plane.intake import IntakeStore, MemoryIntakeStore, TaskIntake, mode_menu
from bot.control_plane.live_view import LiveViewCoordinator, LiveViewUnavailable, is_session_id
from bot.control_plane.models import (
    CommandEnvelope,
    IncomingMessage,
    Reply,
    StatusSnapshot,
    TranscriptionFailure,
)
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import ControlPlaneStore
from bot.control_plane.stt import Transcriber, TranscriptionError
from bot.control_plane.voice_commands import clean_transcript, spoken_command
from bot.operators import ROLES, OperatorSet

log = logging.getLogger(__name__)
CommandSink = Callable[[CommandEnvelope], Awaitable[object]]
AudioDownload = Callable[[], Awaitable[bytes]]
TRANSCRIPTION_ERROR_REPLIES = {
    "timeout": "Transcription timed out. Please send text or a shorter voice message.",
    "too_large": "Voice message is too large to transcribe.",
    "empty_audio": "The voice message was empty.",
    "empty_transcript": "No speech was detected in that voice message.",
}
STATE_CHANGING = frozenset({"run", "pause", "resume", "cancel", "campaign"})
USER_HELP = (
    "Вы пользователь. Как дать задачу:\n"
    "1. Выберите режим (кнопки ниже или /mode).\n"
    "2. Опишите задачу текстом или голосом, например «квартиры в аренду в Мадриде до 1200 €».\n"
    "3. Ответьте на уточняющие вопросы, если бот их задаст.\n"
    "4. Проверьте сводку и нажмите «Запустить» — без этого ничего не запускается.\n\n"
    "Статус: /campaign status. Остановить свою кампанию: /campaign cancel <id>. "
    "Сбросить черновик: напишите «Отмена»."
)


class ControlPlane:
    def __init__(
        self,
        settings: ControlPlaneSettings,
        store: ControlPlaneStore,
        transcriber: Transcriber | None,
        command_sink: CommandSink,
        live: LiveViewCoordinator | None = None,
        access: AccessDesk | None = None,
        settings_store: SettingsStore | None = None,
        intake_store: IntakeStore | None = None,
    ) -> None:
        self.settings, self.store, self.transcriber, self.command_sink = settings, store, transcriber, command_sink
        self.live, self.access = live, access
        # Owners from .env plus operators they approved; shared and live.
        self.operators = access.operators if access else OperatorSet(settings.operator_user_ids)
        self.auto = AutoMode(settings_store or MemorySettingsStore(), default=settings.auto_mode,
                             operators=self.operators, auto_operator_ids=settings.auto_operator_user_ids)
        # Mode choice and task intake (bot/control_plane/intake.py); launching always needs "Запустить".
        self.intake = TaskIntake(intake_store or MemoryIntakeStore(), command_sink, notify_owners=self._tell_owners)

    def _is_operator(self, user_id: int | None) -> bool:
        """Any access at all: helpers, users, operators and owners."""
        return self.operators.has_access(user_id)

    async def _tell_owners(self, text: str) -> None:
        if self.access is None or self.access.notify is None:
            return
        for owner in sorted(self.operators.owners):
            try:
                await self.access.notify(owner, Reply(text))
            except Exception:  # noqa: BLE001 - one unreachable owner must not stop the rest
                log.warning("telegram.control.owner_notice_failed", extra={"owner": owner})

    def _is_user(self, user_id: int | None) -> bool:
        """The ``user`` role: mode, tasks and their own campaigns only."""
        return self.operators.role(user_id) == "user"

    def _may_give_tasks(self, user_id: int | None) -> bool:
        return self._is_user(user_id) or self._can_control(user_id)

    def _can_control(self, user_id: int | None) -> bool:
        """Operators and owners; helpers only handle verification."""
        return self.operators.can_control(user_id)

    def _with_access_button(self, reply: Reply, user_id: int | None) -> Reply:
        if self.access is None or user_id is None or self._is_operator(user_id):
            return reply
        return Reply(reply.text, (*reply.buttons, self.access.button()))

    async def handle_callback(self, user_id: int | None, data: str, display_name: str | None = None, username: str | None = None,
                              chat_id: int | None = None) -> Reply:
        """Inline buttons: ``live:done|cancel:<id>``, ``live:approve:<code>``, ``access:request``,
        ``access:helper|user|operator|deny:<id>``, ``mode:<mode>`` and ``task:<action>[:<value>]``."""
        kind, _, rest = data.partition(":")
        action, _, target = rest.partition(":")
        if kind in {"mode", "task"}:
            if user_id is None or not self._may_give_tasks(user_id):
                return self._with_access_button(Reply("Задачи могут давать только одобренные пользователи."), user_id)
            chat = chat_id if chat_id is not None else user_id
            if kind == "mode":
                return await self.intake.choose_mode(user_id, chat, action)
            who = label(display_name, username, user_id) if self._is_user(user_id) else None
            return await self.intake.on_button(user_id, chat, action, target, who)
        if kind == "access" and self.access is not None:
            if action == "request":
                return await self.access.request(user_id, display_name, username)
            if action in {*ROLES, "deny"}:
                return await self.access.decide(user_id, target, None if action == "deny" else action)
            return Reply("This button is no longer valid.")
        if kind == "live" and action == "approve" and self.live is not None:
            return self.live.approve_browser(target, user_id)
        session_id = target
        if kind != "live" or action not in {"done", "cancel"} or not is_session_id(session_id) or self.live is None:
            return Reply("This button is no longer valid.")
        try:
            return await self.live.finish(session_id, user_id, done=action == "done")
        except LiveViewUnavailable as exc:
            return Reply(f"Could not close the browser: {exc}.")

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
        # Voice costs money per second, so it is limited to operators and
        # approved users; text commands such as /status stay open to everyone.
        if not self._may_give_tasks(message.user_id):
            await self._voice_refused(message, "not_operator")
            identity = f" Your Telegram user ID is {message.user_id}." if message.user_id is not None else ""
            return self._with_access_button(Reply(f"Voice messages are transcribed for operators only; please send text instead.{identity}"), message.user_id)
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
            if not self._is_user(message.user_id) and (refusal := self._operator_refusal(message)) is not None:
                return refusal
            token = text.split(maxsplit=1)[1].strip()
            confirmed = await self.store.consume_confirmation(message, token)
            if confirmed is None:
                return Reply("Confirmation is invalid, expired, or belongs to another operator.")
            command, arguments, confirmation_id = confirmed
            if not self._can_control(message.user_id) and not (command == "campaign" and _is_cancel(arguments)):
                # A user only ever confirms stopping a campaign; the Orchestra checks it is theirs.
                return self._operator_refusal(message) or Reply("Confirmation is invalid.")
            receipt = await self.command_sink(CommandEnvelope(command, arguments, message.chat_id, message.user_id or 0, message.message_id, confirmation_id))
            command_id = getattr(receipt, "command_id", None)
            suffix = f" Queue id: {command_id}." if command_id else ""
            return Reply(f"Confirmed: /{command}. Safely queued for bounded orchestration.{suffix}")
        if not text.startswith("/"):
            if text and await self.auto.applies_to(message.user_id):
                return await self._auto_goal(message, text)
            if text and (self._is_user(message.user_id) or (self._can_control(message.user_id) and await self.intake.mode(message.user_id))):
                return await self.intake.on_text(message, text)
            return Reply("Send /help for control-plane commands. State-changing commands require confirmation.")
        command_line = text[1:].split(maxsplit=1)
        command = command_line[0].split("@", 1)[0].lower()
        arguments = command_line[1] if len(command_line) > 1 else ""
        if command == "mode":
            if not self._may_give_tasks(message.user_id):
                return self._with_access_button(Reply("Режим доступен после одобрения доступа."), message.user_id)
            return mode_menu()
        if command in {"help", "start"}:
            role = self.operators.role(message.user_id)
            if role == "user":
                return mode_menu(USER_HELP) if command == "start" or not await self.intake.mode(message.user_id) else Reply(USER_HELP)
            if role == "helper":
                text = ("You are a helper: when a Facebook login, CAPTCHA or checkpoint needs a person, you get a message "
                        "with a button to open the browser. You can also send /login [facebook|instagram|tiktok] [profile-name].")
            else:
                text = ("Commands: /status, /run <scope>, /pause <scope>, /resume <scope>, /cancel <scope>, "
                        "/campaign <goal> | status | cancel <id>, "
                        "/login [facebook|instagram|tiktok] [profile-name]. Confirm changes with: confirm <token>.")
                if role == "owner":
                    text += " Owners: /operators, /role <ID> helper|user|operator, /revoke <ID>, /auto on|off|status."
                if self.auto.eligible(message.user_id):
                    text += ("\n\nAuto mode (when an owner turns it on): just write or say the goal, e.g. "
                             "«квартиры в аренду в Мадриде»; /campaign, /run, /pause and /resume are queued "
                             "without confirmation, /cancel still asks.")
                elif role is None:
                    text += "\n\nYou have no access yet; press the button to ask for it."
                if command == "start" and self._can_control(message.user_id):
                    text += "\n\nИли выберите режим и опишите задачу: бот уточнит детали и попросит подтвердить «Запустить»."
                    return mode_menu(text)
            return self._with_access_button(Reply(text), message.user_id)
        if command == "role":
            return await self.access.set_role(message.user_id, arguments) if self.access else Reply("Unknown command. Send /help.")
        if command == "operators":
            return await self.access.list(message.user_id) if self.access else Reply("Unknown command. Send /help.")
        if command == "revoke":
            return await self.access.revoke(message.user_id, arguments) if self.access else Reply("Unknown command. Send /help.")
        if command == "login":
            if self._is_user(message.user_id):  # users never open the logged-in browser
                return self._operator_refusal(message) or Reply("Only operators can do that.")
            if self.live is None:
                return Reply("The live browser is not available in this service.")
            if message.chat_id != message.user_id:
                # Telegram shows Mini App buttons in private chats only.
                return Reply("Send /login in a private chat with the bot.")
            parts = arguments.split()
            platform = parts[0].lower() if parts else "facebook"
            name = parts[1].lower() if len(parts) > 1 else f"{platform}-main"
            return await self.live.login(message.user_id, platform, name)
        if command == "status":
            return await self._status(message)
        if command == "auto":
            return await self.auto.command(message.user_id, arguments)
        if command not in STATE_CHANGING:
            return Reply("Unknown command. Send /help.")
        # Telegram channel posts may not have a sending user. Confirmation
        # tokens must stay bound to a concrete Telegram identity.
        if message.user_id is None:
            return Reply("State-changing commands require a Telegram user identity.")
        if self._is_user(message.user_id):
            return await self._user_campaign(message, command, arguments)
        if (refusal := self._operator_refusal(message)) is not None:
            return refusal
        if command == "campaign":
            if not arguments.strip():
                return Reply("Use /campaign <what and where to search>, /campaign status, or /campaign cancel <id>.")
            if arguments.strip().lower() == "status":
                # Read-only: no confirmation; the Orchestra answers in this chat.
                await self.command_sink(CommandEnvelope(command, "status", message.chat_id, message.user_id, message.message_id))
                return Reply("Campaign status requested.")
        cancelling = command == "campaign" and arguments.split(maxsplit=1)[0].lower() == "cancel"
        if command in AUTO_COMMANDS and not cancelling and await self.auto.applies_to(message.user_id):
            return await self._auto_queue(message, command, arguments)
        token = await self.store.create_confirmation(message, command, arguments, ttl_seconds=self.settings.confirmation_ttl_seconds)
        return Reply(f"Confirmation required for /{command}. Reply exactly: confirm {token} (expires in {self.settings.confirmation_ttl_seconds // 60} minutes).")

    async def _user_campaign(self, message: IncomingMessage, command: str, arguments: str) -> Reply:
        """Users: /campaign status, /campaign cancel <id> (confirmed), and a goal goes through intake."""
        assert message.user_id is not None
        if command != "campaign":
            return self._operator_refusal(message) or Reply("Only operators can do that.")
        goal = arguments.strip()
        if not goal:
            return Reply("Используйте /campaign status, /campaign cancel <id> или просто опишите задачу.")
        if goal.lower() == "status":
            await self.command_sink(CommandEnvelope(command, "status", message.chat_id, message.user_id, message.message_id))
            return Reply("Статус кампании запрошен.")
        if _is_cancel(goal):
            token = await self.store.create_confirmation(message, command, goal, ttl_seconds=self.settings.confirmation_ttl_seconds)
            return Reply(f"Остановить кампанию? Ответьте точно: confirm {token} (действует {self.settings.confirmation_ttl_seconds // 60} мин.).")
        return await self.intake.on_text(message, goal)

    async def _auto_goal(self, message: IncomingMessage, text: str) -> Reply:
        """Plain text (or speech) from an auto-operator is a campaign goal."""
        if text.split(maxsplit=1)[0].lower() in {"status", "cancel"}:
            return Reply("Для статуса или отмены используйте /campaign status или /campaign cancel <id>.")
        try:
            plan_campaign(text)  # the Orchestra plans again; this only avoids queuing nonsense
        except InvalidGoal as exc:
            return Reply(f"{exc}\nНапишите цель одной фразой, например «квартиры в аренду в Мадриде», или отправьте /help.")
        return await self._auto_queue(message, "campaign", text)

    async def _auto_queue(self, message: IncomingMessage, command: str, arguments: str) -> Reply:
        """Straight to the Orchestra, which still applies quotas and breakers."""
        assert message.user_id is not None
        receipt = await self.command_sink(CommandEnvelope(command, arguments, message.chat_id, message.user_id, message.message_id, auto=True))
        command_id = getattr(receipt, "command_id", None)
        log.info("telegram.control.auto_queued", extra={"command": command, "chat_id": message.chat_id, "user_id": message.user_id, "command_id": command_id})
        suffix = f" Queue id: {command_id}." if command_id else ""
        return Reply(f"Авто: /{command} поставлена в очередь без подтверждения.{suffix}")

    async def _status(self, message: IncomingMessage) -> Reply:
        # /status is open to everyone, but queue sizes, sources and spend are
        # operational detail: only operators see them.
        if not self._can_control(message.user_id):
            return self._with_access_button(Reply("Control plane is online. Detailed status is shown to operators only."), message.user_id)
        try:
            snapshot = await self.store.status_snapshot()
        except Exception as exc:  # noqa: BLE001 - a status read must never break the bot
            log.warning("telegram.control.status_failed", extra={"error": type(exc).__name__})
            return Reply("Control plane is online, but the status query failed. Check the telegram service logs.")
        return Reply(format_status(snapshot))

    def _operator_refusal(self, message: IncomingMessage) -> Reply | None:
        if self._can_control(message.user_id):
            return None
        log.warning("telegram.control.not_operator", extra={"chat_id": message.chat_id, "user_id": message.user_id})
        identity = f" Your Telegram user ID is {message.user_id}." if message.user_id is not None else ""
        return self._with_access_button(
            Reply(f"Only operators can run, pause, resume, or cancel acquisition; /status and /help are open to everyone.{identity}"),
            message.user_id,
        )


def _is_cancel(arguments: str) -> bool:
    return arguments.split(maxsplit=1)[0].lower() == "cancel" if arguments.strip() else False


def format_status(s: StatusSnapshot) -> str:
    last = s.last_batch_finished_at.strftime("%Y-%m-%d %H:%M UTC") if s.last_batch_finished_at else "never"
    cost = f"${s.voice_cost_usd_30d:.6f}" if s.voice_cost_usd_30d is not None else "$0"
    attention = s.batches_need_verification + s.runs_need_verification + s.sources_need_verification + s.profiles_need_attention + s.verification_jobs_open
    lines = [
        "Control plane is online.",
        f"Commands: {s.commands_queued} queued, {s.commands_running} running.",
        f"Batches: {s.batches_active} active, {s.batches_need_verification} need verification; last finished {last}.",
        f"Runs: {s.runs_running} running, {s.runs_need_verification} awaiting verification.",
        f"Sources: {s.sources_active} active, {s.sources_paused} paused, {s.sources_need_verification} need verification.",
        f"Browser profiles: {s.profiles_ready} ready, {s.profiles_in_use} in use, {s.profiles_need_attention} need attention.",
        f"Posts collected (24h): {s.posts_last_24h}.",
        f"Voice (30 days): {s.voice_notes_30d} notes, {cost}.",
    ]
    if attention:
        lines.append(f"Needs a human: {attention} item(s) waiting for verification or attention.")
    return "\n".join(lines)
