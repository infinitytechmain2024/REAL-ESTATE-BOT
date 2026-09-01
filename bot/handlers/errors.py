"""Catch-all error handler.

Registered on the dispatcher, so an exception escaping any handler is logged
with the update that caused it and answered with a neutral message rather than
silently swallowed.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ErrorEvent

from bot.exceptions import BotError
from bot.logging_conf import get_logger

router = Router(name="errors")
log = get_logger(__name__)


@router.errors()
async def on_error(event: ErrorEvent) -> bool:
    """Log the failure and tell the user something went wrong.

    Returning ``True`` marks the update handled so aiogram does not re-raise
    into the polling loop and stop it.
    """
    exception = event.exception
    user_message = (
        exception.user_message
        if isinstance(exception, BotError)
        else "⚠️ Что-то пошло не так. Попробуйте ещё раз или начните с /start."
    )

    log.exception(
        "handler.unhandled_error",
        error_type=type(exception).__name__,
        error=str(exception),
    )

    message = event.update.message or (
        event.update.callback_query.message if event.update.callback_query else None
    )
    if message is not None:
        try:
            await message.answer(user_message)
        except TelegramAPIError:
            log.debug("error.reply_failed")

    return True
