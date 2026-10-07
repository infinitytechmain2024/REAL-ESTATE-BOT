"""Keep the ``users`` table current and hand the repository to handlers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from bot.services.db import SupabaseRepository


class UserMiddleware(BaseMiddleware):
    """Upsert the sender before the handler runs.

    Doing this here rather than in each handler means a foreign key from
    ``searches`` or ``results`` to ``users`` can never dangle, whichever entry
    point the user came through.
    """

    def __init__(self, repo: SupabaseRepository) -> None:
        self.repo = repo

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None and not user.is_bot:
            await self.repo.upsert_user(
                user.id,
                username=user.username,
                first_name=user.first_name,
                language_code=user.language_code,
            )
        return await handler(event, data)
