"""Long-polling Telegram adapter for the open control plane."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message

from bot.control_plane.models import CommandEnvelope, IncomingMessage
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import PostgresControlPlaneStore
from bot.control_plane.stt import FasterWhisperTranscriber
from bot.orchestra.dispatcher import OrchestraDispatcher
from bot.orchestra.models import ConfirmedCommand
from bot.orchestra.store import PostgresOrchestraStore


def _incoming(message: Message) -> IncomingMessage:
    return IncomingMessage(chat_id=message.chat.id, user_id=message.from_user.id if message.from_user else None, message_id=message.message_id, text=message.text, voice_file_id=message.voice.file_id if message.voice else None, voice_size=message.voice.file_size if message.voice else None, voice_duration_seconds=message.voice.duration if message.voice else None)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = ControlPlaneSettings.from_env()
    store = PostgresControlPlaneStore(settings.database_url)
    await store.connect()
    orchestra_store = PostgresOrchestraStore(settings.database_url)
    await orchestra_store.connect()
    bot = Bot(settings.telegram_token)

    async def notify(chat_id: int, text: str) -> None:
        await bot.send_message(chat_id, text)

    orchestra = OrchestraDispatcher(
        orchestra_store,
        operator_ids=settings.operator_user_ids,
        lease_seconds=settings.orchestra_command_lease_seconds,
        poll_seconds=settings.orchestra_poll_seconds,
        stale_batch_seconds=settings.orchestra_stale_batch_seconds,
        notifier=notify,
    )

    async def enqueue(envelope: CommandEnvelope) -> object:
        logging.getLogger(__name__).info("telegram.control.command_confirmed", extra={"command": envelope.command, "chat_id": envelope.chat_id, "user_id": envelope.user_id})
        return await orchestra.enqueue(ConfirmedCommand(envelope.command, envelope.arguments, envelope.chat_id, envelope.user_id, envelope.message_id, envelope.confirmation_id))

    if not settings.operator_user_ids:
        logging.getLogger(__name__).warning("telegram.control.no_operators", extra={"hint": "set TELEGRAM_OPERATOR_IDS; state-changing commands are refused"})
    control = ControlPlane(settings, store, FasterWhisperTranscriber(model=settings.stt_model, device=settings.stt_device, compute_type=settings.stt_compute_type), enqueue)
    router = Router(name="control-plane")

    @router.message(lambda message: bool(message.voice))
    async def voice(message: Message) -> None:
        if message.voice is None:
            return
        data = await message.bot.download(message.voice)
        reply = await control.handle_voice(_incoming(message), data.read() if data else b"")
        if reply:
            await message.answer(reply.text)

    @router.message()
    async def text(message: Message) -> None:
        reply = await control.handle_text(_incoming(message))
        if reply:
            await message.answer(reply.text)

    # Replies intentionally use Telegram's plain-text default: transcribed
    # speech is untrusted user content and must not be parsed as HTML.
    telegram_dispatcher = Dispatcher()
    telegram_dispatcher.include_router(router)
    dispatcher_task = asyncio.create_task(orchestra.run_forever(), name="orchestra-dispatcher")
    try:
        await telegram_dispatcher.start_polling(bot, allowed_updates=["message"])
    finally:
        orchestra.stop()
        await dispatcher_task
        await orchestra_store.close()
        await store.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
