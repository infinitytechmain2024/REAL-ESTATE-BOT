"""Token-gated HTTPS front door for the shared Facebook browser's live view.

This is the piece that lets the Telegram "Open Facebook" button work without
the admin ever running ssh, a VPN client, or anything else at a terminal: the
button is a plain HTTPS link (served publicly via a tunnel such as Tailscale
Funnel -- see the README section on that), and this app is what stands
between that public link and the noVNC server, which stays bound to
``127.0.0.1`` and is never reachable directly.

Everything under ``/s/<token>/...`` is only ever real once a valid,
unexpired token has been minted for the current incident (see
:mod:`bot.services.facebook.tokens`) -- there is no other way in. A wrong or
stale token gets a plain "this link expired" page, never a redirect to raw
noVNC and never a directory listing.

Deliberately not built here: anything that inspects or interacts with what
the admin does inside that live view. This module only proxies bytes; it has
no idea whether what is on screen is a login form, a checkpoint, or a cat
video, and that is exactly right -- the human is trusted to look at the
screen and decide.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections import defaultdict, deque

from aiohttp import ClientSession, ClientWSTimeout, WSMsgType, web

from bot.logging_conf import get_logger
from bot.services.facebook.tokens import TokenStore

log = get_logger(__name__)

_EXPIRED_HTML = """<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link expired</title>
<body style="font-family: sans-serif; text-align: center; padding: 4em 1em;">
<p>This link expired. Wait for a new Telegram message.</p>
</body>"""

_PIN_FORM_HTML = """<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Enter PIN</title>
<body style="font-family: sans-serif; text-align: center; padding: 4em 1em;">
<form method="post" action="{action}">
<p><input name="pin" type="password" inputmode="numeric" autofocus
   style="font-size: 1.5em; text-align: center; width: 6em;"></p>
<p><button type="submit" style="font-size: 1.2em;">Continue</button></p>
</form>
</body>"""

# Rate limiting: this many failed token/PIN checks per client IP within the
# window locks that IP out for the rest of the window. Not a defense against
# a determined attacker with many IPs -- just enough that guessing a 32-byte
# token or a 4-digit PIN by brute force is not a realistic path.
_RATE_LIMIT_MAX_FAILURES = 20
_RATE_LIMIT_WINDOW_SECONDS = 600

#: Names the cookie that proves *this browser* passed the PIN check. The PIN
#: exists to protect the link itself -- a forwarded message, an unlocked phone
#: -- so passing it must not open the door for everyone else holding the URL.
_PIN_COOKIE = "fb_gate_pin"


class _RateLimiter:
    """Failed attempts per client IP, within a sliding window.

    Reads never create an entry and expired entries are dropped, so scanning
    the gate with a fresh source address each time cannot grow this without
    bound -- which is the one thing an unauthenticated endpoint must not let
    a stranger do to it.
    """

    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, client_ip: str) -> deque[float] | None:
        """Prune and return this IP's failures, or None if it has none left."""
        recent = self._failures.get(client_ip)
        if recent is None:
            return None
        now = time.time()
        while recent and now - recent[0] > _RATE_LIMIT_WINDOW_SECONDS:
            recent.popleft()
        if not recent:
            del self._failures[client_ip]
            return None
        return recent

    def is_blocked(self, client_ip: str) -> bool:
        recent = self._recent(client_ip)
        return recent is not None and len(recent) >= _RATE_LIMIT_MAX_FAILURES

    def record_failure(self, client_ip: str) -> None:
        self._failures[client_ip].append(time.time())


def build_gate_app(
    token_store: TokenStore,
    novnc_internal_url: str,
    pin: str | None,
) -> web.Application:
    """Build the aiohttp app; caller owns running it (see bot/main.py)."""
    rate_limiter = _RateLimiter()
    # token -> the cookie values of browsers that passed the PIN for it.
    # Keyed by token so a new incident's token starts with nobody admitted.
    pin_sessions: dict[str, set[str]] = defaultdict(set)
    backend = novnc_internal_url.rstrip("/")

    def client_ip(request: web.Request) -> str:
        return request.remote or "unknown"

    async def require_valid_token(request: web.Request) -> str | None:
        """Returns the token if valid and (if configured) PIN-verified, else None."""
        ip = client_ip(request)
        if rate_limiter.is_blocked(ip):
            return None
        token = request.match_info["token"]
        if not await token_store.validate(token):
            rate_limiter.record_failure(ip)
            pin_sessions.pop(token, None)  # a dead token admits nobody
            return None
        if pin and not _pin_ok(request, token):
            return None
        return token

    def _pin_ok(request: web.Request, token: str) -> bool:
        """Whether *this* browser has passed the PIN for *token*."""
        cookie = request.cookies.get(_PIN_COOKIE)
        return bool(cookie) and cookie in pin_sessions.get(token, set())

    async def handle_pin_form(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        ip = client_ip(request)
        if rate_limiter.is_blocked(ip) or not await token_store.validate(token):
            return web.Response(text=_EXPIRED_HTML, content_type="text/html", status=410)
        return web.Response(
            text=_PIN_FORM_HTML.format(action=f"/s/{token}/pin"), content_type="text/html"
        )

    async def handle_pin_submit(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        ip = client_ip(request)
        if rate_limiter.is_blocked(ip) or not await token_store.validate(token):
            return web.Response(text=_EXPIRED_HTML, content_type="text/html", status=410)
        form = await request.post()
        submitted = form.get("pin")
        # Constant-time: the token is compared this way, and a 4-digit PIN is
        # the shorter secret of the two.
        if isinstance(submitted, str) and pin and secrets.compare_digest(submitted, pin):
            session_id = secrets.token_urlsafe(24)
            pin_sessions[token].add(session_id)
            response = web.HTTPFound(f"/s/{token}/vnc.html")
            # Scoped to this token's path, so it is not sent anywhere else,
            # and HttpOnly so page scripts cannot read it.
            response.set_cookie(
                _PIN_COOKIE,
                session_id,
                path=f"/s/{token}",
                httponly=True,
                samesite="Lax",
            )
            raise response
        rate_limiter.record_failure(ip)
        return web.Response(
            text=_PIN_FORM_HTML.format(action=f"/s/{token}/pin"), content_type="text/html"
        )

    async def handle_entry(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        raise web.HTTPFound(f"/s/{token}/vnc.html")

    async def handle_proxy(request: web.Request) -> web.StreamResponse:
        token = await require_valid_token(request)
        if token is None:
            if pin and await token_store.validate(request.match_info["token"]):
                # Valid token, just not PIN-verified yet -- ask for the PIN
                # rather than claiming the link itself expired.
                return await handle_pin_form(request)
            return web.Response(text=_EXPIRED_HTML, content_type="text/html", status=410)

        sub_path = request.match_info.get("path", "")
        upstream_url = f"{backend}/{sub_path}"
        if request.query_string:
            upstream_url += f"?{request.query_string}"

        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _proxy_websocket(request, upstream_url)
        return await _proxy_http(request, upstream_url)

    async def _proxy_http(request: web.Request, upstream_url: str) -> web.Response:
        headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        body = await request.read()
        async with ClientSession() as session, session.request(
            request.method, upstream_url, headers=headers, data=body or None
        ) as upstream:
            payload = await upstream.read()
            response_headers = {
                k: v
                for k, v in upstream.headers.items()
                if k.lower() not in {"content-length", "content-encoding", "transfer-encoding"}
            }
            return web.Response(
                body=payload,
                status=upstream.status,
                headers=response_headers,
            )

    async def _proxy_websocket(request: web.Request, upstream_url: str) -> web.WebSocketResponse:
        ws_server = web.WebSocketResponse()
        await ws_server.prepare(request)
        ws_url = upstream_url.replace("http://", "ws://").replace("https://", "wss://")

        async with ClientSession() as session, session.ws_connect(
            ws_url, timeout=ClientWSTimeout(ws_close=10)
        ) as ws_client:

            async def client_to_upstream() -> None:
                async for msg in ws_server:
                    if msg.type == WSMsgType.TEXT:
                        await ws_client.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await ws_client.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break

            async def upstream_to_client() -> None:
                async for msg in ws_client:
                    if msg.type == WSMsgType.TEXT:
                        await ws_server.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await ws_server.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break

            await asyncio.gather(client_to_upstream(), upstream_to_client())
        return ws_server

    app = web.Application()
    app.router.add_get("/s/{token}", handle_entry)
    app.router.add_get("/s/{token}/pin", handle_pin_form)
    app.router.add_post("/s/{token}/pin", handle_pin_submit)
    app.router.add_route("*", "/s/{token}/{path:.*}", handle_proxy)
    return app
