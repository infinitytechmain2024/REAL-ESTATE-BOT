"""Mode selection."""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models.enums import Mode


class ModeCallback(CallbackData, prefix="mode"):
    """Pressed one of the two mode buttons."""

    mode: Mode


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """The two big buttons shown by /start.

    One per row so each gets the full message width -- the labels are long
    enough that a two-column layout truncates them on narrow phones.
    """
    builder = InlineKeyboardBuilder()
    for mode in (Mode.LAND, Mode.INVESTORS):
        builder.row(
            InlineKeyboardButton(text=mode.title, callback_data=ModeCallback(mode=mode).pack())
        )
    return builder.as_markup()


def mode_switch_keyboard(current: Mode) -> InlineKeyboardMarkup:
    """Offer the *other* mode, plus a way back to the menu."""
    other = Mode.INVESTORS if current is Mode.LAND else Mode.LAND
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=f"Переключиться на {other.title}",
            callback_data=ModeCallback(mode=other).pack(),
        )
    )
    return builder.as_markup()
