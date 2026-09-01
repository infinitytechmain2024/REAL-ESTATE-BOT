"""/start, /help, and choosing a mode."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards.main_menu import ModeCallback, main_menu_keyboard, mode_switch_keyboard
from bot.logging_conf import get_logger
from bot.models.enums import Mode
from bot.services.db import SupabaseRepository
from bot.states import Research

router = Router(name="start")
log = get_logger(__name__)

WELCOME = (
    "👋 <b>Привет!</b>\n\n"
    "Я ищу объекты недвижимости и потенциальных партнёров по открытым источникам: "
    "прогоняю ваш запрос через поисковые системы, читаю найденные страницы и "
    "присылаю только то, что действительно подходит.\n\n"
    "Выберите режим:"
)

HELP = (
    "<b>Как это работает</b>\n\n"
    "1. Выберите режим кнопкой ниже.\n"
    "2. Опишите, что ищете — <b>текстом или голосовым сообщением</b>. "
    "Голос я распознаю автоматически.\n"
    "3. Я разберу запрос, поищу по Google, Bing, DuckDuckGo и другим источникам, "
    "прочитаю найденные страницы и пришлю каждый результат отдельным сообщением.\n\n"
    "Чем конкретнее запрос, тем лучше результат. Например:\n"
    "<i>«Участок 2–5 соток в Лимассоле, до 300 000 евро, с видом на море»</i>\n"
    "<i>«Фонды, инвестирующие в жилую недвижимость Испании от 5 млн евро»</i>\n\n"
    "Под каждым результатом — кнопки «Интересно», «Не интересно», «Сохранить» "
    "и «Подробнее».\n\n"
    "Команды: /start — меню, /mode — сменить режим, /help — эта справка."
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    """Greeting and the two mode buttons."""
    await state.clear()
    await state.set_state(Research.choosing_mode)
    await message.answer(WELCOME, reply_markup=main_menu_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP, reply_markup=main_menu_keyboard())


@router.message(Command("mode"))
async def cmd_mode(message: Message, state: FSMContext) -> None:
    """Re-open the mode picker without losing anything else."""
    await state.set_state(Research.choosing_mode)
    await message.answer("Выберите режим:", reply_markup=main_menu_keyboard())


@router.callback_query(ModeCallback.filter())
async def on_mode_selected(
    callback: CallbackQuery,
    callback_data: ModeCallback,
    state: FSMContext,
    repo: SupabaseRepository,
) -> None:
    """Store the chosen mode and ask for the request."""
    mode = callback_data.mode
    await state.set_state(Research.waiting_query)
    await state.update_data(mode=mode.value)
    if callback.from_user is not None:
        await repo.set_current_mode(callback.from_user.id, mode)

    log.info("mode.selected", mode=mode.value)

    prompt = (
        "🏡 <b>Участки и объекты</b>\n\n"
        "Опишите, что ищете: локация, тип объекта, площадь, бюджет.\n\n"
        "<i>Например: «Участок 2–5 соток в Лимассоле, до 300 000 евро, с видом на море»</i>"
        if mode is Mode.LAND
        else "💼 <b>Инвесторы и компании</b>\n\n"
        "Опишите, кого ищете: тип инвестора или компании, регион, объём сделок, специализация.\n\n"
        "<i>Например: «Фонды, инвестирующие в жилую недвижимость Испании от 5 млн евро»</i>"
    )
    await callback.message.edit_text(  # type: ignore[union-attr]
        f"{prompt}\n\n🎤 Можно надиктовать голосовым сообщением.",
        reply_markup=mode_switch_keyboard(mode),
    )
    await callback.answer()
