"""Long-polling Telegram adapter for the open control plane."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from aiohttp import web

from bot.campaign.store import PostgresCampaignStore
from bot.control_plane.access import AccessDesk, PostgresAccessStore
from bot.control_plane.auto import PostgresSettingsStore
from bot.control_plane.live_view import (
    BrowserLiveClient,
    LiveViewConfig,
    LiveViewCoordinator,
    create_gate_app,
)
from bot.control_plane.models import CommandEnvelope, IncomingMessage, Reply
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import PostgresControlPlaneStore, PostgresLiveViewStore
from bot.control_plane.stt import OpenRouterTranscriber
from bot.operators import OperatorSet
from bot.orchestra.dispatcher import OrchestraDispatcher
from bot.orchestra.models import ConfirmedCommand
from bot.orchestra.store import PostgresOrchestraStore


def _incoming(message: Message) -> IncomingMessage:
    return IncomingMessage(chat_id=message.chat.id, user_id=message.from_user.id if message.from_user else None, message_id=message.message_id, text=message.text, voice_file_id=message.voice.file_id if message.voice else None, voice_size=message.voice.file_size if message.voice else None, voice_duration_seconds=message.voice.duration if message.voice else None)


def _markup(reply: Reply) -> InlineKeyboardMarkup | None:
    if not reply.buttons:
        return None
    rows = [
        [InlineKeyboardButton(text=b.text, web_app=WebAppInfo(url=b.web_app_url))]
        if b.web_app_url
        else [InlineKeyboardButton(text=b.text, callback_data=b.callback_data)]
        for b in reply.buttons
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = ControlPlaneSettings.from_env()
    store = PostgresControlPlaneStore(settings.database_url)
    await store.connect()
    orchestra_store = PostgresOrchestraStore(settings.database_url, settings.safety_limits)
    await orchestra_store.connect()
    bot = Bot(settings.telegram_token)

    async def notify(chat_id: int, text: str) -> None:
        await bot.send_message(chat_id, text)

    async def send(chat_id: int, reply: Reply) -> None:
        await bot.send_message(chat_id, reply.text, reply_markup=_markup(reply))

    # Owners from .env plus the helpers/operators they approved, shared by every check.
    operators = OperatorSet(settings.operator_user_ids)
    access = AccessDesk(PostgresAccessStore(store), operators, notify=send)
    try:
        await access.load()
    except Exception:
        logging.getLogger(__name__).exception("telegram.control.operators_load_failed")

    orchestra = OrchestraDispatcher(
        orchestra_store,
        # Helpers only verify; queued commands are re-checked against controllers.
        operator_ids=operators.controllers,
        lease_seconds=settings.orchestra_command_lease_seconds,
        poll_seconds=settings.orchestra_poll_seconds,
        stale_batch_seconds=settings.orchestra_stale_batch_seconds,
        notifier=notify,
        # /campaign plans and stores a campaign; the campaign-runner service runs it.
        campaigns=PostgresCampaignStore(orchestra_store.pool) if orchestra_store.pool else None,
    )

    async def enqueue(envelope: CommandEnvelope) -> object:
        logging.getLogger(__name__).info("telegram.control.command_confirmed", extra={"command": envelope.command, "chat_id": envelope.chat_id, "user_id": envelope.user_id})
        return await orchestra.enqueue(ConfirmedCommand(envelope.command, envelope.arguments, envelope.chat_id, envelope.user_id, envelope.message_id, envelope.confirmation_id, envelope.auto))

    if not settings.operator_user_ids:
        logging.getLogger(__name__).warning("telegram.control.no_operators", extra={"hint": "set TELEGRAM_OPERATOR_IDS; state-changing commands are refused"})
    transcriber: OpenRouterTranscriber | None = None
    if settings.openrouter_api_key:
        transcriber = OpenRouterTranscriber(
            api_key=settings.openrouter_api_key,
            model=settings.stt_model,
            timeout_seconds=settings.stt_timeout_seconds,
            max_audio_bytes=settings.stt_max_audio_bytes,
        )
    else:
        logging.getLogger(__name__).warning("telegram.control.stt_disabled", extra={"hint": "set OPENROUTER_API_KEY; voice messages are refused"})

    browser = BrowserLiveClient(settings.browser_session_url, settings.browser_session_api_token)
    live = LiveViewCoordinator(
        PostgresLiveViewStore(store),
        browser,
        LiveViewConfig(
            public_url=settings.live_view_public_url,
            operator_ids=operators,
            open_minutes=settings.live_view_open_minutes,
            request_minutes=settings.live_view_request_minutes,
        ),
        notifier=send,
    )
    if not live.enabled:
        logging.getLogger(__name__).warning("telegram.control.live_view_disabled", extra={"hint": "set LIVE_VIEW_PUBLIC_URL to an https:// origin"})
    control = ControlPlane(settings, store, transcriber, enqueue, live, access, PostgresSettingsStore(store))
    for user_id in sorted(settings.auto_operator_user_ids):
        if not operators.can_control(user_id):
            # Not refused at startup (approvals change at runtime), but never auto-eligible meanwhile.
            logging.getLogger(__name__).warning("telegram.control.auto_operator_not_eligible", extra={"user_id": user_id})
    router = Router(name="control-plane")

    @router.callback_query()
    async def button(query: CallbackQuery) -> None:
        user = query.from_user
        reply = await control.handle_callback(
            user.id if user else None, query.data or "",
            user.full_name if user else None, user.username if user else None,
        )
        await query.answer()
        if query.message is not None:
            await bot.send_message(query.message.chat.id, reply.text, reply_markup=_markup(reply))

    @router.message(lambda message: bool(message.voice))
    async def voice(message: Message) -> None:
        voice_note = message.voice
        if voice_note is None:
            return

        async def download() -> bytes:
            data = await message.bot.download(voice_note, timeout=int(settings.stt_timeout_seconds))
            return data.read() if data else b""

        reply = await control.handle_voice(_incoming(message), download)
        if reply:
            await message.answer(reply.text, reply_markup=_markup(reply))

    @router.message()
    async def text(message: Message) -> None:
        reply = await control.handle_text(_incoming(message))
        if reply:
            await message.answer(reply.text, reply_markup=_markup(reply))

    # Replies intentionally use Telegram's plain-text default: transcribed
    # speech is untrusted user content and must not be parsed as HTML.
    telegram_dispatcher = Dispatcher()
    telegram_dispatcher.include_router(router)
    dispatcher_task = asyncio.create_task(orchestra.run_forever(), name="orchestra-dispatcher")
    # The gate listens on the private Docker network; Caddy is its only public front.
    gate = web.AppRunner(create_gate_app(live, settings.telegram_token, settings.novnc_url))
    await gate.setup()
    await web.TCPSite(gate, "0.0.0.0", settings.live_view_port).start()
    watcher_stop = asyncio.Event()
    watcher_task = asyncio.create_task(live.run_forever(settings.live_view_poll_seconds, watcher_stop), name="live-view-watcher")
    try:
        await telegram_dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        watcher_stop.set()
        await watcher_task
        await gate.cleanup()
        await browser.aclose()
        orchestra.stop()
        await dispatcher_task
        await orchestra_store.close()
        await store.close()
        if transcriber is not None:
            await transcriber.aclose()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
