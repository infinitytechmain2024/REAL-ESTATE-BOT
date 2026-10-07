"""The four buttons under every result."""

from __future__ import annotations

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot.exceptions import BotError
from bot.keyboards.result import DetailsCallback, FeedbackCallback, result_keyboard_after
from bot.logging_conf import get_logger
from bot.models.enums import Feedback, Mode
from bot.models.query import ParsedQuery
from bot.services.db import SupabaseRepository
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
) -> None:
    """Produce a longer briefing about one result."""
    if callback.from_user is None or callback.message is None:
        await callback.answer()
        return

    result = await repo.get_result(callback_data.result_id)
    if result is None:
        await callback.answer(
            "Не удалось найти этот результат — возможно, база недоступна.", show_alert=True
        )
        return

    await repo.record_feedback(
        result_id=callback_data.result_id,
        user_id=callback.from_user.id,
        action=Feedback.DETAILS,
    )
    await callback.answer("🔍 Собираю подробности…")

    notice = await callback.message.answer("🔍 Читаю страницу и готовлю сводку…")
    try:
        briefing = await pipeline.details(await _context_query(state, result.mode), result)
    except BotError as exc:
        await notice.edit_text(f"⚠️ {exc.user_message}")
        return
    except Exception:
        log.exception("details.crashed")
        await notice.edit_text("⚠️ Не удалось собрать подробности. Попробуйте ещё раз.")
        return

    header = f"🔍 <b>{escape_html(truncate(result.title or result.url, 100))}</b>\n\n"
    chunks = split_message(header + escape_html(briefing))
    await notice.edit_text(chunks[0], disable_web_page_preview=True)
    for chunk in chunks[1:]:
        await callback.message.answer(chunk, disable_web_page_preview=True)


async def _context_query(state: FSMContext, mode: Mode) -> ParsedQuery:
    """Best available description of what the user was looking for.

    The original :class:`ParsedQuery` is not carried in the callback payload
    (64 bytes), so the mode is reconstructed from the stored result and the
    rest is left empty -- the briefing prompt only uses it for framing.
    """
    return ParsedQuery(mode=mode)


__all__ = ["router"]
