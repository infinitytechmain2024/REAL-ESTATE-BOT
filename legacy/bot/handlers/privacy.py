"""``/forget`` -- a user erasing their own data.

Everything the bot keeps about a Telegram user hangs off the ``users`` row by
``on delete cascade``: their searches, the results sent to them, and the
feedback they left. Deleting that one row therefore erases all of it, which is
why this handler is short.

Two rules shape it. The confirmation is a separate tap, because nothing here
is recoverable. And a failed delete says so rather than reporting success --
telling someone their data is gone when it is not would be worse than not
offering the command at all.
"""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot.config import Settings
from bot.keyboards.privacy import ForgetCallback, forget_keyboard
from bot.logging_conf import get_logger
from bot.services.db import SupabaseRepository

router = Router(name="privacy")
log = get_logger(__name__)

_NOTHING_STORED = (
    "Бот сейчас ничего не сохраняет: база данных не подключена, "
    "так что удалять нечего."
)
_CONFIRM = (
    "Удалить все ваши данные?\n\n"
    "Будут удалены: ваш профиль, история запросов, найденные результаты "
    "и сохранённые объекты. Это нельзя отменить."
)
_DONE = "Готово. Все ваши данные удалены."
_FAILED = (
    "Не удалось удалить данные — что-то пошло не так с базой. "
    "Попробуйте ещё раз позже."
)
_CANCELLED = "Отменено, ничего не удалено."


@router.message(Command("forget"))
async def cmd_forget(message: Message, settings: Settings, repo: SupabaseRepository) -> None:
    """Ask first. The button does the deleting."""
    if message.from_user is None:
        return
    if not settings.supabase.configured:
        await message.answer(_NOTHING_STORED)
        return

    await message.answer(_CONFIRM, reply_markup=forget_keyboard())


@router.callback_query(ForgetCallback.filter())
async def on_forget_confirmed(
    query: CallbackQuery,
    settings: Settings,
    repo: SupabaseRepository,
    callback_data: ForgetCallback | None = None,
) -> None:
    """Erase the caller's own rows, and report honestly whether it worked."""
    if query.from_user is None or query.message is None:
        await query.answer()
        return

    if callback_data is not None and not callback_data.confirm:
        await query.answer()
        await query.message.answer(_CANCELLED)
        return

    await query.answer()
    erased = await repo.forget_user(query.from_user.id)
    log.info("privacy.forget", user_id=query.from_user.id, erased=bool(erased))
    await query.message.answer(_DONE if erased else _FAILED)
