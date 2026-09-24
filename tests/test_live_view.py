"""Telegram-driven live browser: Mini App signature, coordinator rules, the gate."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from bot.control_plane.live_view import (
    LiveViewConfig,
    LiveViewCoordinator,
    LiveViewUnavailable,
    create_gate_app,
    verify_init_data,
)
from bot.control_plane.models import IncomingMessage, LiveProfile
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore, MemoryLiveViewStore

TOKEN = "123456:bot-token"
OPERATOR, STRANGER = 11, 999


def init_data(user_id: int, *, token: str = TOKEN, auth_date: int | None = None, tamper: bool = False) -> str:
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAE", "user": json.dumps({"id": user_id, "first_name": "Op"})}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["user"] = json.dumps({"id": OPERATOR, "first_name": "Op"})
    return urlencode(fields)


class FakeBrowser:
    def __init__(self, fail: bool = False) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.fail = fail

    async def start(self, profile: LiveProfile, url: str, minutes: int) -> str:
        if self.fail:
            raise LiveViewUnavailable("the browser is busy")
        self.started.append(f"{profile.name}@{url}")
        return f"pw{len(self.started)}"

    async def stop(self, profile_id: str) -> None:
        self.stopped.append(profile_id)


def coordinator(store: MemoryLiveViewStore | None = None, browser: FakeBrowser | None = None, notifier=None, public_url: str = "https://1-2-3-4.sslip.io") -> LiveViewCoordinator:
    return LiveViewCoordinator(
        store or MemoryLiveViewStore(), browser or FakeBrowser(),
        LiveViewConfig(public_url=public_url, operator_ids=frozenset({OPERATOR})), notifier,
    )


# --- Telegram Mini App signature ----------------------------------------------


def test_signed_init_data_yields_the_user() -> None:
    assert verify_init_data(init_data(OPERATOR), TOKEN) == OPERATOR


@pytest.mark.parametrize(
    "data",
    [
        init_data(STRANGER, tamper=True),  # identity swapped after signing
        init_data(OPERATOR, token="999:another-bot"),  # signed for a different bot
        init_data(OPERATOR, auth_date=int(time.time()) - 3600),  # replayed
        "user=%7B%22id%22%3A11%7D&auth_date=1",  # unsigned
        "not a query string",
        "",
    ],
)
def test_bad_init_data_is_rejected(data: str) -> None:
    assert verify_init_data(data, TOKEN) is None


# --- coordinator ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_creates_the_profile_and_offers_the_mini_app() -> None:
    store = MemoryLiveViewStore()
    reply = await coordinator(store).login(OPERATOR, "facebook", "facebook-main")
    session = next(iter(store.sessions.values()))
    assert session.reason == "login" and session.profile.name == "facebook-main"
    assert [b.text for b in reply.buttons] == ["Open browser", "Done, I am logged in", "Close"]
    assert reply.buttons[0].web_app_url == f"https://1-2-3-4.sslip.io/live/{session.id}/"
    assert reply.buttons[1].callback_data == f"live:done:{session.id}"
    # Asking again reuses the open request rather than stacking a second one.
    await coordinator(store).login(OPERATOR, "facebook", "facebook-main")
    assert len(store.sessions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "platform", "name", "public_url", "expected"),
    [
        (STRANGER, "facebook", "facebook-main", "https://x.sslip.io", "Only operators"),
        (None, "facebook", "facebook-main", "https://x.sslip.io", "Only operators"),
        (OPERATOR, "facebook", "facebook-main", "", "not configured"),
        (OPERATOR, "website", "w", "https://x.sslip.io", "Platform must be"),
        (OPERATOR, "facebook", "Bad Name!", "https://x.sslip.io", "Profile name"),
    ],
)
async def test_login_refusals(user: int | None, platform: str, name: str, public_url: str, expected: str) -> None:
    store = MemoryLiveViewStore()
    reply = await coordinator(store, public_url=public_url).login(user, platform, name)
    assert expected in reply.text and not reply.buttons and not store.sessions


@pytest.mark.asyncio
async def test_open_starts_the_browser_once_and_done_saves_the_profile() -> None:
    store, browser = MemoryLiveViewStore(), FakeBrowser()
    live = coordinator(store, browser)
    await live.login(OPERATOR, "facebook", "facebook-main")
    session_id = next(iter(store.sessions))

    # Done before anyone opened the window is refused.
    early = await live.finish(session_id, OPERATOR, done=True)
    assert "Open the browser" in early.text and store.sessions[session_id].state == "requested"

    assert await live.open(session_id, OPERATOR) == "pw1"
    assert await live.open(session_id, OPERATOR) == "pw1"  # reopening the page reuses the window
    assert browser.started == ["facebook-main@https://www.facebook.com/"]
    assert store.sessions[session_id].state == "open" and store.sessions[session_id].opened_by == OPERATOR

    reply = await live.finish(session_id, OPERATOR, done=True)
    assert "is ready" in reply.text
    assert store.sessions[session_id].state == "completed"
    assert browser.stopped and store.completed
    assert "already closed" in (await live.finish(session_id, OPERATOR, done=True)).text


@pytest.mark.asyncio
async def test_open_is_for_operators_and_live_requests_only() -> None:
    store = MemoryLiveViewStore()
    live = coordinator(store)
    await live.login(OPERATOR, "facebook", "facebook-main")
    session_id = next(iter(store.sessions))
    with pytest.raises(PermissionError):
        await live.open(session_id, STRANGER)
    store.sessions[session_id] = replace(store.sessions[session_id], expires_at=datetime.now(UTC) - timedelta(seconds=1))
    with pytest.raises(LiveViewUnavailable, match="expired"):
        await live.open(session_id, OPERATOR)


@pytest.mark.asyncio
async def test_a_restart_while_open_restarts_the_window() -> None:
    store, browser = MemoryLiveViewStore(), FakeBrowser()
    await coordinator(store, browser).login(OPERATOR, "facebook", "facebook-main")
    session_id = next(iter(store.sessions))
    await coordinator(store, browser).open(session_id, OPERATOR)
    # A new coordinator has no password in memory, as after a restart.
    assert await coordinator(store, browser).open(session_id, OPERATOR) == "pw2"
    assert len(browser.stopped) == 1


@pytest.mark.asyncio
async def test_cancel_leaves_the_profile_untouched() -> None:
    store, browser = MemoryLiveViewStore(), FakeBrowser()
    live = coordinator(store, browser)
    await live.login(OPERATOR, "facebook", "facebook-main")
    session_id = next(iter(store.sessions))
    await live.open(session_id, OPERATOR)
    reply = await live.finish(session_id, OPERATOR, done=False)
    assert "left as provisioned" in reply.text
    assert store.sessions[session_id].state == "cancelled" and not store.completed and browser.stopped
    assert "Only operators" in (await live.finish(session_id, STRANGER, done=False)).text


@pytest.mark.asyncio
async def test_checkpoints_are_announced_once_to_every_operator() -> None:
    store = MemoryLiveViewStore()
    profile = await store.ensure_profile("facebook", "facebook-main", "test")
    store.profiles[profile.id] = replace(profile, state="human_verification_required")
    sent: list[int] = []

    async def notify(chat_id: int, reply: object) -> None:
        if chat_id == 12:
            raise RuntimeError("chat not found")  # an operator who never started the bot
        sent.append(chat_id)

    live = LiveViewCoordinator(store, FakeBrowser(), LiveViewConfig("https://x.sslip.io", frozenset({OPERATOR, 12, 13})), notify)
    await live.tick()
    await live.tick()
    assert sent == [OPERATOR, 13]
    session = next(iter(store.sessions.values()))
    assert session.reason == "checkpoint" and session.state == "requested"


@pytest.mark.asyncio
async def test_a_completed_login_does_not_delay_the_next_checkpoint() -> None:
    store = MemoryLiveViewStore()
    sent: list[int] = []

    async def notify(chat_id: int, reply: object) -> None:
        sent.append(chat_id)

    live = LiveViewCoordinator(store, FakeBrowser(), LiveViewConfig("https://x.sslip.io", frozenset({OPERATOR})), notify)
    await live.login(OPERATOR, "facebook", "facebook-main")
    session_id = next(iter(store.sessions))
    await live.open(session_id, OPERATOR)
    await live.finish(session_id, OPERATOR, done=True)
    profile = store.profiles[store.sessions[session_id].profile.id]
    store.profiles[profile.id] = replace(profile, state="human_verification_required")
    await live.tick()
    assert sent == [OPERATOR]


@pytest.mark.asyncio
async def test_a_closed_request_is_not_repeated_at_once() -> None:
    store = MemoryLiveViewStore()
    sent: list[int] = []

    async def notify(chat_id: int, reply: object) -> None:
        sent.append(chat_id)

    profile = await store.ensure_profile("facebook", "facebook-main", "t")
    store.profiles[profile.id] = replace(profile, state="human_verification_required")
    live = LiveViewCoordinator(store, FakeBrowser(), LiveViewConfig("https://x.sslip.io", frozenset({OPERATOR})), notify)
    await live.tick()
    await live.finish(next(iter(store.sessions)), OPERATOR, done=False)
    await live.tick()
    assert sent == [OPERATOR]


@pytest.mark.asyncio
async def test_expired_sessions_are_closed_and_their_window_stopped() -> None:
    store, browser = MemoryLiveViewStore(), FakeBrowser()
    live = coordinator(store, browser)
    await live.login(OPERATOR, "facebook", "facebook-main")
    session_id = next(iter(store.sessions))
    await live.open(session_id, OPERATOR)
    store.sessions[session_id] = replace(store.sessions[session_id], expires_at=datetime.now(UTC) - timedelta(seconds=1))
    await live.tick()
    assert store.sessions[session_id].state == "expired" and browser.stopped


# --- control plane commands -------------------------------------------------------


def plane(store: MemoryLiveViewStore) -> ControlPlane:
    settings = ControlPlaneSettings(telegram_token=TOKEN, database_url="postgresql://x", operator_user_ids=frozenset({OPERATOR}))
    return ControlPlane(settings, MemoryControlPlaneStore(), None, lambda _: None, coordinator(store))


@pytest.mark.asyncio
async def test_login_command_and_buttons() -> None:
    store = MemoryLiveViewStore()
    control = plane(store)
    reply = await control.handle_text(IncomingMessage(chat_id=OPERATOR, user_id=OPERATOR, message_id=1, text="/login"))
    assert reply and reply.buttons and next(iter(store.sessions.values())).profile.name == "facebook-main"
    group = await control.handle_text(IncomingMessage(chat_id=-100, user_id=OPERATOR, message_id=2, text="/login facebook"))
    assert group and "private chat" in group.text
    session_id = next(iter(store.sessions))
    closed = await control.handle_callback(OPERATOR, f"live:cancel:{session_id}")
    assert "Closed" in closed.text
    for bad in ("live:done:not-a-uuid", "other:done:x", "live:explode:" + session_id, ""):
        assert "no longer valid" in (await control.handle_callback(OPERATOR, bad)).text


# --- the gate -------------------------------------------------------------------------


async def fake_novnc(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Upgrade", "").lower() == "websocket":
        ws = web.WebSocketResponse(protocols=("binary",))
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                await ws.send_bytes(b"echo:" + message.data)
        return ws
    return web.Response(text=f"novnc:{request.match_info['path']}", content_type="text/html")


@pytest.mark.asyncio
async def test_gate_admits_only_a_signed_operator_and_proxies_novnc() -> None:
    upstream_app = web.Application()
    upstream_app.router.add_get("/{path:.*}", fake_novnc)
    upstream = TestServer(upstream_app)
    await upstream.start_server()

    store, browser = MemoryLiveViewStore(), FakeBrowser()
    live = coordinator(store, browser)
    await live.login(OPERATOR, "facebook", "facebook-main")
    session_id = next(iter(store.sessions))
    client = TestClient(TestServer(create_gate_app(live, TOKEN, str(upstream.make_url("")))))
    await client.start_server()
    try:
        page = await client.get(f"/live/{session_id}/")
        assert page.status == 200 and "telegram-web-app.js" in await page.text()
        assert (await client.get("/live/not-a-uuid/")).status == 404

        # Without a cookie, noVNC is not reachable, whatever the path.
        assert (await client.get(f"/live/{session_id}/vnc.html")).status == 403

        assert (await client.post(f"/live/{session_id}/auth", data="garbage")).status == 403
        assert (await client.post(f"/live/{session_id}/auth", data=init_data(STRANGER))).status == 403
        assert browser.started == []

        response = await client.post(f"/live/{session_id}/auth", data=init_data(OPERATOR))
        assert response.status == 200
        viewer = (await response.json())["viewer"]
        assert viewer.startswith(f"/live/{session_id}/vnc.html?path=live/{session_id}/websockify")
        assert "password=pw1" in viewer and browser.started
        cookie = response.cookies["live_view"]
        # HTTPS-only, script-proof, and scoped to this session's path.
        assert cookie["secure"] and cookie["httponly"] and cookie["path"] == f"/live/{session_id}/"
        # The test server is plain HTTP on an IP, where a client does not
        # replay a Secure cookie by itself; send it as a browser would over HTTPS.
        admitted = {"Cookie": f"live_view={cookie.value}"}

        static = await client.get(f"/live/{session_id}/vnc.html", headers=admitted)
        assert static.status == 200 and await static.text() == "novnc:vnc.html"

        async with client.ws_connect(f"/live/{session_id}/websockify", protocols=("binary",), headers=admitted) as ws:
            await ws.send_bytes(b"frame")
            reply = await ws.receive()
            assert reply.data == b"echo:frame"

        # The cookie belongs to this session only.
        other, _ = await store.request_live_view(await store.ensure_profile("facebook", "facebook-b", "t"), "login", "t", 600)
        assert (await client.get(f"/live/{other.id}/vnc.html", headers=admitted)).status == 403
    finally:
        await client.close()
        await upstream.close()


@pytest.mark.asyncio
async def test_gate_reports_a_busy_browser() -> None:
    store = MemoryLiveViewStore()
    live = coordinator(store, FakeBrowser(fail=True))
    await live.login(OPERATOR, "facebook", "facebook-main")
    session_id = next(iter(store.sessions))
    client = TestClient(TestServer(create_gate_app(live, TOKEN, "http://127.0.0.1:9")))
    await client.start_server()
    try:
        response = await client.post(f"/live/{session_id}/auth", data=init_data(OPERATOR))
        assert response.status == 409 and "busy" in (await response.json())["error"]
    finally:
        await client.close()
