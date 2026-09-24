"""The token gate in front of the Facebook browser's live view.

This is the only remotely reachable surface in the project, and what is behind
it is a browser already logged into Facebook. Everything here is about one
question: can someone who should not be looking at that browser get to it?

The tests drive a real aiohttp server with a real upstream behind it, so
"passed the gate" means bytes actually came back from upstream, not that a
handler returned the right constant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from aiohttp import ClientSession, CookieJar, web
from aiohttp.client_exceptions import WSServerHandshakeError
from aiohttp.test_utils import TestServer

from bot.services.facebook.gate import build_gate_app
from bot.services.facebook.tokens import TokenStore

UPSTREAM_BODY = "UPSTREAM OK"
PIN = "1234"


@pytest.fixture
async def upstream() -> AsyncIterator[str]:
    """A stand-in for noVNC, so reaching it is observable.

    Speaks WebSocket as well as HTTP: the real noVNC carries the whole VNC
    session over a socket, so an auth check that covers only plain requests
    would leave the part that actually drives the browser unguarded.
    """

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_str(UPSTREAM_BODY)
        await socket.close()
        return socket

    async def handler(request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await ws_handler(request)
        return web.Response(text=UPSTREAM_BODY)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    yield f"http://127.0.0.1:{server.port}"
    await server.close()


@pytest.fixture
async def store(tmp_path) -> TokenStore:
    return TokenStore(str(tmp_path / "token.json"))


async def _gate(store: TokenStore, upstream: str, pin: str | None) -> TestServer:
    """One running gate. Several clients may share it -- which is the point.

    Building a fresh app per client would give each its own server-side state
    and quietly make the PIN-sharing test vacuous.
    """
    server = TestServer(build_gate_app(store, novnc_internal_url=upstream, pin=pin))
    await server.start_server()
    return server


def _browser(server: TestServer) -> ClientSession:
    """A client with its own cookie jar, i.e. a distinct browser.

    ``unsafe=True`` only because the test server is on 127.0.0.1: aiohttp's
    jar discards cookies from bare IP hosts, which would silently drop the
    gate's PIN cookie and make these tests measure the wrong thing.
    """
    return ClientSession(base_url=str(server.make_url("/")), cookie_jar=CookieJar(unsafe=True))


# --- without a PIN configured ----------------------------------------------


async def test_valid_token_reaches_upstream(store, upstream) -> None:
    token = await store.create(ttl_seconds=60)
    server = await _gate(store, upstream, pin=None)
    async with _browser(server) as client:
        response = await client.get(f"/s/{token}/vnc.html")
        assert response.status == 200
        assert await response.text() == UPSTREAM_BODY
    await server.close()


async def test_unknown_token_never_reaches_upstream(store, upstream) -> None:
    await store.create(ttl_seconds=60)
    server = await _gate(store, upstream, pin=None)
    async with _browser(server) as client:
        response = await client.get("/s/not-the-token/vnc.html")
        assert response.status == 410
        assert UPSTREAM_BODY not in await response.text()
    await server.close()


async def test_invalidated_token_stops_working(store, upstream) -> None:
    """Recovery kills the link; the old URL must die with it."""
    token = await store.create(ttl_seconds=60)
    await store.invalidate()
    server = await _gate(store, upstream, pin=None)
    async with _browser(server) as client:
        response = await client.get(f"/s/{token}/vnc.html")
        assert response.status == 410
    await server.close()


# --- with a PIN configured --------------------------------------------------


async def test_token_alone_does_not_reach_upstream_when_a_pin_is_set(store, upstream) -> None:
    token = await store.create(ttl_seconds=60)
    server = await _gate(store, upstream, pin=PIN)
    async with _browser(server) as client:
        response = await client.get(f"/s/{token}/vnc.html")
        body = await response.text()
        assert UPSTREAM_BODY not in body
        assert "pin" in body.lower()
    await server.close()


async def test_correct_pin_lets_that_client_through(store, upstream) -> None:
    token = await store.create(ttl_seconds=60)
    server = await _gate(store, upstream, pin=PIN)
    async with _browser(server) as client:
        await client.post(f"/s/{token}/pin", data={"pin": PIN})
        response = await client.get(f"/s/{token}/vnc.html")
        assert await response.text() == UPSTREAM_BODY
    await server.close()


async def test_a_second_client_still_needs_the_pin(store, upstream) -> None:
    """The PIN must protect the link, not merely the first person to use it.

    Whoever holds the link — a forwarded message, an unlocked phone — must
    still be stopped by the PIN even after the admin has already entered it.
    """
    token = await store.create(ttl_seconds=60)
    server = await _gate(store, upstream, pin=PIN)
    async with _browser(server) as admin, _browser(server) as stranger:
        await admin.post(f"/s/{token}/pin", data={"pin": PIN})
        assert await (await admin.get(f"/s/{token}/vnc.html")).text() == UPSTREAM_BODY

        leaked = await stranger.get(f"/s/{token}/vnc.html")
        assert UPSTREAM_BODY not in await leaked.text(), "the PIN was bypassed by link alone"
    await server.close()


async def test_wrong_pin_does_not_let_that_client_through(store, upstream) -> None:
    token = await store.create(ttl_seconds=60)
    server = await _gate(store, upstream, pin=PIN)
    async with _browser(server) as client:
        await client.post(f"/s/{token}/pin", data={"pin": "9999"})
        response = await client.get(f"/s/{token}/vnc.html")
        assert UPSTREAM_BODY not in await response.text()
    await server.close()


async def test_repeated_wrong_pins_lock_the_client_out(store, upstream) -> None:
    token = await store.create(ttl_seconds=60)
    server = await _gate(store, upstream, pin=PIN)
    async with _browser(server) as client:
        for _ in range(25):
            await client.post(f"/s/{token}/pin", data={"pin": "9999"})
        # Even the right PIN now gets nowhere while the window is open.
        await client.post(f"/s/{token}/pin", data={"pin": PIN})
        response = await client.get(f"/s/{token}/vnc.html")
        assert UPSTREAM_BODY not in await response.text()
    await server.close()


# --- the socket, which is what actually drives the browser ------------------


async def test_websocket_needs_a_valid_token(store, upstream) -> None:
    """noVNC carries the session over a socket; the gate must guard that too."""
    await store.create(ttl_seconds=60)
    server = await _gate(store, upstream, pin=None)
    async with _browser(server) as client:
        try:
            async with client.ws_connect("/s/wrong-token/websockify") as socket:
                message = await socket.receive()
                assert UPSTREAM_BODY not in str(message.data), "socket reached upstream"
        except WSServerHandshakeError:
            pass  # refused outright, which is the desired outcome
    await server.close()


async def test_websocket_needs_the_pin_too(store, upstream) -> None:
    """A valid token without the PIN must not open the socket either."""
    token = await store.create(ttl_seconds=60)
    server = await _gate(store, upstream, pin=PIN)
    async with _browser(server) as client:
        try:
            async with client.ws_connect(f"/s/{token}/websockify") as socket:
                message = await socket.receive()
                assert UPSTREAM_BODY not in str(message.data), "socket bypassed the PIN"
        except WSServerHandshakeError:
            pass
    await server.close()


async def test_websocket_works_once_the_pin_is_passed(store, upstream) -> None:
    """And the admin's own socket must still connect, or the gate is useless."""
    token = await store.create(ttl_seconds=60)
    server = await _gate(store, upstream, pin=PIN)
    async with _browser(server) as client:
        await client.post(f"/s/{token}/pin", data={"pin": PIN})
        async with client.ws_connect(f"/s/{token}/websockify") as socket:
            message = await socket.receive()
            assert UPSTREAM_BODY in str(message.data)
    await server.close()


# --- the PIN cookie is itself a credential ---------------------------------


async def _pin_cookie_header(store, upstream, **gate_kwargs) -> str:
    """The raw Set-Cookie the gate issues when the PIN is accepted."""
    token = await store.create(ttl_seconds=60)
    app = build_gate_app(store, novnc_internal_url=upstream, pin=PIN, **gate_kwargs)
    server = TestServer(app)
    await server.start_server()
    async with _browser(server) as client:
        response = await client.post(
            f"/s/{token}/pin", data={"pin": PIN}, allow_redirects=False
        )
        header = response.headers.get("Set-Cookie", "")
    await server.close()
    return header


async def test_published_gate_marks_the_pin_cookie_https_only(store, upstream) -> None:
    """Without Secure, a single plaintext request leaks the session.

    The cookie is all that separates a browser that passed the PIN from one
    merely holding the link, and SameSite=Lax still sends it on a top-level
    navigation -- so an http:// redirect would hand it to anyone on the path.
    """
    header = await _pin_cookie_header(store, upstream, secure_cookie=True)
    assert "Secure" in header, header
    assert "HttpOnly" in header


async def test_loopback_gate_does_not_mark_it_secure(store, upstream) -> None:
    """Plain http on 127.0.0.1 has no network to intercept, and some clients
    will not return a Secure cookie over it."""
    header = await _pin_cookie_header(store, upstream, secure_cookie=False)
    assert "Secure" not in header, header


async def test_pin_cookie_does_not_outlive_the_link(store, upstream) -> None:
    header = await _pin_cookie_header(store, upstream, cookie_max_age=1800)
    assert "Max-Age=1800" in header, header
