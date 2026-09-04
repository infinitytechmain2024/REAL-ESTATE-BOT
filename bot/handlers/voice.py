"""Voice messages and audio files.

Telegram voice notes are OGG/Opus, which the OpenAI-compatible transcription
endpoints accept directly -- no ffmpeg step is needed. The transcript is echoed
back to the user before the search starts, so a misheard request is obvious
immediately rather than after a minute of searching.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import Settings
from bot.exceptions import STTError
from bot.handlers.search import run_research
from bot.keyboards.main_menu import main_menu_keyboard
from bot.logging_conf import get_logger
from bot.middlewares.throttling import SearchSlots
from bot.services.limits import QuotaService
from bot.services.pipeline import ResearchPipeline
from bot.services.stt import AudioFile, STTManager
from bot.states import Research
from bot.utils.text import escape_html, truncate

router = Router(name="voice")
log = get_logger(__name__)

_VOICE_FILTER = F.voice | F.audio | F.video_note


@router.message(Research.processing, _VOICE_FILTER)
async def on_voice_while_busy(message: Message) -> None:
    """A voice note that arrived while a search is already running.

    Registered ahead of the mode-less handler below, and it deliberately does
    not touch the FSM. Without it a voice note during a search fell through to
    `on_voice_without_mode`, which reset the state to `choosing_mode` -- so the
    running search would finish and set the state back to `waiting_query`,
    leaving the user staring at a mode menu that was already obsolete, with no
    idea which of the two messages to believe.

    Transcribing now and queueing the request was the alternative; refusing is
    the honest one. The user gets an answer immediately and keeps their voice
    note, instead of a request they can no longer see or cancel firing off
    minutes later against a mode they may since have changed.
    """
    await message.answer(
        "⏳ Сейчас выполняется ваш предыдущий запрос.\n"
        "Дождитесь результатов и отправьте голосовое ещё раз — я его не потерял, "
        "просто пока не могу взять в работу."
    )


@router.message(Research.waiting_query, _VOICE_FILTER)
async def on_voice(
    message: Message,
    state: FSMContext,
    pipeline: ResearchPipeline,
    settings: Settings,
    slots: SearchSlots,
    quota: QuotaService,
    stt: STTManager,
) -> None:
    """Transcribe, confirm, then run the normal search flow."""
    if not settings.stt.enabled:
        await message.answer(
            "🔇 Распознавание голосовых сообщений отключено. Напишите запрос текстом, пожалуйста."
        )
        return

    status = await message.answer("🎧 Распознаю голосовое сообщение…")

    try:
        audio = await _download(message, settings)
    except STTError as exc:
        await status.edit_text(f"⚠️ {exc.user_message}")
        return

    try:
        transcript = await stt.transcribe(audio)
    except STTError as exc:
        log.warning("voice.transcribe_failed", error=str(exc))
        await status.edit_text(f"⚠️ {exc.user_message}")
        return

    log.info("voice.transcribed", chars=len(transcript.text), language=transcript.language)
    await status.edit_text(
        f"🗣 <b>Распознано:</b>\n<i>{escape_html(truncate(transcript.text, 800))}</i>"
    )

    await run_research(
        message=message,
        state=state,
        pipeline=pipeline,
        settings=settings,
        slots=slots,
        quota=quota,
        text=transcript.text,
        transcript=transcript.text,
    )


@router.message(_VOICE_FILTER)
async def on_voice_without_mode(message: Message, state: FSMContext) -> None:
    """A voice note arrived before a mode was picked.

    Only reached when no search is in flight -- `on_voice_while_busy` claims
    that case first -- so resetting the state here is safe.
    """
    await state.set_state(Research.choosing_mode)
    await message.answer(
        "Сначала выберите режим, потом можно диктовать:",
        reply_markup=main_menu_keyboard(),
    )


async def _download(message: Message, settings: Settings) -> AudioFile:
    """Pull the audio payload off Telegram's servers.

    The size is checked against ``STT_MAX_AUDIO_MB`` before downloading, since
    Telegram reports it in the update and there is no point spending the
    bandwidth on a file the provider will refuse.
    """
    source = message.voice or message.audio or message.video_note
    if source is None or message.bot is None:
        raise STTError("no audio payload in message")

    size_mb = (source.file_size or 0) / 1_048_576
    if size_mb > settings.stt.max_audio_mb:
        raise STTError(
            f"audio is {size_mb:.1f} MB, over the {settings.stt.max_audio_mb} MB limit",
            user_message=(
                f"Файл слишком большой ({size_mb:.1f} МБ). "
                f"Максимум — {settings.stt.max_audio_mb:g} МБ. Запишите покороче."
            ),
        )

    try:
        buffer = await message.bot.download(source)
    except Exception as exc:
        raise STTError(f"could not download audio: {exc}") from exc

    if buffer is None:
        raise STTError("Telegram returned an empty file")

    data = buffer.read()
    if not data:
        raise STTError("downloaded audio was empty")

    filename, mime = _naming(message)
    return AudioFile(
        data=data,
        filename=filename,
        mime_type=mime,
        duration_seconds=getattr(source, "duration", None),
    )


def _naming(message: Message) -> tuple[str, str]:
    """Filename and MIME type to send to the provider.

    Several gateways dispatch on the file extension rather than the MIME type,
    so the two are kept consistent.
    """
    if message.voice is not None:
        return "voice.ogg", message.voice.mime_type or "audio/ogg"
    if message.video_note is not None:
        return "note.mp4", "video/mp4"
    audio = message.audio
    if audio is not None:
        name = audio.file_name or "audio.mp3"
        return name, audio.mime_type or "audio/mpeg"
    return "audio.ogg", "audio/ogg"
