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
import time
from collections import defaultdict, deque

from aiohttp import ClientError, ClientSession, ClientWSTimeout, WSMsgType, web

from bot.logging_conf import get_logger
from bot.services.facebook.tokens import TokenStore

log = get_logger(__name__)

_EXPIRED_HTML = """<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link expired</title>
<body style="font-family: sans-serif; text-align: center; padding: 4em 1em;">
<p>This link expired. Wait for a new Telegram message.</p>
</body>"""

_UNAVAILABLE_HTML = """<!doctype html>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live view unavailable</title>
<body style="font-family: sans-serif; text-align: center; padding: 4em 1em;">
<p>The browser on the server is not responding. The link is still valid --
try again in a moment.</p>
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


# noVNC builds its own WebSocket URL from ``window.location`` plus a ``path``
# setting that defaults to ``websockify`` -- so a plain ``/s/<token>/vnc.html``
# loads the page and then dials ``wss://<host>/websockify`` at the *root*,
# which this gate does not route: everything here lives under ``/s/<token>/``.
# The link therefore has to carry the prefixed path explicitly (no leading
# slash -- noVNC joins it on itself), or the viewer opens to a page that never
# connects. Tokens are ``secrets.token_urlsafe``, so they are already safe to
# drop into a query value unescaped.
#
# ``autoconnect`` skips noVNC's own "Connect" screen -- opening a tokenised
# link is the authentication, a second button to press is just one more thing
# to do one-handed on a phone. ``resize=scale`` fits the 1280x900 desktop onto
# that phone screen instead of showing the top-left corner of it.
def _viewer_path(token: str) -> str:
    return (
        f"/s/{token}/vnc.html"
        f"?path=s/{token}/websockify"
        "&autoconnect=true"
        "&resize=scale"
        "&reconnect=true"
    )


# Rate limiting: this many failed token/PIN checks per client IP within the
# window locks that IP out for the rest of the window. Not a defense against
# a determined attacker with many IPs -- just enough that guessing a 32-byte
# token or a 4-digit PIN by brute force is not a realistic path.
_RATE_LIMIT_MAX_FAILURES = 20
_RATE_LIMIT_WINDOW_SECONDS = 600


class _RateLimiter:
    def __init__(self) -> None:
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def is_blocked(self, client_ip: str) -> bool:
        now = time.time()
        recent = self._failures[client_ip]
        while recent and now - recent[0] > _RATE_LIMIT_WINDOW_SECONDS:
            recent.popleft()
        return len(recent) >= _RATE_LIMIT_MAX_FAILURES

    def record_failure(self, client_ip: str) -> None:
        self._failures[client_ip].append(time.time())


def build_gate_app(
    token_store: TokenStore,
    novnc_internal_url: str,
    pin: str | None,
) -> web.Application:
    """Build the aiohttp app; caller owns running it (see bot/main.py)."""
    rate_limiter = _RateLimiter()
    pin_verified: set[str] = set()  # tokens that have passed the PIN check
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
            return None
        if pin and token not in pin_verified:
            return None
        return token

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
        if form.get("pin") == pin:
            pin_verified.add(token)
            raise web.HTTPFound(_viewer_path(token))
        rate_limiter.record_failure(ip)
        return web.Response(
            text=_PIN_FORM_HTML.format(action=f"/s/{token}/pin"), content_type="text/html"
        )

    async def handle_entry(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        raise web.HTTPFound(_viewer_path(token))

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

    async def _proxy_websocket(request: web.Request, upstream_url: str) -> web.StreamResponse:
        ws_url = upstream_url.replace("http://", "ws://").replace("https://", "wss://")
        # noVNC asks for a subprotocol ("binary", plus "base64" on older
        # builds). A proxy that answers the handshake before knowing what the
        # upstream picked can echo back a protocol websockify never agreed to,
        # and the browser then drops the connection. So: connect upstream
        # first, and answer this side with whatever it actually chose.
        offered = [
            proto.strip()
            for proto in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
            if proto.strip()
        ]

        async with ClientSession() as session:
            try:
                ws_client = await session.ws_connect(
                    ws_url, protocols=offered, timeout=ClientWSTimeout(ws_close=10)
                )
            except (OSError, ClientError) as exc:
                # The upstream is noVNC on loopback: unreachable means the
                # browser stack died, not that the admin did anything wrong.
                log.warning("facebook.gate.upstream_unreachable", error=str(exc))
                return web.Response(text=_UNAVAILABLE_HTML, content_type="text/html", status=502)

            ws_server = web.WebSocketResponse(
                protocols=[ws_client.protocol] if ws_client.protocol else ()
            )
            await ws_server.prepare(request)

            async with ws_client:

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

                # Whichever direction ends first -- almost always the admin
                # closing the phone tab -- tears down the other. Waiting for
                # *both* (the shape this started as) leaks one connection to
                # websockify per viewing session: the upstream pump has no
                # reason to return until websockify itself times out, so the
                # handler never finishes and the socket sits there.
                pumps = [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ]
                try:
                    await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for pump in pumps:
                        pump.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
                    await ws_client.close()
                    await ws_server.close()
        return ws_server

    app = web.Application()
    app.router.add_get("/s/{token}", handle_entry)
    app.router.add_get("/s/{token}/pin", handle_pin_form)
    app.router.add_post("/s/{token}/pin", handle_pin_submit)
    app.router.add_route("*", "/s/{token}/{path:.*}", handle_proxy)
    return app
