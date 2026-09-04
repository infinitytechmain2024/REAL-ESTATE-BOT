"""Running a search and sending its results.

Both the text handler and the voice handler funnel into :func:`run_research`,
so a dictated request and a typed one behave identically from that point on.
"""

from __future__ import annotations

import asyncio
import contextlib

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.config import Settings
from bot.exceptions import BotError, PipelineTimeoutError
from bot.handlers.formatting import format_result, format_summary
from bot.keyboards.main_menu import main_menu_keyboard, mode_switch_keyboard
from bot.keyboards.result import result_keyboard
from bot.logging_conf import get_logger
from bot.middlewares.throttling import BUSY_MESSAGES, SearchSlots
from bot.models.enums import Mode
from bot.services.limits import QuotaKind, QuotaService
from bot.services.pipeline import ResearchPipeline
from bot.states import Research
from bot.utils.text import plural_ru

router = Router(name="search")
log = get_logger(__name__)

_MIN_QUERY_CHARS = 3


@router.message(Research.processing)
async def busy(message: Message) -> None:
    """Refuse a second request while one is still running."""
    await message.answer(
        "⏳ Ваш предыдущий запрос ещё обрабатывается. Дождитесь результатов, пожалуйста."
    )


@router.message(Research.waiting_query, F.text & ~F.text.startswith("/"))
async def on_text_query(
    message: Message,
    state: FSMContext,
    pipeline: ResearchPipeline,
    settings: Settings,
    slots: SearchSlots,
    quota: QuotaService,
) -> None:
    """A typed request."""
    await run_research(
        message=message,
        state=state,
        pipeline=pipeline,
        settings=settings,
        slots=slots,
        quota=quota,
        text=message.text or "",
    )


@router.message(F.text & ~F.text.startswith("/"))
async def on_text_without_mode(message: Message, state: FSMContext) -> None:
    """Text arrived before a mode was picked."""
    await state.set_state(Research.choosing_mode)
    await message.answer(
        "Сначала выберите режим — от него зависит, что и где искать:",
        reply_markup=main_menu_keyboard(),
    )


async def run_research(
    *,
    message: Message,
    state: FSMContext,
    pipeline: ResearchPipeline,
    settings: Settings,
    slots: SearchSlots,
    quota: QuotaService,
    text: str,
    transcript: str | None = None,
) -> None:
    """Run the pipeline for *text* and send everything back.

    Owns the FSM transition into and out of ``processing`` and the concurrency
    slot, so both must be released on every exit path. The order of the guards
    matters: the cheap local checks come first, and the daily quota -- which
    costs a database round trip and, once spent, cannot be given back -- is
    only consumed once the request is definitely going to run.
    """
    user = message.from_user
    if user is None:
        return

    query = (text or "").strip()
    if len(query) < _MIN_QUERY_CHARS:
        await message.answer("Запрос слишком короткий. Опишите, что именно вы ищете.")
        return

    mode = await _current_mode(state)
    if mode is None:
        await state.set_state(Research.choosing_mode)
        await message.answer("Сначала выберите режим:", reply_markup=main_menu_keyboard())
        return

    denial = slots.try_acquire(user.id)
    if denial is not None:
        log.info("search.refused", user_id=user.id, reason=denial.value)
        await message.answer(BUSY_MESSAGES[denial])
        return

    try:
        await quota.consume(user.id, QuotaKind.SEARCHES)
    except BotError as exc:
        slots.release(user.id)
        await message.answer(f"⚠️ {exc.user_message}")
        return
    except Exception:  # a leaked slot would lock the user out until a restart
        slots.release(user.id)
        log.exception("search.quota_check_failed", user_id=user.id)
        await message.answer("⚠️ Не удалось проверить лимиты. Попробуйте ещё раз.")
        return

    status = await message.answer("🧠 Разбираю запрос…")
    await state.set_state(Research.processing)

    timeout = settings.pipeline.timeout_seconds
    try:
        # Without this ceiling a provider that never answers leaves the user
        # watching a status message forever, holding a slot the whole time.
        async with asyncio.timeout(timeout):
            outcome = await pipeline.run(
                user_id=user.id,
                mode=mode,
                text=query,
                transcript=transcript,
                progress=lambda line: _update_status(status, line),
            )
    except TimeoutError:
        log.warning("search.timed_out", user_id=user.id, timeout_seconds=timeout)
        await _safe_edit(status, f"⚠️ {PipelineTimeoutError.default_user_message}")
        return
    except BotError as exc:
        log.warning("search.failed", error=str(exc))
        await _safe_edit(status, f"⚠️ {exc.user_message}")
        return
    except Exception:
        log.exception("search.crashed")
        await _safe_edit(
            status, "⚠️ Внутренняя ошибка при обработке запроса. Попробуйте ещё раз."
        )
        return
    finally:
        # Outside the timeout block on purpose: by the time this runs the
        # cancellation has been converted to TimeoutError, so these awaits are
        # not themselves cancelled and the slot is always given back.
        slots.release(user.id)
        await state.set_state(Research.waiting_query)

    await _send_results(message, status, outcome, settings, mode)


async def _send_results(message, status, outcome, settings: Settings, mode: Mode) -> None:  # type: ignore[no-untyped-def]
    """Send each result as its own message, then the closing summary."""
    total = len(outcome.results)

    if total == 0:
        await _safe_edit(
            status,
            format_summary(
                mode=mode,
                sent=0,
                hits=outcome.hits_found,
                duplicates=outcome.duplicates_skipped,
                degraded=outcome.degraded,
            ),
        )
        return

    noun = plural_ru(total, "подходящий результат", "подходящих результата", "подходящих результатов")
    await _safe_edit(status, f"✅ Нашёл {total} {noun}, отправляю…")

    sent = 0
    for index, result in enumerate(outcome.results, start=1):
        try:
            await message.answer(
                format_result(result, index, total),
                reply_markup=result_keyboard(result.id),
                disable_web_page_preview=False,
            )
            sent += 1
        except TelegramRetryAfter as exc:
            # Telegram tells us exactly how long to wait; obey and retry once.
            log.warning("send.rate_limited", retry_after=exc.retry_after)
            await asyncio.sleep(exc.retry_after)
            with contextlib.suppress(TelegramAPIError):
                await message.answer(
                    format_result(result, index, total),
                    reply_markup=result_keyboard(result.id),
                )
                sent += 1
        except TelegramAPIError as exc:
            log.warning("send.failed", url=result.url, error=str(exc))

        if settings.pipeline.send_delay_seconds:
            await asyncio.sleep(settings.pipeline.send_delay_seconds)

    await message.answer(
        format_summary(
            mode=mode,
            sent=sent,
            hits=outcome.hits_found,
            duplicates=outcome.duplicates_skipped,
            degraded=outcome.degraded,
        ),
        reply_markup=mode_switch_keyboard(mode),
    )


async def _current_mode(state: FSMContext) -> Mode | None:
    """Mode from FSM data, if one was chosen."""
    data = await state.get_data()
    raw = data.get("mode")
    if not raw:
        return None
    try:
        return Mode(raw)
    except ValueError:
        return None


async def _update_status(status: Message, line: str) -> None:
    await _safe_edit(status, line)


async def _safe_edit(status: Message, text: str) -> None:
    """Edit the status message, tolerating Telegram's complaints.

    'message is not modified' and 'message to edit not found' are both routine
    (identical progress line, or the user deleted it) and must not abort a
    search that is otherwise fine.
    """
    try:
        await status.edit_text(text)
    except TelegramAPIError as exc:
        log.debug("status.edit_failed", error=str(exc))
