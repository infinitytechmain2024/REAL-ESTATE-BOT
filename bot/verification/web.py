"""The verification page, served only to ``tailscale serve`` on 127.0.0.1.

Every request must carry the Tailscale identity header that ``tailscale
serve`` adds for tailnet users; the service binds to loopback inside the
Tailscale container's network namespace, so nothing else can reach it to
forge that header. On top of it: a single-use Telegram link, then a
short, HttpOnly, SameSite=Strict session cookie bound to job, user,
profile and Tailscale login, and a CSRF token on every button.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import logging
import uuid
from collections.abc import Awaitable, Callable
from urllib.parse import quote

from aiohttp import ClientError, ClientSession, ClientWSTimeout, WSMsgType, web

from .service import AccessDenied, ActionRefused, VerificationService

log = logging.getLogger(__name__)
LOGIN_HEADER = "Tailscale-User-Login"
COOKIE = "verification_session"
ACTIONS = ("claim", "view", "solve", "resume", "cancel", "fail")

_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    # Our pages carry no scripts at all; the noVNC viewer is framed from our own origin.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}

_STYLE = """body{font-family:system-ui,sans-serif;max-width:60em;margin:1em auto;padding:0 1em;color:#1b1b1b}
h1{font-size:1.3em}dt{font-weight:600}dd{margin:0 0 .5em}form{display:inline-block;margin:.25em .5em .25em 0}
button{font-size:1em;padding:.5em 1em}.msg{padding:.6em;background:#eef;border-radius:4px}
.warn{padding:.6em;background:#fee;border-radius:4px}iframe{width:100%;height:75vh;border:1px solid #999}
table{border-collapse:collapse;font-size:.9em}td{padding:.2em .6em;border-bottom:1px solid #ddd}"""


def _page(title: str, body: str, status: int = 200) -> web.Response:
    return web.Response(
        text=f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head><body>{body}</body></html>",
        content_type="text/html", status=status, headers=_PAGE_HEADERS,
    )


def _denied(message: str, status: int = 403) -> web.Response:
    return _page("Not allowed", f"<h1>Not allowed</h1><p>{html.escape(message)}</p>", status)


def _viewer(job_id: str, password: str) -> str:
    return (f"/jobs/{job_id}/live/vnc.html?path={quote(f'jobs/{job_id}/live/websockify')}"
            f"&password={quote(password)}&autoconnect=true&resize=scale&reconnect=true")


def create_app(service: VerificationService, novnc_url: str) -> web.Application:
    backend = novnc_url.rstrip("/")

    @web.middleware
    async def guard(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        if request.path == "/healthz":
            return await handler(request)
        job_id = request.match_info.get("job")
        if job_id is not None:
            try:
                if str(uuid.UUID(job_id)) != job_id:
                    raise ValueError
            except ValueError:
                return _denied("Unknown job.", 404)
        try:
            service.check_login(request.headers.get(LOGIN_HEADER))
        except AccessDenied as exc:
            log.warning("verification.denied_login", extra={"login": request.headers.get(LOGIN_HEADER), "path": request.path[:60]})
            return _denied(str(exc))
        return await handler(request)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def open_link(request: web.Request) -> web.StreamResponse:
        try:
            opened = await service.open(request.match_info["token"], request.headers.get(LOGIN_HEADER))
        except AccessDenied as exc:
            return _denied(str(exc))
        response = web.HTTPSeeOther(f"/jobs/{opened.session.job_id}")
        response.set_cookie(COOKIE, opened.cookie, path="/", httponly=True, secure=True, samesite="Strict",
                            max_age=service.config.session_minutes * 60)
        response.headers.update(_PAGE_HEADERS)
        raise response

    async def _session(request: web.Request) -> object:
        return await service.session(request.cookies.get(COOKIE), request.match_info["job"], request.headers.get(LOGIN_HEADER))

    async def job_page(request: web.Request) -> web.Response:
        try:
            session = await _session(request)
            job, events = await service.page(session)  # type: ignore[arg-type]
        except AccessDenied as exc:
            return _denied(str(exc))
        me = session.user_id  # type: ignore[attr-defined]
        owner = me == service.config.owner_id
        buttons = []
        if job.sensitive:
            allowed = ("cancel", "fail") if owner and job.is_open else ()
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
        csrf = html.escape(session.csrf_token)  # type: ignore[attr-defined]
        for action in allowed:
            buttons.append(f"<form method='post' action='/jobs/{job.id}/{action}'><input type='hidden' name='csrf' value='{csrf}'>"
                           f"<button type='submit'>{labels[action]}</button></form>")
        message = request.query.get("m", "")
        rows = "".join(
            f"<tr><td>{e.occurred_at:%H:%M:%S}</td><td>{html.escape(e.event)}</td><td>{html.escape(e.actor)}</td></tr>" for e in events[-15:]
        )
        live = ""
        requested_live = request.query.get("live", "")
        # Only this job's own viewer may be framed; anything else is ignored.
        if (job.state == "active" and job.claimed_by == me and service.live_open(job.id)
                and requested_live.startswith(f"/jobs/{job.id}/live/vnc.html?")):
            live = f"<iframe src='{html.escape(requested_live)}' title='Live browser'></iframe>"
        warning = ("<p class='warn'>This is about the account itself (identity, new 2FA or a restriction). Nothing is automated "
                   "and no live browser is offered. Handle it on the account directly, then close the job.</p>") if job.sensitive else ""
        body = (
            f"<h1>Verification {html.escape(job.platform)} / {html.escape(job.profile_name or '')}</h1>{warning}"
            + (f"<p class='msg'>{html.escape(message)}</p>" if message else "")
            + f"<dl><dt>State</dt><dd>{html.escape(job.state)}{' (resumed)' if job.resumed_at else ''}</dd>"
            f"<dt>Challenge</dt><dd>{html.escape(job.challenge_kind or 'unknown')}</dd>"
            f"<dt>Page</dt><dd>{html.escape(job.source_url)}</dd>"
            f"<dt>Claimed by</dt><dd>{job.claimed_by or 'nobody'}</dd></dl>"
            + "".join(buttons) + live
            + f"<h2>Audit</h2><table>{rows}</table>"
        )
        return _page("Verification", body)

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
        raise web.HTTPSeeOther(f"/jobs/{job_id}?m={quote(message)}{query}")

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

    app = web.Application(middlewares=[guard], client_max_size=16 * 1024)
    app.router.add_get("/healthz", health)
    app.router.add_get("/v/{token}", open_link)
    app.router.add_get("/jobs/{job}", job_page)
    app.router.add_post("/jobs/{job}/{action}", act)
    app.router.add_get("/jobs/{job}/live/{path:.+}", live_proxy)
    return app
