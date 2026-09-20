"""Admin-only controls for the shared Facebook browser session."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


class FacebookAdminCallback(CallbackData, prefix="fbadmin"):
    """Pressed one of the two Facebook-connection buttons."""

    action: str  # "status" | "start_login"


def facebook_admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="🔄 Проверить статус",
            callback_data=FacebookAdminCallback(action="status").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🔓 Открыть окно для входа",
            callback_data=FacebookAdminCallback(action="start_login").pack(),
        )
    )
    return builder.as_markup()
