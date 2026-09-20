"""Confirmation for an irreversible erasure request."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class ForgetCallback(CallbackData, prefix="forget"):
    """Pressed the confirmation under /forget."""

    confirm: bool


def forget_keyboard() -> InlineKeyboardMarkup:
    """Confirm or cancel. Deliberately two taps -- this cannot be undone."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🗑 Да, удалить мои данные",
            callback_data=ForgetCallback(confirm=True).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="Отмена", callback_data=ForgetCallback(confirm=False).pack()
        )
    )
    return builder.as_markup()
