"""Telegram-driven live browser sessions for Facebook login and checkpoints.

Flow:

1. A profile needs a human: an operator sends ``/login``, or a collector
   marked it ``human_verification_required`` after a checkpoint.
2. The bot messages the operator(s) with an "Open browser" button. It is a
   Telegram Mini App, so Telegram signs the user's identity into the page.
3. The gate below verifies that signature and the operator allowlist, asks
   the browser service to open the profile's page with a one-time VNC
   password, and proxies noVNC to that operator only.
4. The human logs in or clears the checkpoint by hand and presses "Done";
   the window closes, the login is saved in the profile, and the profile and
   its verification holds return to ready.

The bot never types, clicks or solves anything inside the window.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import parse_qsl, quote

import httpx
from aiohttp import ClientError, ClientSession, ClientWSTimeout, WSMsgType, web

from bot.control_plane.models import Button, LiveProfile, LiveSession, Reply

log = logging.getLogger(__name__)

START_URLS = {
    "facebook": "https://www.facebook.com/",
    "instagram": "https://www.instagram.com/",
    "tiktok": "https://www.tiktok.com/",
}
PROFILE_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")
# initData is minted when the Mini App opens; older data is a replay.
INIT_DATA_MAX_AGE_SECONDS = 600


class LiveViewStore(Protocol):
    async def ensure_profile(self, platform: str, name: str, actor: str) -> LiveProfile: ...
    async def request_live_view(self, profile: LiveProfile, reason: str, requested_by: str, ttl_seconds: int) -> tuple[LiveSession, bool]: ...
    async def get_live_view(self, session_id: str) -> LiveSession | None: ...
    async def mark_live_view_open(self, session_id: str, user_id: int, open_seconds: int) -> LiveSession | None: ...
    async def close_live_view(self, session_id: str, state: str, actor: str, error_code: str | None = None) -> LiveSession | None: ...
    async def complete_verification(self, profile: LiveProfile, actor: str) -> None: ...
    async def profiles_needing_human(self, cooldown_seconds: int) -> list[LiveProfile]: ...
    async def expired_live_views(self) -> list[LiveSession]: ...


class LiveBrowser(Protocol):
    async def start(self, profile: LiveProfile, url: str, minutes: int) -> str: ...
    async def stop(self, profile_id: str) -> None: ...


class LiveViewUnavailable(RuntimeError):
    """Shown to the operator as-is."""


def is_session_id(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def verify_init_data(init_data: str, bot_token: str, *, now: float | None = None, max_age: int = INIT_DATA_MAX_AGE_SECONDS) -> int | None:
    """Return the Telegram user id Telegram signed into Mini App ``initData``.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", "")
    if not received:
        return None
    check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(pairs.get("auth_date", "0"))
        user_id = int(json.loads(pairs.get("user", "{}"))["id"])
    except (ValueError, KeyError, TypeError):
        return None
    if abs((time.time() if now is None else now) - auth_date) > max_age:
        return None
    return user_id


class BrowserLiveClient:
    """The browser service's internal live-view API."""

    def __init__(self, base_url: str, token: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._url = f"{base_url.rstrip('/')}/v1/live"
        self._headers = {"Authorization": f"Bearer {token}"}
        # Opening a page may take up to a minute of navigation.
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(90))

    async def start(self, profile: LiveProfile, url: str, minutes: int) -> str:
        body = {"profile_id": profile.id, "profile_name": profile.name, "platform": profile.platform, "url": url, "minutes": minutes}
        try:
            response = await self._client.post(self._url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise LiveViewUnavailable("the browser service is not reachable") from exc
        if response.status_code == 409:
            raise LiveViewUnavailable(f"the browser is busy: {response.text[:200]}")
        if response.status_code != 200:
            raise LiveViewUnavailable(f"the browser service returned HTTP {response.status_code}")
        return str(response.json()["password"])

    async def stop(self, profile_id: str) -> None:
        try:
            await self._client.request("DELETE", self._url, json={"profile_id": profile_id}, headers=self._headers)
        except httpx.HTTPError:
            log.warning("telegram.live_view.stop_failed", extra={"profile_id": profile_id})

    async def aclose(self) -> None:
        await self._client.aclose()


Notifier = Callable[[int, Reply], Awaitable[None]]


@dataclass
class LiveViewConfig:
    public_url: str  # e.g. https://203-0-113-7.sslip.io ; empty disables the feature
    operator_ids: frozenset[int]
    open_minutes: int = 20
    request_minutes: int = 60


class LiveViewCoordinator:
    def __init__(self, store: LiveViewStore, browser: LiveBrowser, config: LiveViewConfig, notifier: Notifier | None = None) -> None:
        self.store, self.browser, self.config, self.notifier = store, browser, config, notifier
        # One-time VNC passwords of open sessions; never stored in the database.
        self._passwords: dict[str, str] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.config.public_url.startswith("https://")

    def is_operator(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.config.operator_ids

    def request_reply(self, session: LiveSession) -> Reply:
        profile = session.profile
        why = (
            f"Profile {profile.name} ({profile.platform}) needs a login."
            if session.reason == "login"
            else f"{profile.platform.capitalize()} asked for verification on profile {profile.name}; collection on it is paused."
        )
        url = f"{self.config.public_url.rstrip('/')}/live/{session.id}/"
        return Reply(
            f"{why}\n\nOpen the browser, log in or pass the check yourself, then press Done. "
            f"The window stays open for {self.config.open_minutes} minutes; this request lapses in "
            f"{self.config.request_minutes} minutes.",
            (
                Button("Open browser", web_app_url=url),
                Button("Done, I am logged in", callback_data=f"live:done:{session.id}"),
                Button("Close", callback_data=f"live:cancel:{session.id}"),
            ),
        )

    async def login(self, user_id: int | None, platform: str, name: str) -> Reply:
        if not self.is_operator(user_id):
            return Reply("Only operators can log a browser profile in.")
        if not self.enabled:
            return Reply("The live browser is not configured: set LIVE_VIEW_PUBLIC_URL (see .env.example).")
        if platform not in START_URLS:
            return Reply(f"Platform must be one of: {', '.join(sorted(START_URLS))}.")
        if not 1 <= len(name) <= 64 or not set(name) <= PROFILE_NAME_CHARS:
            return Reply("Profile name may contain only a-z, 0-9, '_' and '-'.")
        profile = await self.store.ensure_profile(platform, name, f"telegram:{user_id}")
        if profile.platform != platform:
            return Reply(f"Profile {name} belongs to {profile.platform}, not {platform}.")
        if profile.state in {"disabled", "retired", "in_use"}:
            return Reply(f"Profile {name} is {profile.state}; it cannot be opened now.")
        session, _ = await self.store.request_live_view(profile, "login", f"telegram:{user_id}", self.config.request_minutes * 60)
        return self.request_reply(session)

    async def open(self, session_id: str, user_id: int) -> str:
        """Start (or resume) the window for an operator; returns the VNC password."""
        if not self.is_operator(user_id):
            raise PermissionError("not an operator")
        async with self._lock:
            session = await self.store.get_live_view(session_id)
            if session is None or session.state not in {"requested", "open"} or session.expires_at <= datetime.now(UTC):
                raise LiveViewUnavailable("this request has expired; wait for a new message or send /login")
            if session.state == "open" and session_id in self._passwords:
                return self._passwords[session_id]
            if session.state == "open":
                # The bot restarted while the window was open: start it afresh.
                await self.browser.stop(session.profile.id)
            password = await self.browser.start(session.profile, START_URLS.get(session.profile.platform, START_URLS["facebook"]), self.config.open_minutes)
            opened = await self.store.mark_live_view_open(session_id, user_id, self.config.open_minutes * 60) if session.state == "requested" else session
            if opened is None:
                await self.browser.stop(session.profile.id)
                raise LiveViewUnavailable("this request has expired; wait for a new message or send /login")
            self._passwords[session_id] = password
            log.info("telegram.live_view.opened", extra={"session_id": session_id, "user_id": user_id})
            return password

    async def finish(self, session_id: str, user_id: int | None, *, done: bool) -> Reply:
        if not self.is_operator(user_id):
            return Reply("Only operators can close a browser session.")
        async with self._lock:
            session = await self.store.get_live_view(session_id)
            if session is None or session.state not in {"requested", "open"}:
                return Reply("This browser session is already closed.")
            if done and session.state != "open":
                return Reply("Open the browser and log in first, then press Done.")
            if session.state == "open":
                # Closing the browser writes the login into the profile.
                await self.browser.stop(session.profile.id)
            self._passwords.pop(session_id, None)
            actor = f"telegram:{user_id}"
            if done:
                await self.store.complete_verification(session.profile, actor)
            await self.store.close_live_view(session_id, "completed" if done else "cancelled", actor)
        if done:
            return Reply(f"Saved. Profile {session.profile.name} is ready; paused {session.profile.platform} sources are active again.")
        return Reply(f"Closed. Profile {session.profile.name} was left as {session.profile.state}.")

    async def tick(self) -> None:
        """Expire lapsed sessions, then ask operators about profiles that need a human."""
        for session in await self.store.expired_live_views():
            async with self._lock:
                if session.state == "open":
                    await self.browser.stop(session.profile.id)
                self._passwords.pop(session.id, None)
                await self.store.close_live_view(session.id, "expired", "system")
        if not self.enabled or self.notifier is None:
            return
        for profile in await self.store.profiles_needing_human(self.config.request_minutes * 60):
            session, created = await self.store.request_live_view(profile, "checkpoint", "system", self.config.request_minutes * 60)
            if not created:
                continue
            reply = self.request_reply(session)
            for operator in sorted(self.config.operator_ids):
                try:
                    await self.notifier(operator, reply)
                except Exception:  # noqa: BLE001 - one operator who never started the bot must not stop the rest
                    log.warning("telegram.live_view.notify_failed", extra={"user_id": operator})

    async def run_forever(self, poll_seconds: float, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("telegram.live_view.tick_failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poll_seconds)


# --- the gate: Mini App page, signature check, noVNC proxy ------------------

_PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Browser</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>body{{font-family:sans-serif;text-align:center;padding:3em 1em}}</style>
</head><body><p id="m">Opening the browser, this can take up to a minute&hellip;</p>
<script>
const tg = window.Telegram && window.Telegram.WebApp;
const say = (t) => document.getElementById('m').textContent = t;
if (!tg || !tg.initData) {{
  say('Open this page from the button in the Telegram bot.');
}} else {{
  tg.ready(); tg.expand();
  fetch('{auth}', {{method: 'POST', headers: {{'Content-Type': 'text/plain'}}, body: tg.initData, credentials: 'same-origin'}})
    .then(r => r.json().then(j => [r.ok, j]))
    .then(([ok, j]) => ok ? location.replace(j.viewer) : say(j.error || 'Not allowed.'))
    .catch(() => say('The server did not answer. Try again in a moment.'));
}}
</script></body></html>"""

_COOKIE = "live_view"


def _viewer_path(session_id: str, password: str) -> str:
    return (
        f"/live/{session_id}/vnc.html?path={quote(f'live/{session_id}/websockify')}"
        f"&password={quote(password)}&autoconnect=true&resize=scale&reconnect=true"
    )


def create_gate_app(coordinator: LiveViewCoordinator, bot_token: str, novnc_url: str) -> web.Application:
    backend = novnc_url.rstrip("/")
    # cookie value -> (session id, user id, expiry epoch); lost on restart, which only
    # means pressing the button again.
    admitted: dict[str, tuple[str, int, float]] = {}

    def cookie_ok(request: web.Request, session_id: str) -> bool:
        entry = admitted.get(request.cookies.get(_COOKIE, ""))
        if entry is None or entry[0] != session_id or entry[2] < time.time():
            return False
        return coordinator.is_operator(entry[1])

    @web.middleware
    async def known_ids_only(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        if not is_session_id(request.match_info.get("session", "")):
            raise web.HTTPNotFound()
        return await handler(request)

    async def page(request: web.Request) -> web.Response:
        session_id = request.match_info["session"]
        return web.Response(text=_PAGE.format(auth=f"/live/{session_id}/auth"), content_type="text/html",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def auth(request: web.Request) -> web.Response:
        session_id = request.match_info["session"]
        user_id = verify_init_data((await request.text())[:4096], bot_token)
        if user_id is None:
            return web.json_response({"error": "Open this page from the Telegram bot."}, status=403)
        if not coordinator.is_operator(user_id):
            return web.json_response({"error": "Only operators can open the browser."}, status=403)
        try:
            password = await coordinator.open(session_id, user_id)
        except LiveViewUnavailable as exc:
            return web.json_response({"error": f"Cannot open the browser: {exc}."}, status=409)
        now = time.time()
        for stale in [key for key, (_, _, until) in admitted.items() if until < now]:
            del admitted[stale]
        cookie = secrets.token_urlsafe(32)
        max_age = coordinator.config.open_minutes * 60
        admitted[cookie] = (session_id, user_id, time.time() + max_age)
        response = web.json_response({"viewer": _viewer_path(session_id, password)})
        response.set_cookie(_COOKIE, cookie, path=f"/live/{session_id}/", httponly=True, secure=True, samesite="Lax", max_age=max_age)
        return response

    async def proxy(request: web.Request) -> web.StreamResponse:
        session_id, sub_path = request.match_info["session"], request.match_info["path"]
        if not cookie_ok(request, session_id):
            return web.Response(text="This browser link is not open for you. Use the button in the Telegram bot.", status=403)
        upstream = f"{backend}/{sub_path}"
        if request.query_string:
            upstream += f"?{request.query_string}"
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _proxy_websocket(request, upstream)
        async with ClientSession() as client:
            try:
                async with client.get(upstream) as response:
                    body = await response.read()
                    content_type = response.headers.get("Content-Type", "application/octet-stream")
            except (OSError, ClientError):
                return web.Response(text="The browser is not running. Press Close and ask for a new window.", status=502)
        return web.Response(body=body, status=response.status, headers={"Content-Type": content_type, "Cache-Control": "no-store"})

    async def _proxy_websocket(request: web.Request, upstream: str) -> web.StreamResponse:
        offered = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        async with ClientSession() as client:
            try:
                ws_client = await client.ws_connect(upstream.replace("http://", "ws://", 1), protocols=offered, timeout=ClientWSTimeout(ws_close=10))
            except (OSError, ClientError):
                return web.Response(text="The browser is not running.", status=502)
            # Answer with the subprotocol websockify actually chose.
            ws_server = web.WebSocketResponse(protocols=[ws_client.protocol] if ws_client.protocol else ())
            await ws_server.prepare(request)
            async with ws_client:
                async def pump(source: object, sink: object) -> None:
                    async for message in source:  # type: ignore[attr-defined]
                        if message.type == WSMsgType.BINARY:
                            await sink.send_bytes(message.data)  # type: ignore[attr-defined]
                        elif message.type == WSMsgType.TEXT:
                            await sink.send_str(message.data)  # type: ignore[attr-defined]
                        else:
                            break

                pumps = [asyncio.create_task(pump(ws_server, ws_client)), asyncio.create_task(pump(ws_client, ws_server))]
                try:
                    # Either side closing (usually the phone) ends both.
                    await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in pumps:
                        task.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)
                    await ws_client.close()
                    await ws_server.close()
        return ws_server

    app = web.Application(client_max_size=64 * 1024, middlewares=[known_ids_only])
    app.router.add_get("/live/{session}/", page)
    app.router.add_post("/live/{session}/auth", auth)
    app.router.add_get("/live/{session}/{path:.+}", proxy)
    return app
