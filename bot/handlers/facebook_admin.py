"""Admin-only: connect and check the shared Facebook browser session.

This is not a per-user feature. There is exactly one Facebook session, owned
by the bot's operator(s) -- see ``bot/services/facebook/browser.py`` for why
(one shared account, one browser, chosen deliberately over letting every
Telegram user bring their own Facebook login). Only the Telegram IDs listed
in ``FACEBOOK_ADMIN_TELEGRAM_IDS`` can use ``/facebook``; everyone else gets
silence rather than an error, so the command's existence is not advertised.

"Connect" is a URL button: it opens a private, token-gated, short-lived link
to the live browser view (see ``bot/services/facebook/gate.py``), reached
through a public tunnel (Tailscale Funnel -- see the README) in front of
noVNC. The admin never runs a terminal command or types anything technical;
they tap the button, sign in (or clear a checkpoint) in that window, and a
background watcher notices the moment the session is healthy again and sends
exactly one confirmation -- see ``_watch_for_recovery`` below. If
``FACEBOOK_DESKTOP_PUBLIC_BASE`` is not configured, there is no link to give
out, and the fallback message says to use the browser window directly on the
machine the bot runs on.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import Settings
from bot.keyboards.facebook_admin import FacebookAdminCallback, facebook_admin_keyboard
from bot.logging_conf import get_logger
from bot.services.facebook import FacebookSession, SessionState, TokenStore

router = Router(name="facebook_admin")
log = get_logger(__name__)

OPEN_BUTTON_TEXT = "Открыть Facebook"

STATE_LABEL = {
    SessionState.HEALTHY: "✅ Подключено",
    SessionState.LOGIN_NEEDED: "🔒 Нужен вход",
    SessionState.HUMAN_REQUIRED: "⚠️ Нужна проверка",
    SessionState.AUTO_LOGIN_ATTEMPT: "⏳ Пробую войти автоматически…",
}

# How often the watcher polls, and how long it keeps trying before giving up
# and telling the admin instead of polling forever in the background.
_WATCH_INTERVAL_SECONDS = 10
_WATCH_TIMEOUT_SECONDS = 15 * 60

# One watcher at a time. A second tap while one is already running re-uses
# it rather than stacking up duplicate pollers/alerts.
_watcher_task: asyncio.Task[None] | None = None

# In-memory only -- good enough for "how long has this been the state" on a
# best-effort status line; resets on restart, which just means the line is
# blank until the next observed change, not wrong.
_last_known_state: SessionState | None = None
_last_change_at: datetime | None = None


def _is_admin(user_id: int | None, settings: Settings) -> bool:
    ids = settings.facebook.admin_telegram_ids
    return user_id is not None and bool(ids) and str(user_id) in ids


def _note_state(state: SessionState) -> None:
    """Record when the observed state last changed, for the status line."""
    global _last_known_state, _last_change_at
    if state != _last_known_state:
        _last_known_state = state
        _last_change_at = datetime.now(UTC)


def _format_last_change() -> str:
    if _last_change_at is None:
        return ""
    minutes = int((datetime.now(UTC) - _last_change_at).total_seconds() // 60)
    if minutes < 1:
        return "только что"
    if minutes == 1:
        return "1 минуту назад"
    return f"{minutes} минут назад"


async def _open_button(
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


async def _watch_for_recovery(
    facebook_session: FacebookSession,
    token_store: TokenStore | None,
    settings: Settings,
    bot: Bot,
    chat_id: int,
) -> None:
    """Poll until the session is healthy again, then send exactly one alert.

    Runs as a detached background task started from the callback handler
    below. Never raises into the event loop's default handler on its own
    account: a failed check_state() call is logged and treated as "still not
    recovered" rather than crashing the watcher. On recovery, invalidates the
    live-view token so the old link stops working the moment it is no longer
    needed, matching "close the page" in the message the admin gets.
    """
    elapsed = 0
    while elapsed < _WATCH_TIMEOUT_SECONDS:
        await asyncio.sleep(_WATCH_INTERVAL_SECONDS)
        elapsed += _WATCH_INTERVAL_SECONDS
        try:
            state = await facebook_session.check_state()
        except Exception:
            log.exception("facebook.admin.watch_check_failed")
            continue
        _note_state(state)
        if state == SessionState.HEALTHY:
            log.info("facebook.admin.watch_recovered", chat_id=chat_id, elapsed_seconds=elapsed)
            if token_store is not None:
                await token_store.invalidate()
            await bot.send_message(chat_id, "Готово. Страницу можно закрыть. Бот продолжит работу.")
            return

    log.warning("facebook.admin.watch_timed_out", chat_id=chat_id)
    keyboard = await _open_button(settings, token_store)
    text = "Facebook так и не подтвердил вход за 15 минут."
    if keyboard is not None:
        text += " Нажмите «Открыть Facebook» ещё раз."
    else:
        text += " Проверьте окно браузера на машине, где запущен бот."
    await bot.send_message(chat_id, text, reply_markup=keyboard)


def _start_watcher(
    facebook_session: FacebookSession,
    token_store: TokenStore | None,
    settings: Settings,
    bot: Bot,
    chat_id: int,
) -> None:
    global _watcher_task
    if _watcher_task is not None and not _watcher_task.done():
        return
    _watcher_task = asyncio.create_task(
        _watch_for_recovery(facebook_session, token_store, settings, bot, chat_id)
    )


@router.message(Command("facebook"))
async def cmd_facebook(
    message: Message, settings: Settings, facebook_session: FacebookSession | None
) -> None:
    """Entry point: /facebook. Silently ignored for anyone not an admin."""
    if message.from_user is None or not _is_admin(message.from_user.id, settings):
        return

    if not settings.facebook.enabled or facebook_session is None:
        await message.answer(
            "Facebook-модуль выключен. Задайте FACEBOOK_ENABLED=true, "
            "FACEBOOK_GROUP_URLS и перезапустите бота, чтобы включить его."
        )
        return

    await message.answer(
        "Управление подключением к Facebook — общий аккаунт бота, не ваш личный.",
        reply_markup=facebook_admin_keyboard(),
    )


@router.callback_query(FacebookAdminCallback.filter())
async def on_facebook_admin_action(
    query: CallbackQuery,
    callback_data: FacebookAdminCallback,
    settings: Settings,
    facebook_session: FacebookSession | None,
    facebook_token_store: TokenStore | None,
) -> None:
    if query.from_user is None or not _is_admin(query.from_user.id, settings):
        await query.answer()
        return
    if facebook_session is None or query.message is None:
        await query.answer("Facebook-модуль выключен.", show_alert=True)
        return

    # Idempotent: launches/attaches the browser on first use, no-ops after.
    await facebook_session.start()

    if callback_data.action == "status":
        await query.answer("Проверяю…")
        state = await facebook_session.check_state()
        _note_state(state)
        log.info("facebook.admin.status_checked", admin_id=query.from_user.id, state=state.value)

        label = STATE_LABEL.get(state, state.value)
        since = _format_last_change()
        text = f"{label} ({since})" if since else label

        keyboard = None
        if state != SessionState.HEALTHY:
            keyboard = await _open_button(settings, facebook_token_store)
        await query.message.answer(text, reply_markup=keyboard)
        return

    if callback_data.action == "start_login":
        await query.answer("Проверяю…")
        state = await facebook_session.check_state()
        _note_state(state)
        log.info("facebook.admin.login_started", admin_id=query.from_user.id, state=state.value)

        if state == SessionState.HEALTHY:
            await query.message.answer("✅ Уже подключено — можно ничего не делать.")
            return

        keyboard = await _open_button(settings, facebook_token_store)
        if keyboard is not None:
            text = (
                "Facebook просит вас войти.\n\n"
                "Нажмите «Открыть Facebook», войдите и подождите, пока не увидите свою "
                "страницу или группы. Потом можно закрыть страницу — бот заметит сам и "
                "напишет, когда всё готово."
            )
        else:
            text = (
                "Facebook просит вас войти, а публичная ссылка не настроена "
                "(FACEBOOK_DESKTOP_PUBLIC_BASE). Войдите прямо в окне браузера на машине, "
                "где запущен бот."
            )
        await query.message.answer(text, reply_markup=keyboard)

        if query.bot is not None:
            _start_watcher(
                facebook_session, facebook_token_store, settings, query.bot, query.message.chat.id
            )
        return

    await query.answer()
