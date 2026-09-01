"""Bind the user and chat to every log line produced while handling an update."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from bot.logging_conf import bind_request_context, clear_request_context


class LoggingContextMiddleware(BaseMiddleware):
    """Populate structlog's contextvars for the duration of one update."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        bind_request_context(
            update_id=str(uuid4())[:8],
            user_id=user.id if user else None,
        )
        try:
            return await handler(event, data)
        finally:
            # Handlers run as tasks on a shared loop; leaving context bound
            # would leak one user's id into the next update's log lines.
            clear_request_context()
