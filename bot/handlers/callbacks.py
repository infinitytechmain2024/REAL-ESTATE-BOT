"""The four buttons under every result."""

from __future__ import annotations

import asyncio

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.config import Settings
from bot.exceptions import BotError, PipelineTimeoutError
from bot.keyboards.result import DetailsCallback, FeedbackCallback, result_keyboard_after
from bot.logging_conf import get_logger
from bot.middlewares.throttling import BUSY_MESSAGES, Cooldown, SearchSlots
from bot.models.enums import Feedback, Mode
from bot.models.query import ParsedQuery
from bot.services.db import SupabaseRepository
from bot.services.limits import QuotaKind, QuotaService
from bot.services.pipeline import ResearchPipeline
from bot.utils.text import escape_html, split_message, truncate

router = Router(name="callbacks")
log = get_logger(__name__)

_TOASTS: dict[Feedback, str] = {
    Feedback.INTERESTING: "👍 Отмечено как интересное",
    Feedback.NOT_INTERESTING: "👎 Отмечено как неинтересное",
    Feedback.SAVE: "⭐ Сохранено",
}


@router.callback_query(FeedbackCallback.filter())
async def on_feedback(
    callback: CallbackQuery,
    callback_data: FeedbackCallback,
    repo: SupabaseRepository,
) -> None:
    """Record a reaction and reflect it in the message's keyboard."""
    if callback.from_user is None:
        await callback.answer()
        return

    await repo.record_feedback(
        result_id=callback_data.result_id,
        user_id=callback.from_user.id,
        action=callback_data.action,
    )
    log.info("feedback.recorded", action=callback_data.action.value)

    try:
        await callback.message.edit_reply_markup(  # type: ignore[union-attr]
            reply_markup=result_keyboard_after(callback_data.result_id, callback_data.action)
        )
    except TelegramAPIError as exc:
        # The message may be too old to edit; the feedback is already stored.
        log.debug("feedback.markup_edit_failed", error=str(exc))

    await callback.answer(_TOASTS.get(callback_data.action, "Готово"))


@router.callback_query(DetailsCallback.filter())
async def on_details(
    callback: CallbackQuery,
    callback_data: DetailsCallback,
    repo: SupabaseRepository,
    pipeline: ResearchPipeline,
    state: FSMContext,
    settings: Settings,
    slots: SearchSlots,
    quota: QuotaService,
    details_cooldown: Cooldown,
) -> None:
    """Produce a longer briefing about one result.

    This is the only button that costs money -- an LLM call and, when the page
    text was not stored, a fetch on top. It is therefore guarded exactly like a
    search: a cooldown, a concurrency slot, a daily quota and a timeout. The
    feedback buttons next to it stay free and instant.
    """
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return

    user_id = callback.from_user.id

    wait = details_cooldown.try_pass(user_id)
    if wait > 0:
        log.debug("details.throttled", user_id=user_id, wait=round(wait, 2))
        await callback.answer(
            f"⏳ Слишком часто. Подождите {wait:.0f} сек.", show_alert=False
        )
        return

    denial = slots.try_acquire(user_id)
    if denial is not None:
        log.info("details.refused", user_id=user_id, reason=denial.value)
        await callback.answer(BUSY_MESSAGES[denial], show_alert=True)
        return

    try:
        await _run_details(
            callback=callback,
            callback_data=callback_data,
            repo=repo,
            pipeline=pipeline,
            state=state,
            settings=settings,
            quota=quota,
            user_id=user_id,
        )
    finally:
        slots.release(user_id)


async def _run_details(
    *,
    callback: CallbackQuery,
    callback_data: DetailsCallback,
    repo: SupabaseRepository,
    pipeline: ResearchPipeline,
    state: FSMContext,
    settings: Settings,
    quota: QuotaService,
    user_id: int,
) -> None:
    """The body of :func:`on_details`, with the slot already held."""
    result = await repo.get_result(callback_data.result_id)
    if result is None:
        await callback.answer(
            "Не удалось найти этот результат — возможно, база недоступна.", show_alert=True
        )
        return

    # Consumed only once the result is known to exist, so a press that could
    # never have produced anything does not cost the user part of their day.
    try:
        await quota.consume(user_id, QuotaKind.DETAILS)
    except BotError as exc:
        await callback.answer(exc.user_message, show_alert=True)
        return

    await repo.record_feedback(
        result_id=callback_data.result_id,
        user_id=user_id,
        action=Feedback.DETAILS,
    )
    await callback.answer("🔍 Собираю подробности…")

    notice = await callback.message.answer("🔍 Читаю страницу и готовлю сводку…")  # type: ignore[union-attr]
    timeout = settings.pipeline.details_timeout_seconds
    try:
        async with asyncio.timeout(timeout):
            briefing = await pipeline.details(
                await _context_query(state, result.mode), result, user_id=user_id
            )
    except TimeoutError:
        log.warning("details.timed_out", user_id=user_id, timeout_seconds=timeout)
        await _safe_edit(notice, f"⚠️ {PipelineTimeoutError.default_user_message}")
        return
    except BotError as exc:
        log.warning("details.failed", user_id=user_id, error=str(exc))
        await _safe_edit(notice, f"⚠️ {exc.user_message}")
        return
    except Exception:
        log.exception("details.crashed")
        await _safe_edit(notice, "⚠️ Не удалось собрать подробности. Попробуйте ещё раз.")
        return

    header = f"🔍 <b>{escape_html(truncate(result.title or result.url, 100))}</b>\n\n"
    chunks = split_message(header + escape_html(briefing))
    await _safe_edit(notice, chunks[0])
    for chunk in chunks[1:]:
        await callback.message.answer(chunk, disable_web_page_preview=True)  # type: ignore[union-attr]


async def _safe_edit(notice, text: str) -> None:  # type: ignore[no-untyped-def]
    """Edit a message, tolerating Telegram's complaints.

    The briefing is already paid for by the time this runs; losing it to a
    'message is not modified' or a deleted message would waste the call.
    """
    try:
        await notice.edit_text(text, disable_web_page_preview=True)
    except TelegramAPIError as exc:
        log.debug("details.edit_failed", error=str(exc))


async def _context_query(state: FSMContext, mode: Mode) -> ParsedQuery:
    """Best available description of what the user was looking for.

    The original :class:`ParsedQuery` is not carried in the callback payload
    (64 bytes), so the mode is reconstructed from the stored result and the
    rest is left empty -- the briefing prompt only uses it for framing.
    """
    return ParsedQuery(mode=mode)


__all__ = ["router"]
