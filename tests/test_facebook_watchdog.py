import asyncio

import pytest

from bot.config import FacebookSettings, Settings
from bot.services.facebook.browser import FacebookSession, SessionState
from tests.conftest import FakeBot, FakeContext, FakePage, FakeSession, FakeTokenStore


def make_watchdog(session):
    from bot.services.facebook.watchdog import FacebookWatchdog

    settings = Settings(
        facebook=FacebookSettings(
            enabled=True,
            admin_telegram_ids=["1"],
            watchdog_interval_seconds=0.01,
        )
    )
    bot, tokens = FakeBot(), FakeTokenStore()
    return FacebookWatchdog(session, tokens, settings, bot), bot, tokens


async def test_incident_and_recovery_are_each_announced_once():
    session = FakeSession(
        [
            SessionState.HEALTHY,
            SessionState.LOGIN_NEEDED,
            SessionState.LOGIN_NEEDED,
            SessionState.HEALTHY,
            SessionState.HEALTHY,
        ]
    )
    watchdog, bot, tokens = make_watchdog(session)
    for _ in range(5):
        await watchdog.tick()
    assert len(bot.messages) == 2
    assert "Facebook" in bot.messages[0]
    assert "Готово" in bot.messages[1]
    assert tokens.invalidated == 1
    assert session.observed_unlocked == session.probe_calls == session.start_calls == 0


async def test_idle_watchdog_does_not_start_or_observe_browser():
    session = FakeSession([])
    session.has_live_context = False
    watchdog, bot, _ = make_watchdog(session)
    await watchdog.tick()
    assert session.observe_calls == session.start_calls == 0
    assert not bot.messages


async def test_real_session_observation_never_navigates():
    session = FacebookSession(FacebookSettings())
    page = FakePage(selectors={'form[action*="login"]': 1})
    session._context = FakeContext(pages=[page])
    session._page = page
    watchdog, _, _ = make_watchdog(session)
    await watchdog.tick()
    assert page.goto_calls == []


async def test_background_loop_can_be_cancelled():
    session = FakeSession([SessionState.LOGIN_NEEDED] * 10)
    watchdog, bot, _ = make_watchdog(session)
    task = asyncio.create_task(watchdog.run())
    for _ in range(20):
        if bot.messages:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(bot.messages) == 1


@pytest.mark.parametrize("enabled, admins", [(True, ["1"]), (True, []), (False, [])])
async def test_services_start_and_stop_watchdog_without_browser(monkeypatch, enabled, admins):
    from unittest.mock import AsyncMock, Mock

    from bot import main
    from bot.config import ParserSettings

    warning = Mock()
    monkeypatch.setattr(main.log, "warning", warning)
    monkeypatch.setattr(main, "_start_facebook_gate", AsyncMock())
    monkeypatch.setattr(main.LLMManager, "__init__", lambda self, settings: None)
    monkeypatch.setattr(main.LLMManager, "aclose", AsyncMock())
    monkeypatch.setattr(main.STTManager, "__init__", lambda self, settings: None)
    monkeypatch.setattr(main.STTManager, "aclose", AsyncMock())
    monkeypatch.setattr(main, "SearXNGClient", Mock(return_value=Mock(aclose=AsyncMock())))
    monkeypatch.setattr(
        main,
        "SupabaseRepository",
        Mock(
            return_value=Mock(
                connect=AsyncMock(),
                aclose=AsyncMock(),
                current_facebook_incident=AsyncMock(return_value=None),
            )
        ),
    )
    monkeypatch.setattr(main.FacebookSession, "start", AsyncMock())
    services = await main.build_services(
        Settings(
            facebook=FacebookSettings(enabled=enabled, admin_telegram_ids=admins),
            parser=ParserSettings(enabled=False),
        ),
        bot=FakeBot(),
    )
    task = services.facebook_watchdog_task
    assert (task is not None and not task.done()) if enabled else task is None
    if enabled and not admins:
        warning.assert_called_once_with(
            "startup.facebook_no_admins",
            detail="Facebook alerts have no recipients",
        )
    main.FacebookSession.start.assert_not_awaited()
    await services.aclose()
    assert task is None or task.cancelled()


async def test_token_storage_failure_cannot_silence_alerts():
    from unittest.mock import AsyncMock

    watchdog, bot, tokens = make_watchdog(
        FakeSession(
            [
                SessionState.LOGIN_NEEDED,
                SessionState.HEALTHY,
            ]
        )
    )
    watchdog.settings.facebook.desktop_public_base = "https://example.com"
    tokens.get_or_create = AsyncMock(side_effect=OSError("read-only disk"))
    tokens.invalidate = AsyncMock(side_effect=OSError("read-only disk"))
    await watchdog.tick()
    await watchdog.tick()
    assert len(bot.messages) == 2
    assert "Готово" in bot.messages[1]


async def test_manual_login_does_not_stack_a_recovery_watcher(monkeypatch):
    from unittest.mock import AsyncMock, Mock

    from bot.handlers import facebook_admin
    from bot.keyboards.facebook_admin import FacebookAdminCallback

    session = FakeSession([SessionState.LOGIN_NEEDED] * 3 + [SessionState.HEALTHY])
    watchdog, bot, tokens = make_watchdog(session)
    await watchdog.tick()
    start = Mock()
    monkeypatch.setattr(facebook_admin, "_start_watcher", start)
    query = Mock(
        from_user=Mock(id=1), answer=AsyncMock(), bot=bot, message=Mock(answer=AsyncMock())
    )
    await facebook_admin.on_facebook_admin_action(
        query,
        FacebookAdminCallback(action="start_login"),
        watchdog.settings,
        session,
        tokens,
        watchdog,
    )
    await watchdog.tick()
    assert len(bot.messages) == 2
    start.assert_not_called()
