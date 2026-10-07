"""The verification page, opened as a Telegram Mini App under /verify.

The same link also works in an ordinary browser (Safari): there is no
Telegram signature there, so the bot first asks the link's recipient to
approve that browser with a signed Mini App button, and only the waiting
browser that asked (it holds a pending cookie) gets the page session.

Caddy serves it over HTTPS; the service itself has no host port. Access is
layered: Telegram's signature on the Mini App says which user pressed the
button (checked before anything else), that user must be an operator, the
single-use link must have been sent to that very user, and the page session
it becomes is an HttpOnly, Secure, SameSite=Strict cookie bound to job, user
and profile. Every button carries a CSRF token.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import logging
import secrets
import uuid
from collections.abc import Awaitable, Callable
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientWSTimeout, WSMsgType, web

from bot.telegram_webapp import TELEGRAM_WEB_APP_JS

from .service import BROWSER_REQUEST_SECONDS, AccessDenied, ActionRefused, VerificationService

log = logging.getLogger(__name__)
PREFIX = "/verify"
COOKIE = "verification_session"
PENDING_COOKIE = "verification_pending"
ACTIONS = ("claim", "view", "solve", "resume", "cancel", "fail")

_BASE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
# Job pages carry no script at all; the noVNC viewer is framed from our own origin.
_PAGE_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-src 'self'; form-action 'self'; frame-ancestors 'self' https://web.telegram.org; base-uri 'none'"

_STYLE = """body{font-family:system-ui,sans-serif;max-width:60em;margin:1em auto;padding:0 1em;color:#1b1b1b}
h1{font-size:1.3em}dt{font-weight:600}dd{margin:0 0 .5em}form{display:inline-block;margin:.25em .5em .25em 0}
button{font-size:1em;padding:.5em 1em}.msg{padding:.6em;background:#eef;border-radius:4px}
.warn{padding:.6em;background:#fee;border-radius:4px}iframe{width:100%;height:75vh;border:1px solid #999}
table{border-collapse:collapse;font-size:.9em}td{padding:.2em .6em;border-bottom:1px solid #ddd}"""


def _html(title: str, body: str, *, status: int = 200, csp: str = _PAGE_CSP, head: str = "") -> web.Response:
    return web.Response(
        text=f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style>{head}</head><body>{body}</body></html>",
        content_type="text/html", status=status, headers={**_BASE_HEADERS, "Content-Security-Policy": csp},
    )


def _denied(message: str, status: int = 403) -> web.Response:
    return _html("Not allowed", f"<h1>Not allowed</h1><p>{html.escape(message)}</p>", status=status)


def _viewer(job_id: str, password: str) -> str:
    return (f"{PREFIX}/jobs/{job_id}/live/vnc.html?path={quote(f'verify/jobs/{job_id}/live/websockify')}"
            f"&password={quote(password)}&autoconnect=true&resize=scale&reconnect=true")


def create_app(service: VerificationService, novnc_url: str) -> web.Application:
    backend = novnc_url.rstrip("/")

    @web.middleware
    async def known_ids(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        job_id = request.match_info.get("job")
        if job_id is not None:
            try:
                if str(uuid.UUID(job_id)) != job_id:
                    raise ValueError
            except ValueError:
                return _denied("Unknown job.", 404)
        return await handler(request)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    def _mini_app(title: str, waiting: str, fallback: str, post_to: str, done: str, *, full: bool = False) -> web.Response:
        """A page that posts Telegram's signed initData, then follows ``next`` or shows ``done``."""
        nonce = secrets.token_urlsafe(16)
        script = f"""<script nonce="{nonce}">
const tg = window.Telegram && window.Telegram.WebApp;
const say = (t) => document.getElementById('m').textContent = t;
if (tg && tg.initData) {{
  document.getElementById('m').textContent = {json.dumps(waiting)};
  tg.ready(); tg.expand();
  {"try { if (tg.requestFullscreen) tg.requestFullscreen(); } catch (e) {} try { if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch (e) {}" if full else ""}
  fetch({json.dumps(post_to)}, {{method: 'POST', headers: {{'Content-Type': 'text/plain'}}, body: tg.initData, credentials: 'same-origin'}})
    .then(r => r.json().then(j => [r.ok, j]))
    .then(([ok, j]) => !ok ? say(j.error || 'Not allowed.') : j.next ? location.replace(j.next) : (say({json.dumps(done)}), setTimeout(() => tg.close(), 1500)))
    .catch(() => say('The server did not answer. Try again in a moment.'));
}}
</script>"""
        csp = (f"default-src 'none'; script-src 'nonce-{nonce}' https://telegram.org; connect-src 'self'; style-src 'unsafe-inline'; "
               "form-action 'self'; frame-ancestors 'self' https://web.telegram.org; base-uri 'none'")
        return _html(title, f"<div id='m'>{fallback}</div>{script}", csp=csp, head=f"<script src='{TELEGRAM_WEB_APP_JS}'></script>")

    async def opener(request: web.Request) -> web.Response:
        """First page of the link: signed Mini App inside Telegram, approval flow anywhere else."""
        token = request.match_info["token"]
        outside = (
            "<h1>Verification</h1><p>This link is opened outside Telegram. Continue here and the bot will ask you "
            "to approve this browser in Telegram first.</p>"
            f"<form method='post' action='{PREFIX}/v/{html.escape(token)}/browser'><button type='submit'>Continue in this browser</button></form>"
        )
        return _mini_app("Verification", "Checking who you are…", outside, f"{PREFIX}/v/{token}/auth", "", full=True)

    async def browser_request(request: web.Request) -> web.StreamResponse:
        token = request.match_info["token"]
        forwarded = request.headers.get("X-Forwarded-For", request.remote or "").split(",")[0].strip()
        agent = " ".join(request.headers.get("User-Agent", "unknown browser").split())[:100]
        try:
            cookie = await service.request_browser(token, f"{agent}; IP {forwarded or 'unknown'}")
        except AccessDenied as exc:
            return _denied(str(exc))
        response = web.HTTPSeeOther(f"{PREFIX}/v/{token}/wait")
        response.set_cookie(PENDING_COOKIE, cookie, path=f"{PREFIX}/", httponly=True, secure=True, samesite="Strict",
                            max_age=BROWSER_REQUEST_SECONDS)
        return response

    async def browser_wait(request: web.Request) -> web.StreamResponse:
        try:
            opened = await service.browser_status(request.match_info["token"], request.cookies.get(PENDING_COOKIE))
        except AccessDenied as exc:
            return _denied(str(exc))
        if opened is None:
            return _html("Waiting for approval", "<h1>Check Telegram</h1><p>The bot sent you an <b>Approve browser login</b> "
                         "button. Press it, then come back here; this page checks every few seconds.</p>",
                         head="<meta http-equiv='refresh' content='3'>")
        response = web.HTTPSeeOther(f"{PREFIX}/jobs/{opened.session.job_id}")
        response.set_cookie(COOKIE, opened.cookie, path=f"{PREFIX}/", httponly=True, secure=True, samesite="Strict",
                            max_age=service.config.session_minutes * 60)
        response.del_cookie(PENDING_COOKIE, path=f"{PREFIX}/")
        return response

    async def approval_page(request: web.Request) -> web.Response:
        approval = request.match_info["approval"]
        return _mini_app("Approve browser", "Approving…", "<p>Open this from the Approve button in the Telegram bot.</p>",
                         f"{PREFIX}/a/{approval}/auth", "Approved. Go back to your browser.")

    async def approval_auth(request: web.Request) -> web.Response:
        try:
            await service.approve_browser(request.match_info["approval"], (await request.text())[:4096])
        except AccessDenied as exc:
            return web.json_response({"error": str(exc)}, status=403, headers=_BASE_HEADERS)
        return web.json_response({"approved": True}, headers=_BASE_HEADERS)

    async def auth(request: web.Request) -> web.Response:
        try:
            opened = await service.open(request.match_info["token"], (await request.text())[:4096])
        except AccessDenied as exc:
            return web.json_response({"error": str(exc)}, status=403, headers=_BASE_HEADERS)
        response = web.json_response({"next": f"{PREFIX}/jobs/{opened.session.job_id}"}, headers=_BASE_HEADERS)
        response.set_cookie(COOKIE, opened.cookie, path=f"{PREFIX}/", httponly=True, secure=True, samesite="Strict",
                            max_age=service.config.session_minutes * 60)
        return response

    async def _session(request: web.Request) -> object:
        return await service.session(request.cookies.get(COOKIE), request.match_info["job"])

    async def job_page(request: web.Request) -> web.Response:
        try:
            session = await _session(request)
            job, events = await service.page(session)  # type: ignore[arg-type]
        except AccessDenied as exc:
            return _denied(str(exc))
        me = session.user_id  # type: ignore[attr-defined]
        owner = me == service.config.owner_id
        if job.sensitive:
            allowed: tuple[str, ...] = ("cancel", "fail") if owner and job.is_open else ()
        elif job.state == "requested":
            allowed = ("claim", "cancel")
        elif job.state == "active" and job.claimed_by == me:
            allowed = ("view", "solve", "cancel", "fail")
        elif job.state == "verified" and job.resumed_at is None and job.claimed_by == me:
            allowed = ("resume",)
        else:
            allowed = ()
        labels = {"claim": "Claim", "view": "Open live browser", "solve": "Solved", "resume": "Resume the run",
                  "cancel": "Cancel", "fail": "Failed"}
        if job.platform == "website":
            labels["solve"] = "Готово"
        csrf = html.escape(session.csrf_token)  # type: ignore[attr-defined]
        buttons = "".join(
            f"<form method='post' action='{PREFIX}/jobs/{job.id}/{action}'><input type='hidden' name='csrf' value='{csrf}'>"
            f"<button type='submit'>{labels[action]}</button></form>" for action in allowed
        )
        message = request.query.get("m", "")
        rows = "".join(
            f"<tr><td>{e.occurred_at:%H:%M:%S}</td><td>{html.escape(e.event)}</td><td>{html.escape(e.actor)}</td></tr>" for e in events[-15:]
        )
        live = ""
        requested_live = request.query.get("live", "")
        # Only this job's own viewer may be framed; anything else is ignored.
        if (job.state == "active" and job.claimed_by == me and service.live_open(job.id)
                and requested_live.startswith(f"{PREFIX}/jobs/{job.id}/live/vnc.html?")):
            live = f"<iframe src='{html.escape(requested_live)}' title='Live browser'></iframe>"
        warning = ("<p class='warn'>This is about the account itself (identity, new 2FA or a restriction). Nothing is automated "
                   "and no live browser is offered. Handle it on the account directly, then close the job.</p>") if job.sensitive else ""
        body = (
            f"<h1>Verification {html.escape(job.platform)} / {html.escape(job.profile_name or '')}</h1>{warning}"
            + (f"<p class='msg'>{html.escape(message)}</p>" if message else "")
            + f"<dl><dt>State</dt><dd>{html.escape(job.state)}{' (resumed)' if job.resumed_at else ''}</dd>"
            f"<dt>Challenge</dt><dd>{html.escape(job.challenge_kind or 'unknown')}</dd>"
            f"<dt>Page</dt><dd>{html.escape(job.page_url)}</dd>"
            f"<dt>Claimed by</dt><dd>{job.claimed_by or 'nobody'}</dd></dl>"
            + buttons + live
            + f"<h2>Audit</h2><table>{rows}</table>"
        )
        return _html("Verification", body)

    async def act(request: web.Request) -> web.StreamResponse:
        action = request.match_info["action"]
        job_id = request.match_info["job"]
        if action not in ACTIONS:
            return _denied("Unknown action.", 404)
        try:
            session = await _session(request)
        except AccessDenied as exc:
            return _denied(str(exc))
        form = await request.post()
        submitted = form.get("csrf")
        if not isinstance(submitted, str) or not hmac.compare_digest(submitted, session.csrf_token):  # type: ignore[attr-defined]
            return _denied("The form expired. Reload the page.", 400)
        query = ""
        try:
            if action == "claim":
                await service.claim(session)  # type: ignore[arg-type]
                message = "Claimed. Open the live browser, solve the challenge by hand, then press Solved."
            elif action == "view":
                password = await service.view(session)  # type: ignore[arg-type]
                message = "The browser is open below."
                query = "&live=" + quote(_viewer(job_id, password))
            elif action == "solve":
                ok = await service.solve(session)  # type: ignore[arg-type]
                message = ("Recovered: the watchdog loaded the page and saw no challenge. You can resume the run."
                           if ok else "The watchdog still sees a challenge. Open the browser again, or mark the job Failed.")
            elif action == "resume":
                batch_id = await service.resume(session)  # type: ignore[arg-type]
                message = f"Run resumed: batch {batch_id} is queued again." if batch_id else "Resumed."
            elif action == "cancel":
                await service.cancel(session)  # type: ignore[arg-type]
                message = "Cancelled. The stopped run was cancelled; the profile still needs a human."
            else:
                await service.fail(session)  # type: ignore[arg-type]
                message = "Marked failed. The profile is quarantined and the owner was told."
        except (ActionRefused, AccessDenied) as exc:
            message = str(exc)
        raise web.HTTPSeeOther(f"{PREFIX}/jobs/{job_id}?m={quote(message)}{query}")

    async def live_proxy(request: web.Request) -> web.StreamResponse:
        try:
            session = await _session(request)
            job, _ = await service.page(session)  # type: ignore[arg-type]
        except AccessDenied as exc:
            return _denied(str(exc))
        if job.state != "active" or job.claimed_by != session.user_id or not service.live_open(job.id):  # type: ignore[attr-defined]
            return _denied("The live browser is not open for you.")
        upstream = f"{backend}/{request.match_info['path']}"
        if request.query_string:
            upstream += f"?{request.query_string}"
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _websocket(request, upstream)
        async with ClientSession() as client:
            try:
                async with client.get(upstream) as response:
                    body = await response.read()
                    content_type = response.headers.get("Content-Type", "application/octet-stream")
                    status = response.status
            except (OSError, ClientError):
                return _denied("The browser is not running.", 502)
        return web.Response(body=body, status=status, headers={"Content-Type": content_type, "Cache-Control": "no-store"})

    async def _websocket(request: web.Request, upstream: str) -> web.StreamResponse:
        offered = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        async with ClientSession() as client:
            try:
                ws_client = await client.ws_connect(upstream.replace("http://", "ws://", 1), protocols=offered, timeout=ClientWSTimeout(ws_close=10))
            except (OSError, ClientError):
                return _denied("The browser is not running.", 502)
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

                tasks = [asyncio.create_task(pump(ws_server, ws_client)), asyncio.create_task(pump(ws_client, ws_server))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await ws_client.close()
                    await ws_server.close()
        return ws_server

    app = web.Application(middlewares=[known_ids], client_max_size=16 * 1024)
    app.router.add_get("/healthz", health)
    app.router.add_get(f"{PREFIX}/v/{{token}}", opener)
    app.router.add_post(f"{PREFIX}/v/{{token}}/auth", auth)
    app.router.add_post(f"{PREFIX}/v/{{token}}/browser", browser_request)
    app.router.add_get(f"{PREFIX}/v/{{token}}/wait", browser_wait)
    app.router.add_get(f"{PREFIX}/a/{{approval}}", approval_page)
    app.router.add_post(f"{PREFIX}/a/{{approval}}/auth", approval_auth)
    app.router.add_get(f"{PREFIX}/jobs/{{job}}", job_page)
    app.router.add_post(f"{PREFIX}/jobs/{{job}}/{{action}}", act)
    app.router.add_get(f"{PREFIX}/jobs/{{job}}/live/{{path:.+}}", live_proxy)
    return app
