"""Buttons under each result message.

Callback payloads are capped by Telegram at 64 bytes, so the result is
referenced by its database UUID (36 chars) rather than by URL. Results that
were never persisted -- Supabase disabled or unreachable -- get no buttons at
all, since there would be nothing to record the press against; the message
itself still carries the link.
"""

from __future__ import annotations

from uuid import UUID

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models.enums import Feedback


class FeedbackCallback(CallbackData, prefix="fb"):
    """Interesting / not interesting / save."""

    action: Feedback
    result_id: UUID


class DetailsCallback(CallbackData, prefix="det"):
    """Ask the LLM for a longer briefing on one result."""

    result_id: UUID


_LABELS: dict[Feedback, str] = {
    Feedback.INTERESTING: "👍 Интересно",
    Feedback.NOT_INTERESTING: "👎 Не интересно",
    Feedback.SAVE: "⭐ Сохранить",
}


def result_keyboard(result_id: UUID | None) -> InlineKeyboardMarkup | None:
    """The four buttons under a result, or ``None`` if it was not stored."""
    if result_id is None:
        return None

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=_LABELS[Feedback.INTERESTING],
            callback_data=FeedbackCallback(action=Feedback.INTERESTING, result_id=result_id).pack(),
        ),
        InlineKeyboardButton(
            text=_LABELS[Feedback.NOT_INTERESTING],
            callback_data=FeedbackCallback(
                action=Feedback.NOT_INTERESTING, result_id=result_id
            ).pack(),
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text=_LABELS[Feedback.SAVE],
            callback_data=FeedbackCallback(action=Feedback.SAVE, result_id=result_id).pack(),
        ),
        InlineKeyboardButton(
            text="🔍 Подробнее",
            callback_data=DetailsCallback(result_id=result_id).pack(),
        ),
    )
    return builder.as_markup()


def result_keyboard_after(result_id: UUID, action: Feedback) -> InlineKeyboardMarkup:
    """Keyboard shown once the user has reacted.

    The chosen reaction is replaced by a static confirmation so the message
    reflects the decision, while 'Подробнее' stays available -- it is a read,
    not a vote, and is still useful after marking something interesting.
    """
    confirmations: dict[Feedback, str] = {
        Feedback.INTERESTING: "✅ Отмечено как интересное",
        Feedback.NOT_INTERESTING: "🚫 Отмечено как неинтересное",
        Feedback.SAVE: "⭐ Сохранено",
    }

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=confirmations.get(action, "✅ Готово"),
            callback_data=DetailsCallback(result_id=result_id).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔍 Подробнее",
            callback_data=DetailsCallback(result_id=result_id).pack(),
        )
    )
    return builder.as_markup()
