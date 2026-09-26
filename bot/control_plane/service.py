"""Safe command routing, deduplication and confirmation for an open bot.

Anyone may check the bot is online; only allowlisted operators see the
detailed status or may change acquisition state.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from bot.campaign import offers as near
from bot.campaign.architect import InvalidGoal, plan_campaign
from bot.campaign.models import TERMINAL_STATES
from bot.control_plane.access import SETTINGS_BUTTON, AccessDesk, label
from bot.control_plane.auto import AUTO_COMMANDS, AutoMode, MemorySettingsStore, SettingsStore
from bot.control_plane.intake import (
    CANCEL_WORDS,
    STOP_CALLBACK,
    IntakeStore,
    MemoryIntakeStore,
    TaskIntake,
    mode_menu,
)
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
from bot.control_plane.understanding import Understander
from bot.control_plane.voice_commands import clean_transcript, spoken_command
from bot.operators import ROLES, OperatorSet

if TYPE_CHECKING:
    from bot.campaign.store import CampaignStore

log = logging.getLogger(__name__)
CommandSink = Callable[[CommandEnvelope], Awaitable[object]]
AudioDownload = Callable[[], Awaitable[bytes]]
TRANSCRIPTION_ERROR_REPLIES = {
    "timeout": "Transcription timed out. Please send text or a shorter voice message.",
    "too_large": "Voice message is too large to transcribe.",
    "empty_audio": "The voice message was empty.",
    "empty_transcript": "No speech was detected in that voice message.",
}
# Everyone but the owner hears about voice problems in plain Russian, without technical detail.
VOICE_REPLIES_RU = {
    "stt_not_configured": "Голосовые сообщения сейчас недоступны. Напишите задачу текстом.",
    "too_large": "Голосовое сообщение слишком большое. Запишите покороче или напишите текстом.",
    "too_long": "Голосовое сообщение слишком длинное. Запишите покороче или напишите текстом.",
    "download_failed": "Не удалось получить голосовое сообщение. Попробуйте ещё раз или напишите текстом.",
    "empty_audio": "Голосовое сообщение пустое. Попробуйте ещё раз или напишите текстом.",
    "empty_transcript": "Не удалось разобрать речь. Попробуйте ещё раз или напишите текстом.",
    "failed": "Не удалось распознать голосовое сообщение. Попробуйте ещё раз или напишите текстом.",
}
STATE_CHANGING = frozenset({"run", "pause", "resume", "cancel", "campaign"})
GREETING = (
    "Привет! 👋 Я помогу найти недвижимость и инвесторов.\n\n"
    "Как это работает:\n"
    "1️⃣ Выберите режим кнопкой ниже.\n"
    "2️⃣ Опишите, что ищете, текстом или голосом. Например: «квартиры в аренду в Мадриде до 1200 €».\n"
    "3️⃣ Если чего-то не хватает, я задам пару уточняющих вопросов.\n"
    "4️⃣ Проверьте сводку и нажмите «Запустить». Без этого поиск не начнётся.\n\n"
    "Найденные варианты я пришлю сюда. Передумали — напишите «Отмена»."
)
USER_HELP = GREETING
HELPER_GREETING = (
    "Привет! 👋 Вы помощник.\n\n"
    "Когда для входа в Facebook понадобится человек (капча или проверка), я пришлю сообщение "
    "с кнопкой. Откройте по ней браузер и пройдите проверку — больше ничего делать не нужно."
)
GUEST_GREETING = (
    "Привет! 👋 Я бот для поиска недвижимости и инвесторов.\n\n"
    "Чтобы начать, нажмите «Запросить доступ» ниже. Когда заявку одобрят, я пришлю сообщение, "
    "и можно будет давать задачи."
)
# What anyone who may not use a technical command hears instead (no ids, no English).
USER_NOT_AVAILABLE = "Эта команда недоступна. Опишите, что ищете, — я начну поиск."
HELPER_NOT_AVAILABLE = ("Эта команда недоступна. Когда для входа в Facebook понадобится человек, "
                        "я пришлю сообщение с кнопкой.")
NOT_AVAILABLE = "Эта команда недоступна."
VOICE_NO_ACCESS = "Голосовые задачи доступны только после одобрения доступа."
GUEST_NOT_AVAILABLE = "Эта команда недоступна. Чтобы начать, нажмите «Запросить доступ»."
VOICE_GUEST = "Голосовые задачи доступны после одобрения доступа. Нажмите «Запросить доступ»."
VOICE_HELPER = ("Голосовые сообщения недоступны. Когда для входа в Facebook понадобится человек, "
                "я пришлю сообщение с кнопкой.")
STALE_BUTTON = "Эта кнопка устарела."
# Stopping one's own search without an id: «стоп» (or «Отмена» with no task being written).
STOP_WORDS = frozenset({"стоп", "stop", "остановить", "остановить поиск", "останови поиск", "стоп поиск", "хватит"})
SEARCH_STOPPED = "Поиск остановлен."
NO_ACTIVE_SEARCH = "Сейчас нет активного поиска."
STOP_FAILED = "Не получилось остановить поиск. Попробуйте ещё раз."


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
        offers: near.OfferDesk | None = None,
        campaigns: CampaignStore | None = None,
        understander: Understander | None = None,
    ) -> None:
        self.settings, self.store, self.transcriber, self.command_sink = settings, store, transcriber, command_sink
        self.live, self.access = live, access
        # «Одобрить» / «Нет» under the runner's «show similar options?» question (bot/campaign/offers.py).
        self.offers = offers
        # Read-only here: finds the person's running campaign for «стоп»; the Orchestra cancels it.
        self.campaigns = campaigns
        # Owners from .env plus operators they approved; shared and live.
        self.operators = access.operators if access else OperatorSet(settings.operator_user_ids)
        self.auto = AutoMode(settings_store or MemorySettingsStore(), default=settings.auto_mode,
                             operators=self.operators, auto_operator_ids=settings.auto_operator_user_ids)
        # Mode choice and task intake (bot/control_plane/intake.py); launching always needs "Запустить".
        # Only the owner sees planner details and queue ids in intake replies.
        # ``understander`` reads tasks with AI; None (tests, no key) keeps the deterministic rules.
        self.intake = TaskIntake(intake_store or MemoryIntakeStore(), command_sink, notify_owners=self._tell_owners,
                                 technical=self._is_owner, understander=understander)

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

    def _is_owner(self, user_id: int | None) -> bool:
        return self.operators.role(user_id) == "owner"

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

    def _not_available(self, user_id: int | None) -> Reply:
        """A technical command from anyone who may not use it: one short Russian line, per role."""
        role = self.operators.role(user_id)
        if role == "user":
            return Reply(USER_NOT_AVAILABLE)
        if role == "helper":
            return Reply(HELPER_NOT_AVAILABLE)
        if self.access is None:  # no request button to point at
            return Reply(NOT_AVAILABLE)
        return self._with_access_button(Reply(GUEST_NOT_AVAILABLE), user_id)

    def _stale_button(self, user_id: int | None) -> Reply:
        return Reply("This button is no longer valid." if self._can_control(user_id) else STALE_BUTTON)

    async def _stop_search(self, message: IncomingMessage, *, button: bool = False) -> Reply:
        """Cancel the person's running campaign in this chat through the Orchestra's ``campaign cancel``.

        Only the requester (or an owner) may stop it; the Orchestra checks ownership again.
        A «Остановить поиск» tap has no message of its own: its idempotency key is
        derived from the campaign, so a double tap queues one cancel and an old
        button still stops a newer search.
        """
        assert message.user_id is not None
        campaign = None
        if self.campaigns is not None:
            try:
                campaign = await self.campaigns.latest_for_chat(message.chat_id)
            except Exception:  # noqa: BLE001 - a database hiccup must not break the bot
                log.warning("telegram.control.stop_lookup_failed", extra={"user_id": message.user_id})
                return Reply(STOP_FAILED)
        if (campaign is None or campaign.state in TERMINAL_STATES
                or (campaign.requested_by != message.user_id and not self._is_owner(message.user_id))):
            return Reply(NO_ACTIVE_SEARCH)
        try:
            key = _button_key(campaign.id) if button else message.message_id
            await self.command_sink(CommandEnvelope("campaign", f"cancel {campaign.id}", message.chat_id,
                                                    message.user_id, key))
        except Exception:  # noqa: BLE001 - nothing was queued; the person may try again
            log.warning("telegram.control.stop_failed", extra={"user_id": message.user_id})
            return Reply(STOP_FAILED)
        log.info("telegram.control.stop_requested", extra={"user_id": message.user_id, "campaign_id": campaign.id})
        return Reply(SEARCH_STOPPED)

    async def _wants_stop(self, message: IncomingMessage, text: str) -> bool:
        """«стоп»; or «Отмена» when no task is being written (then it means the draft)."""
        if not self._may_give_tasks(message.user_id):
            return False
        word = text.casefold().strip(".! ")
        if word in STOP_WORDS:
            return True
        return word in CANCEL_WORDS and not await self.intake.drafting(message.user_id)  # type: ignore[arg-type]

    async def handle_callback(self, user_id: int | None, data: str, display_name: str | None = None, username: str | None = None,
                              chat_id: int | None = None) -> Reply:
        """Inline buttons: ``live:done|cancel:<id>``, ``live:approve:<code>``, ``access:request``,
        ``access:helper|user|operator|deny:<id>``, ``set:...`` (owner settings), ``mode:<mode>``, ``task:<action>[:<value>]``,
        ``search:stop`` (the same as typing «стоп») and ``near:yes|no:similar|other:<campaign id>``."""
        kind, _, rest = data.partition(":")
        action, _, target = rest.partition(":")
        if data == STOP_CALLBACK:
            if user_id is None or not self._may_give_tasks(user_id):
                return Reply(STALE_BUTTON)
            chat = chat_id if chat_id is not None else user_id
            return await self._stop_search(IncomingMessage(chat_id=chat, user_id=user_id, message_id=0), button=True)
        if kind == near.CALLBACK_KIND:
            return await self._near_match_answer(user_id, action, target)
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
            return self._stale_button(user_id)
        if kind == "set" and self.access is not None:
            return await self.access.settings(user_id, action, target)
        if kind == "live" and user_id not in self.operators:
            # The logged-in browser is staff-only; a forwarded button tells a user or stranger nothing.
            return Reply(STALE_BUTTON)
        if kind == "live" and action == "approve" and self.live is not None:
            return self.live.approve_browser(target, user_id)
        session_id = target
        if kind != "live" or action not in {"done", "cancel"} or not is_session_id(session_id) or self.live is None:
            return self._stale_button(user_id)
        try:
            return await self.live.finish(session_id, user_id, done=action == "done")
        except LiveViewUnavailable as exc:
            return Reply(f"Could not close the browser: {exc}.")

    async def _near_match_answer(self, user_id: int | None, action: str, target: str) -> Reply:
        """Only the campaign's requester (or an owner) answers, and only once; the runner acts on it."""
        parsed = near.parse_callback(action, target)
        if parsed is None or self.offers is None or user_id is None:
            return Reply(near.reply_for("unknown", "similar"))
        approve, bucket, campaign_id = parsed
        try:
            decision = await self.offers.decide(campaign_id, bucket, approve=approve, user_id=user_id,
                                                owner=self._is_owner(user_id))
        except Exception:  # noqa: BLE001 - a database hiccup must not break the bot; the button stays usable
            log.warning("telegram.control.offer_failed", extra={"user_id": user_id})
            return Reply("Не получилось сохранить ответ. Попробуйте ещё раз.")
        log.info("telegram.control.offer_answered", extra={"user_id": user_id, "bucket": bucket, "decision": decision})
        return Reply(near.reply_for(decision, bucket))

    async def handle_text(self, message: IncomingMessage) -> Reply | None:
        if not await self.store.claim_message(message):
            return self._duplicate(message.user_id)
        return await self._handle_command(message, message.text or "")

    async def handle_voice(self, message: IncomingMessage, download: AudioDownload) -> Reply | None:
        """Transcribe one voice note with a single, bounded, paid provider call.

        Every check that can refuse the message runs before the download and
        before any provider request: duplicates, non-operators, missing
        configuration and Telegram's declared size/duration never cost money.
        """
        if not await self.store.claim_message(message):
            return self._duplicate(message.user_id)
        # Voice costs money per second, so it is limited to operators and
        # approved users; text commands such as /status stay open to everyone.
        if not self._may_give_tasks(message.user_id):
            await self._voice_refused(message, "not_operator")
            if self.operators.role(message.user_id) == "helper":
                return Reply(VOICE_HELPER)
            if self.access is None:
                return Reply(VOICE_NO_ACCESS)
            return self._with_access_button(Reply(VOICE_GUEST), message.user_id)
        owner = self._is_owner(message.user_id)

        def refuse(code: str, english: str) -> Reply:
            return Reply(english if owner else VOICE_REPLIES_RU.get(code, VOICE_REPLIES_RU["failed"]))

        if self.transcriber is None:
            await self._voice_refused(message, "stt_not_configured")
            return refuse("stt_not_configured", "Voice transcription is not configured. Please send text.")
        max_bytes, max_seconds = self.settings.stt_max_audio_bytes, self.settings.stt_max_audio_seconds
        if (message.voice_size or 0) > max_bytes:
            await self._voice_refused(message, "too_large")
            return refuse("too_large", f"Voice message is too large; maximum is {max_bytes / 1_048_576:g} MB.")
        if (message.voice_duration_seconds or 0) > max_seconds:
            await self._voice_refused(message, "too_long")
            return refuse("too_long", f"Voice message is too long; maximum is {max_seconds} seconds.")
        try:
            audio = await download()
        except Exception as exc:  # noqa: BLE001 - any download failure ends this message
            log.warning("telegram.control.voice_download_failed", extra={"error": type(exc).__name__})
            await self._voice_refused(message, "download_failed")
            return refuse("download_failed", "I could not download that voice message from Telegram. Please try again or send text.")
        if len(audio) > max_bytes:
            await self._voice_refused(message, "too_large")
            return refuse("too_large", f"Voice message is too large; maximum is {max_bytes / 1_048_576:g} MB.")
        try:
            transcript = await self.transcriber.transcribe(audio, filename="voice.ogg")
        except TranscriptionError as exc:
            log.warning("telegram.control.transcription_failed", extra={"error_code": exc.code, "status": exc.status})
            await self._voice_refused(message, exc.code, status=exc.status)
            detail = TRANSCRIPTION_ERROR_REPLIES.get(exc.code, "The transcription service returned an error.")
            return refuse(exc.code, f"I could not transcribe that voice message. {detail} It was not retried; please send text or try again.")
        await self.store.save_transcript(message, transcript)
        log.info(
            "telegram.control.transcribed",
            extra={"model": transcript.model, "language": transcript.language, "cost_usd": transcript.cost_usd, "audio_seconds": transcript.audio_seconds},
        )
        # The stored transcript stays exactly as returned; only the reply and
        # the command see the cleaned text.
        text = clean_transcript(transcript.text)
        # Spoken commands (status, pause all, ...) are for those who control collection; a user's words are a task.
        command = spoken_command(text) if self._can_control(message.user_id) else None
        command_reply = await self._handle_command(message, command or text)
        if not owner:
            # The transcript is internal: nobody but the owner ever sees it echoed back.
            # Operators still see which command a short phrase was mapped to.
            heard = f"Understood as: {command}\n\n" if command else ""
            return Reply(heard + command_reply.text, command_reply.buttons)
        confidence = f", confidence {transcript.confidence:.0%}" if transcript.confidence is not None else ""
        heard = f"\nUnderstood as: {command}" if command else ""
        return Reply(f"Transcript ({transcript.language or 'unknown'}{confidence}):\n{text}{heard}\n\n{command_reply.text}", command_reply.buttons)

    def _duplicate(self, user_id: int | None) -> Reply | None:
        """Telegram re-delivered an update: operators hear about it, everyone else hears nothing."""
        return Reply("Duplicate update ignored.") if self._can_control(user_id) else None

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
                if not self._can_control(message.user_id):
                    return Reply("Подтверждение устарело. Чтобы остановить поиск, напишите «стоп».")
                return Reply("Confirmation is invalid, expired, or belongs to another operator.")
            command, arguments, confirmation_id = confirmed
            if not self._can_control(message.user_id) and not (command == "campaign" and _is_cancel(arguments)):
                # A user only ever confirms stopping a campaign; the Orchestra checks it is theirs.
                return self._operator_refusal(message) or Reply(USER_NOT_AVAILABLE)
            receipt = await self.command_sink(CommandEnvelope(command, arguments, message.chat_id, message.user_id or 0, message.message_id, confirmation_id))
            if not self._can_control(message.user_id):
                return Reply(SEARCH_STOPPED)
            command_id = getattr(receipt, "command_id", None)
            suffix = f" Queue id: {command_id}." if command_id else ""
            return Reply(f"Confirmed: /{command}. Safely queued for bounded orchestration.{suffix}")
        if not text.startswith("/"):
            if text and await self._wants_stop(message, text):
                return await self._stop_search(message)
            if text and await self.auto.applies_to(message.user_id):
                return await self._auto_goal(message, text)
            if text and (self._is_user(message.user_id) or (self._can_control(message.user_id) and await self.intake.mode(message.user_id))):
                return await self.intake.on_text(message, text)
            if not self._can_control(message.user_id):
                if self.operators.role(message.user_id) == "helper":
                    return Reply(HELPER_GREETING)
                return self._with_access_button(Reply(GUEST_GREETING), message.user_id)
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
            if command == "start" and role != "owner":
                # Only the owner sees the command list; everyone else gets a plain greeting.
                if role == "helper":
                    return Reply(HELPER_GREETING)
                if self._may_give_tasks(message.user_id):
                    return mode_menu(GREETING)
                return self._with_access_button(Reply(GUEST_GREETING), message.user_id)
            if role == "user":
                return mode_menu(USER_HELP) if command == "start" or not await self.intake.mode(message.user_id) else Reply(USER_HELP)
            if role == "helper":
                return Reply(HELPER_GREETING)
            if role is None:
                return self._with_access_button(Reply(GUEST_GREETING), message.user_id)
            text = ("Commands: /status, /run <scope>, /pause <scope>, /resume <scope>, /cancel <scope>, "
                    "/campaign <goal> | status | cancel <id>, "
                    "/login [facebook|instagram|tiktok] [profile-name]. Confirm changes with: confirm <token>.")
            if role == "owner":
                text += " Owners: /settings (roles with buttons), /operators, /role <ID> helper|user|operator, /revoke <ID>, /auto on|off|status."
            if self.auto.eligible(message.user_id):
                text += ("\n\nAuto mode (when an owner turns it on): just write or say the goal, e.g. "
                         "«квартиры в аренду в Мадриде»; /campaign, /run, /pause and /resume are queued "
                         "without confirmation, /cancel still asks.")
            if command == "start" and self._can_control(message.user_id):
                text += "\n\nИли выберите режим и опишите задачу: бот уточнит детали и попросит подтвердить «Запустить»."
                menu = mode_menu(text)
                if role == "owner" and self.access is not None:
                    return Reply(menu.text, (*menu.buttons, SETTINGS_BUTTON))
                return menu
            return Reply(text)
        if command in {"role", "settings", "operators", "revoke", "auto"} and not self._can_control(message.user_id):
            return self._not_available(message.user_id)
        if command == "role":
            return await self.access.set_role(message.user_id, arguments) if self.access else Reply("Unknown command. Send /help.")
        if command == "settings":
            return await self.access.settings(message.user_id) if self.access else Reply("Unknown command. Send /help.")
        if command == "operators":
            return await self.access.list(message.user_id) if self.access else Reply("Unknown command. Send /help.")
        if command == "revoke":
            return await self.access.revoke(message.user_id, arguments) if self.access else Reply("Unknown command. Send /help.")
        if command == "login":
            if message.user_id not in self.operators:  # users and strangers never open the logged-in browser
                return self._not_available(message.user_id)
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
            return Reply("Unknown command. Send /help.") if self._can_control(message.user_id) else self._not_available(message.user_id)
        # Telegram channel posts may not have a sending user. Confirmation
        # tokens must stay bound to a concrete Telegram identity.
        if message.user_id is None:
            return self._not_available(None)
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
            return self._not_available(message.user_id)
        goal = arguments.strip()
        if not goal:
            return Reply("Опишите, что ищете, — я начну поиск. Чтобы остановить поиск, напишите «стоп».")
        if goal.lower() == "status":
            await self.command_sink(CommandEnvelope(command, "status", message.chat_id, message.user_id, message.message_id))
            return Reply("Проверяю, как идёт поиск.")
        if _is_cancel(goal):
            # Users never see campaign ids: «cancel» (with or without one) stops their running search.
            return await self._stop_search(message)
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
            return self._not_available(message.user_id)
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
        return self._not_available(message.user_id)


def _button_key(campaign_id: str) -> int:
    """A negative message id (Telegram's are positive) unique to the campaign: the Orchestra's idempotency key."""
    return -int(hashlib.sha256(f"stop:{campaign_id}".encode()).hexdigest()[:15], 16) - 1


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
