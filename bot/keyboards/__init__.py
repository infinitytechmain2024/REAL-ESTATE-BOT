"""Inline keyboards. The bot uses no reply keyboards at all, by design."""

from bot.keyboards.main_menu import main_menu_keyboard, mode_switch_keyboard
from bot.keyboards.result import (
    DetailsCallback,
    FeedbackCallback,
    result_keyboard,
    result_keyboard_after,
)

__all__ = [
    "DetailsCallback",
    "FeedbackCallback",
    "main_menu_keyboard",
    "mode_switch_keyboard",
    "result_keyboard",
    "result_keyboard_after",
]
