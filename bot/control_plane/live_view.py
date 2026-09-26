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
import logging
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote

import httpx
from aiohttp import ClientError, ClientSession, ClientWSTimeout, WSMsgType, web

from bot.control_plane.models import Button, LiveProfile, LiveSession, Reply
from bot.telegram_webapp import verify_init_data

log = logging.getLogger(__name__)

START_URLS = {
    "facebook": "https://www.facebook.com/",
    "instagram": "https://www.instagram.com/",
    "tiktok": "https://www.tiktok.com/",
    "linkedin": "https://www.linkedin.com/login",
}
PLATFORM_NAMES = {"facebook": "Facebook", "instagram": "Instagram", "tiktok": "TikTok", "linkedin": "LinkedIn"}
NOT_LOGGED_IN = "Вход не найден. Войдите в аккаунт в окне и нажмите «Готово»."
CHECK_FAILED = ("Не удалось проверить вход: окно браузера не отвечает. Откройте браузер ещё раз, "
                "войдите и нажмите «Готово», или нажмите «Закрыть» и начните вход заново.")
PROFILE_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


class LiveViewStore(Protocol):
    async def ensure_profile(self, platform: str, name: str, actor: str) -> LiveProfile: ...
    async def request_live_view(self, profile: LiveProfile, reason: str, requested_by: str, ttl_seconds: int) -> tuple[LiveSession, bool]: ...
    async def get_live_view(self, session_id: str) -> LiveSession | None: ...
    async def mark_live_view_open(self, session_id: str, user_id: int, open_seconds: int) -> LiveSession | None: ...
    async def close_live_view(self, session_id: str, state: str, actor: str, error_code: str | None = None) -> LiveSession | None: ...
    async def complete_verification(self, profile: LiveProfile, actor: str) -> None: ...
    async def profiles_needing_human(self, cooldown_seconds: int) -> list[LiveProfile]: ...
    async def expired_live_views(self) -> list[LiveSession]: ...
    async def platform_states(self) -> dict[str, str]:
        """platform -> the most usable profile state (ready, else in_use, else any)."""
        ...


class LiveBrowser(Protocol):
    async def start(self, profile: LiveProfile, url: str, minutes: int) -> str: ...
    async def stop(self, profile_id: str) -> None: ...
    async def is_open(self, profile_id: str) -> bool: ...
    async def logged_in(self, profile_id: str) -> bool | None:
        """Whether the open window's profile is signed in; None when it cannot be checked."""
        ...


class LiveViewUnavailable(RuntimeError):
    """Shown to the operator as-is."""


def is_session_id(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


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

    async def is_open(self, profile_id: str) -> bool:
        """Whether the browser service still shows this profile's window (a restart closes it)."""
        try:
            response = await self._client.get(self._url, headers=self._headers)
        except httpx.HTTPError:
            return False
        return response.status_code == 200 and response.json().get("profile_id") == profile_id

    async def logged_in(self, profile_id: str) -> bool | None:
        """The browser service reads the login cookie's presence in the open window; never its value."""
        try:
            response = await self._client.post(f"{self._url}/check", json={"profile_id": profile_id}, headers=self._headers)
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        value = response.json().get("logged_in")
        return value if isinstance(value, bool) else None

    async def stop(self, profile_id: str) -> None:
        try:
            await self._client.request("DELETE", self._url, json={"profile_id": profile_id}, headers=self._headers)
        except httpx.HTTPError:
            log.warning("telegram.live_view.stop_failed", extra={"profile_id": profile_id})

    async def aclose(self) -> None:
        await self._client.aclose()


Notifier = Callable[[int, Reply], Awaitable[None]]
BROWSER_REQUEST_SECONDS = 600
MAX_BROWSER_REQUESTS = 3


@dataclass
class _BrowserRequest:
    session_id: str
    user_id: int
    approval: str
    expires_at: float
    approved: bool = False


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
        # Browsers waiting for approval: waiting browser's cookie -> request. Lost on
        # restart, which only means opening the link again.
        self._browser: dict[str, _BrowserRequest] = {}

    @property
    def enabled(self) -> bool:
        return self.config.public_url.startswith("https://")

    def is_operator(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.config.operator_ids

    def request_reply(self, session: LiveSession, user_id: int | None = None) -> Reply:
        profile = session.profile
        platform = PLATFORM_NAMES.get(profile.platform, profile.platform)
        why = (
            f"Нужен вход в {platform}: профиль {profile.name}."
            if session.reason == "login"
            else f"{platform} попросил проверку на профиле {profile.name}; сбор на нём приостановлен."
        )
        url = f"{self.config.public_url.rstrip('/')}/live/{session.id}/"
        # The same page in Safari or a desktop browser (full size): it names the
        # recipient, who must approve that browser from Telegram before it opens.
        browser = (f"\n\nНа большом экране удобнее: скопируйте ссылку в Safari или другой браузер; перед открытием "
                   f"я попрошу подтвердить это здесь, в Telegram. Не пересылайте её.\n{url}?for={user_id}") if user_id else ""
        return Reply(
            f"{why}\n\nОткройте браузер, войдите в аккаунт или пройдите проверку сами, затем нажмите «Готово, я вошёл». "
            f"Окно открыто {self.config.open_minutes} мин.; запрос действует {self.config.request_minutes} мин.{browser}",
            (
                Button("Открыть браузер", web_app_url=url),
                Button("Готово, я вошёл", callback_data=f"live:done:{session.id}"),
                Button("Закрыть", callback_data=f"live:cancel:{session.id}"),
            ),
        )

    async def login(self, user_id: int | None, platform: str, name: str) -> Reply:
        if not self.is_operator(user_id):
            return Reply("Входить в профили браузера могут только операторы (Only operators).")
        if not self.enabled:
            return Reply("Живой браузер не настроен (not configured): задайте LIVE_VIEW_PUBLIC_URL, см. .env.example.")
        if platform not in START_URLS:
            return Reply(f"Платформа должна быть одной из (Platform must be one of): {', '.join(sorted(START_URLS))}.")
        if not 1 <= len(name) <= 64 or not set(name) <= PROFILE_NAME_CHARS:
            return Reply("Имя профиля (Profile name): только a-z, 0-9, '_' и '-'.")
        profile = await self.store.ensure_profile(platform, name, f"telegram:{user_id}")
        if profile.platform != platform:
            return Reply(f"Профиль {name} относится к {profile.platform}, а не к {platform}.")
        if profile.state in {"disabled", "retired", "in_use"}:
            return Reply(f"Профиль {name} сейчас {profile.state}; открыть его нельзя. Попробуйте позже.")
        session, _ = await self.store.request_live_view(profile, "login", f"telegram:{user_id}", self.config.request_minutes * 60)
        return self.request_reply(session, user_id)

    async def open(self, session_id: str, user_id: int) -> str:
        """Start (or resume) the window for an operator; returns the VNC password."""
        if not self.is_operator(user_id):
            raise PermissionError("not an operator")
        async with self._lock:
            session = await self.store.get_live_view(session_id)
            if session is None or session.state not in {"requested", "open"} or session.expires_at <= datetime.now(UTC):
                raise LiveViewUnavailable("this request has expired; wait for a new message or send /login")
            if session.state == "open" and session_id in self._passwords:
                if await self.browser.is_open(session.profile.id):
                    return self._passwords[session_id]
                # The browser service restarted and closed the window: open it again.
                self._passwords.pop(session_id, None)
            if session.state == "open":
                # The bot or the browser restarted while the window was open: start it afresh.
                await self.browser.stop(session.profile.id)
            password = await self.browser.start(session.profile, START_URLS.get(session.profile.platform, START_URLS["facebook"]), self.config.open_minutes)
            opened = await self.store.mark_live_view_open(session_id, user_id, self.config.open_minutes * 60) if session.state == "requested" else session
            if opened is None:
                await self.browser.stop(session.profile.id)
                raise LiveViewUnavailable("this request has expired; wait for a new message or send /login")
            self._passwords[session_id] = password
            log.info("telegram.live_view.opened", extra={"session_id": session_id, "user_id": user_id})
            return password

    # --- the same window in an ordinary browser, approved from Telegram ----------------

    def _browser_prune(self) -> None:
        now = time.time()
        for key in [k for k, r in self._browser.items() if r.expires_at < now]:
            del self._browser[key]

    async def request_browser(self, session_id: str, for_user: int, client: str) -> str:
        """The link was opened outside Telegram: ask the named operator; returns the waiting cookie."""
        if not self.is_operator(for_user) or self.notifier is None:
            raise LiveViewUnavailable("this link is not valid; open it from the Telegram message")
        session = await self.store.get_live_view(session_id)
        if session is None or session.state not in {"requested", "open"} or session.expires_at <= datetime.now(UTC):
            raise LiveViewUnavailable("this request has expired; wait for a new message or send /login")
        self._browser_prune()
        if sum(1 for r in self._browser.values() if r.session_id == session_id) >= MAX_BROWSER_REQUESTS:
            raise LiveViewUnavailable("too many attempts with this link; send /login again")
        cookie, approval = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
        self._browser[cookie] = _BrowserRequest(session_id, for_user, approval, time.time() + BROWSER_REQUEST_SECONDS)
        await self.notifier(for_user, Reply(
            f"The browser link for profile {session.profile.name} was opened in a browser ({client[:120]}).\n\n"
            "If that was you, press Approve and go back to that browser. If not, ignore this message: nothing opens without it.",
            (Button("Approve browser login", callback_data=f"live:approve:{approval}"),),
        ))
        return cookie

    def approve_browser(self, approval: str, user_id: int | None) -> Reply:
        """The operator pressed Approve in the chat: Telegram vouches for who pressed it."""
        self._browser_prune()
        for request in self._browser.values():
            if secrets.compare_digest(request.approval, approval):
                if request.user_id != user_id or not self.is_operator(user_id):
                    return Reply("Only the person the link was sent to can approve it.")
                request.approved = True
                return Reply("Approved. Go back to your browser; the window opens there in a few seconds.")
        return Reply("This approval has expired. Open the link in the browser again.")

    def browser_status(self, session_id: str, cookie: str | None) -> int | None:
        """For the waiting browser: the approving operator's ID once approved (once), else None."""
        self._browser_prune()
        request = self._browser.get(cookie or "")
        if request is None or request.session_id != session_id:
            raise LiveViewUnavailable("this browser request has expired; open the link again")
        if not request.approved:
            return None
        del self._browser[cookie or ""]
        return request.user_id

    async def finish(self, session_id: str, user_id: int | None, *, done: bool) -> Reply:
        if not self.is_operator(user_id):
            return Reply("Закрывать окно браузера могут только операторы (Only operators).")
        async with self._lock:
            session = await self.store.get_live_view(session_id)
            if session is None or session.state not in {"requested", "open"}:
                return Reply("Это окно браузера уже закрыто (already closed).")
            if done and session.state != "open":
                return Reply("Сначала откройте браузер и войдите в аккаунт, затем нажмите «Готово».")
            if done:
                # The profile becomes ready only with a real login in it; the window stays open otherwise.
                signed = await self.browser.logged_in(session.profile.id)
                if signed is False:
                    return Reply(NOT_LOGGED_IN)
                if signed is None and session.profile.platform in PLATFORM_NAMES:
                    return Reply(CHECK_FAILED)
            if session.state == "open":
                # Closing the browser writes the login into the profile.
                await self.browser.stop(session.profile.id)
            self._passwords.pop(session_id, None)
            actor = f"telegram:{user_id}"
            if done:
                await self.store.complete_verification(session.profile, actor)
            await self.store.close_live_view(session_id, "completed" if done else "cancelled", actor)
        platform = PLATFORM_NAMES.get(session.profile.platform, session.profile.platform)
        if done:
            return Reply(f"Сохранено: вход в {platform} выполнен, профиль {session.profile.name} готов (is ready). "
                         "Приостановленный сбор продолжится.")
        return Reply(f"Закрыто (Closed). Профиль {session.profile.name} оставлен как был ({session.profile.state}).")

    async def login_panel(self) -> Reply:
        """«🔐 Вход в соцсети»: one button per platform with its login status; a tap starts /login <platform>."""
        states = await self.store.platform_states()
        buttons = tuple(
            Button(f"{name} — {'✅ вошёл' if states.get(platform) in {'ready', 'in_use'} else '⚠️ нужен вход'}",
                   callback_data=f"login:go:{platform}")
            for platform, name in PLATFORM_NAMES.items()
        )
        return Reply("🔐 Вход в соцсети\n\nВыберите сеть: я открою окно браузера, вы войдёте в аккаунт сами "
                     "и нажмёте «Готово, я вошёл». Логин и пароль бот не видит и не хранит.", buttons)

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
            for operator in sorted(self.config.operator_ids):
                try:
                    await self.notifier(operator, self.request_reply(session, operator))
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
<style>body{{font-family:sans-serif;text-align:center;padding:3em 1em}}button{{font-size:1.1em;padding:.6em 1.2em}}</style>
</head><body><div id="m">{outside}</div>
<script>
const tg = window.Telegram && window.Telegram.WebApp;
const say = (t) => document.getElementById('m').textContent = t;
if (tg && tg.initData) {{
  say('Opening the browser, this can take up to a minute\u2026');
  tg.ready(); tg.expand();
  // Use the whole screen, and keep swipes inside the remote page from closing the app.
  try {{ if (tg.requestFullscreen) tg.requestFullscreen(); }} catch (e) {{}}
  try {{ if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); }} catch (e) {{}}
  fetch('{auth}', {{method: 'POST', headers: {{'Content-Type': 'text/plain'}}, body: tg.initData, credentials: 'same-origin'}})
    .then(r => r.json().then(j => [r.ok, j]))
    .then(([ok, j]) => ok ? location.replace(j.viewer) : say(j.error || 'Not allowed.'))
    .catch(() => say('The server did not answer. Try again in a moment.'));
}}
</script></body></html>"""

_OUTSIDE = (
    "<p>This page is opened outside Telegram. Continue here and the bot will ask you to approve this browser first.</p>"
    "<form method='post' action='/live/{session}/browser?for={user}'><button type='submit'>Continue in this browser</button></form>"
)

_WAIT = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="3"><title>Waiting for approval</title>
<style>body{font-family:sans-serif;text-align:center;padding:3em 1em}</style></head>
<body><h1>Check Telegram</h1><p>The bot sent you an <b>Approve browser login</b> button. Press it, then come back here;
this page checks every few seconds.</p></body></html>"""

_PENDING_COOKIE = "live_view_pending"

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
        named = request.query.get("for", "")
        outside = (_OUTSIDE.format(session=session_id, user=int(named)) if named.isdigit()
                   else "Open this page from the button or link in the Telegram bot.")
        return web.Response(text=_PAGE.format(auth=f"/live/{session_id}/auth", outside=outside), content_type="text/html",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    def _admit(session_id: str, user_id: int, password: str) -> tuple[str, str, int]:
        now = time.time()
        for stale in [key for key, (_, _, until) in admitted.items() if until < now]:
            del admitted[stale]
        cookie = secrets.token_urlsafe(32)
        max_age = coordinator.config.open_minutes * 60
        admitted[cookie] = (session_id, user_id, now + max_age)
        return cookie, _viewer_path(session_id, password), max_age

    async def browser_request(request: web.Request) -> web.StreamResponse:
        session_id, named = request.match_info["session"], request.query.get("for", "")
        forwarded = request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()
        agent = " ".join(request.headers.get("User-Agent", "unknown browser").split())[:100]
        if not named.isdigit():
            return web.Response(text="Open this page from the link in the Telegram bot.", status=403)
        try:
            cookie = await coordinator.request_browser(session_id, int(named), f"{agent}; IP {forwarded or 'unknown'}")
        except LiveViewUnavailable as exc:
            return web.Response(text=f"Cannot continue: {exc}.", status=403)
        response = web.HTTPSeeOther(f"/live/{session_id}/wait")
        response.set_cookie(_PENDING_COOKIE, cookie, path=f"/live/{session_id}/", httponly=True, secure=True,
                            samesite="Strict", max_age=BROWSER_REQUEST_SECONDS)
        return response

    async def browser_wait(request: web.Request) -> web.StreamResponse:
        session_id = request.match_info["session"]
        try:
            user_id = coordinator.browser_status(session_id, request.cookies.get(_PENDING_COOKIE))
            if user_id is None:
                return web.Response(text=_WAIT, content_type="text/html", headers={"Cache-Control": "no-store"})
            password = await coordinator.open(session_id, user_id)
        except (LiveViewUnavailable, PermissionError) as exc:
            return web.Response(text=f"Cannot open the browser: {exc}.", status=403)
        cookie, viewer, max_age = _admit(session_id, user_id, password)
        response = web.HTTPSeeOther(viewer)
        response.set_cookie(_COOKIE, cookie, path=f"/live/{session_id}/", httponly=True, secure=True, samesite="Lax", max_age=max_age)
        response.del_cookie(_PENDING_COOKIE, path=f"/live/{session_id}/")
        return response

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
        cookie, viewer, max_age = _admit(session_id, user_id, password)
        response = web.json_response({"viewer": viewer})
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
            # noVNC can still be binding for a moment right after the window opens.
            for attempt in range(4):
                try:
                    async with client.get(upstream) as response:
                        body = await response.read()
                        content_type = response.headers.get("Content-Type", "application/octet-stream")
                    break
                except (OSError, ClientError):
                    if attempt == 3:
                        return web.Response(text="The browser is not running. Press Close and ask for a new window.", status=502)
                    await asyncio.sleep(1)
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
    app.router.add_post("/live/{session}/browser", browser_request)
    app.router.add_get("/live/{session}/wait", browser_wait)
    app.router.add_get("/live/{session}/{path:.+}", proxy)
    return app
