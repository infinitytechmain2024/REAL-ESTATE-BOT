"""Shared live-view button and recovery copy for Facebook alerts."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import Settings
from bot.services.facebook.tokens import TokenStore

OPEN_BUTTON_TEXT = "Открыть Facebook"
RECOVERY_TEXT = "Готово. Проверка завершена. Бот продолжит работу."


async def open_button(
    settings: Settings, token_store: TokenStore | None
) -> InlineKeyboardMarkup | None:
    """A keyboard with the live-view link, or None if there is nothing to link to.

    Reuses the current token if one is still valid rather than always minting
    a fresh one -- see ``TokenStore.get_or_create`` for why: a fresh token on
    every button tap would invalidate a login the admin is already mid-way
    through in an open tab.
    """
    if token_store is None or not settings.facebook.desktop_public_base:
        return None
    token = await token_store.get_or_create(settings.facebook.desktop_token_ttl_seconds)
    url = f"{settings.facebook.desktop_public_base.rstrip('/')}/s/{token}"
    button = InlineKeyboardButton(text=OPEN_BUTTON_TEXT, url=url)
    return InlineKeyboardMarkup(inline_keyboard=[[button]])
