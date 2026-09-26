"""Login to the social networks: LinkedIn end to end, the logged-in check on «Готово», the owner's panel."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from bot.control_plane.access import AccessDesk, MemoryAccessStore
from bot.control_plane.live_view import (
    CHECK_FAILED,
    NOT_LOGGED_IN,
    START_URLS,
    BrowserLiveClient,
    LiveViewConfig,
    LiveViewCoordinator,
)
from bot.control_plane.menu import OWNER as OWNER_MENU
from bot.control_plane.models import IncomingMessage, Reply
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore, MemoryLiveViewStore
from bot.operators import OperatorSet
from tests.test_live_view import FakeBrowser

OWNER, OPERATOR, USER = 11, 22, 31


def coordinator(store: MemoryLiveViewStore, browser: FakeBrowser) -> LiveViewCoordinator:
    return LiveViewCoordinator(store, browser, LiveViewConfig(public_url="https://1-2-3-4.sslip.io",
                                                              operator_ids=frozenset({OWNER, OPERATOR})))


def plane(store: MemoryLiveViewStore, browser: FakeBrowser) -> ControlPlane:
    operators = OperatorSet({OWNER}, {OPERATOR: "operator", USER: "user"})
    access = MemoryAccessStore()
    for uid, role in operators.approved.items():
        access.approved[uid] = (None, None, role)
    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x", operator_user_ids=frozenset({OWNER}))
    return ControlPlane(settings, MemoryControlPlaneStore(), None, lambda _: None, coordinator(store, browser),
                        access=AccessDesk(access, operators))


def callbacks(reply: Reply) -> list[str]:
    return [b.callback_data or "" for b in reply.buttons]


@pytest.mark.asyncio
async def test_linkedin_login_opens_the_login_page_and_done_needs_a_real_login() -> None:
    store, browser = MemoryLiveViewStore(), FakeBrowser()
    live = coordinator(store, browser)
    reply = await live.login(OWNER, "linkedin", "linkedin-main")
    assert reply.text.startswith("Нужен вход в LinkedIn") and reply.buttons[1].text == "Готово, я вошёл"
    session_id = next(iter(store.sessions))
    await live.open(session_id, OWNER)
    assert browser.started == ["linkedin-main@https://www.linkedin.com/login"]

    # Not logged in yet: nothing is saved and the window stays open.
    browser.signed_in = False
    assert (await live.finish(session_id, OWNER, done=True)).text == NOT_LOGGED_IN
    assert store.sessions[session_id].state == "open" and not browser.stopped and not store.completed
    assert "Вход не найден" in NOT_LOGGED_IN and "«Готово»" in NOT_LOGGED_IN

    # The check cannot run (the browser service restarted and closed the window): still nothing saved.
    browser.window = None
    assert (await live.finish(session_id, OWNER, done=True)).text == CHECK_FAILED
    assert not store.completed

    browser.window, browser.signed_in = store.sessions[session_id].profile.id, True
    done = await live.finish(session_id, OWNER, done=True)
    assert "вход в LinkedIn выполнен" in done.text and store.completed and browser.stopped
    profile = next(iter(store.profiles.values()))
    assert (profile.platform, profile.state) == ("linkedin", "ready")


@pytest.mark.asyncio
async def test_login_command_accepts_linkedin() -> None:
    store, browser = MemoryLiveViewStore(), FakeBrowser()
    control = plane(store, browser)
    reply = await control.handle_text(IncomingMessage(chat_id=OWNER, user_id=OWNER, message_id=1, text="/login linkedin"))
    assert reply and callbacks(reply)[0] == "" and reply.buttons[0].web_app_url
    assert next(iter(store.sessions.values())).profile.name == "linkedin-main"
    assert START_URLS["linkedin"] == "https://www.linkedin.com/login"
    assert any("LinkedIn" in description for command, description in OWNER_MENU if command == "login")


@pytest.mark.asyncio
async def test_browser_client_reports_the_check_as_a_boolean_or_none() -> None:
    answers = {"yes": httpx.Response(200, json={"logged_in": True}), "no": httpx.Response(200, json={"logged_in": False}),
               "gone": httpx.Response(409, text="no live window"), "odd": httpx.Response(200, json={"logged_in": "maybe"})}
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/live/check" and request.headers["Authorization"] == "Bearer tok"
        body = __import__("json").loads(request.content)
        seen.append(body)
        return answers[body["profile_id"]]

    client = BrowserLiveClient("http://browser:8090", "tok", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await client.logged_in("yes") is True
    assert await client.logged_in("no") is False
    assert await client.logged_in("gone") is None
    assert await client.logged_in("odd") is None
    await client.aclose()
    assert seen == [{"profile_id": p} for p in ("yes", "no", "gone", "odd")]


@pytest.mark.asyncio
async def test_owner_settings_offer_social_login_with_status_per_network() -> None:
    store, browser = MemoryLiveViewStore(), FakeBrowser()
    control = plane(store, browser)
    facebook = await store.ensure_profile("facebook", "facebook-main", "test")
    store.profiles[facebook.id] = replace(facebook, state="ready")
    tiktok = await store.ensure_profile("tiktok", "tiktok-main", "test")
    store.profiles[tiktok.id] = replace(tiktok, state="human_verification_required")

    settings = await control.handle_callback(OWNER, "set:list", chat_id=OWNER)
    assert settings.buttons[-1].text == "🔐 Вход в соцсети" and settings.buttons[-1].callback_data == "login:list"
    panel = await control.handle_callback(OWNER, "login:list", chat_id=OWNER)
    assert panel.text.startswith("🔐 Вход в соцсети")
    assert [b.text for b in panel.buttons] == [
        "Facebook — ✅ вошёл", "Instagram — ⚠️ нужен вход", "TikTok — ⚠️ нужен вход", "LinkedIn — ⚠️ нужен вход"]
    assert callbacks(panel) == ["login:go:facebook", "login:go:instagram", "login:go:tiktok", "login:go:linkedin"]

    # A tap is the same flow as /login linkedin.
    started = await control.handle_callback(OWNER, "login:go:linkedin", chat_id=OWNER)
    assert started.text.startswith("Нужен вход в LinkedIn")
    assert [s.profile.name for s in store.sessions.values()] == ["linkedin-main"]
    # Mini App buttons work only in a private chat.
    assert "личном чате" in (await control.handle_callback(OWNER, "login:go:tiktok", chat_id=-100)).text


@pytest.mark.asyncio
async def test_social_login_buttons_are_for_the_owner_only() -> None:
    store, browser = MemoryLiveViewStore(), FakeBrowser()
    control = plane(store, browser)
    for who in (OPERATOR, USER, 999):
        assert "устарела" in (await control.handle_callback(who, "login:list", chat_id=who)).text
        assert "устарела" in (await control.handle_callback(who, "login:go:linkedin", chat_id=who)).text
    assert "устарела" in (await control.handle_callback(OWNER, "login:go:myspace", chat_id=OWNER)).text
    assert not store.sessions
