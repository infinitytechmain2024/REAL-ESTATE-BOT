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


async def _record_command(envelope: CommandEnvelope) -> None:
    logging.getLogger(__name__).info("telegram.control.command_confirmed", extra={"command": envelope.command, "chat_id": envelope.chat_id, "user_id": envelope.user_id})


def _incoming(message: Message) -> IncomingMessage:
    return IncomingMessage(chat_id=message.chat.id, user_id=message.from_user.id if message.from_user else None, message_id=message.message_id, text=message.text, voice_file_id=message.voice.file_id if message.voice else None, voice_size=message.voice.file_size if message.voice else None, voice_duration_seconds=message.voice.duration if message.voice else None)


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = ControlPlaneSettings.from_env()
    store = PostgresControlPlaneStore(settings.database_url)
    await store.connect()
    control = ControlPlane(settings, store, FasterWhisperTranscriber(model=settings.stt_model, device=settings.stt_device, compute_type=settings.stt_compute_type), _record_command)
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
    bot = Bot(settings.telegram_token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    try:
        await dispatcher.start_polling(bot, allowed_updates=["message"])
    finally:
        await store.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(run())
