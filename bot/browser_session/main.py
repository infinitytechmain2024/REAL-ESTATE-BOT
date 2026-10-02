"""Internal HTTP control boundary for the browser session owner."""

from __future__ import annotations

from pathlib import Path

import redis.asyncio as redis
from aiohttp import web

from .live import LiveViewController, LiveViewError
from .manager import BrowserSessionManager, ProfileUnavailableError, SessionBusyError
from .models import BrowserProfileStatus, Device, ProfileRequest, SessionHandle
from .settings import BrowserSessionSettings


def create_app(manager: BrowserSessionManager, token: str, live: LiveViewController | None = None) -> web.Application:
    app = web.Application()
    live = live or LiveViewController(manager)

    @web.middleware
    async def auth(request: web.Request, handler: web.Handler) -> web.StreamResponse:
        if request.path != "/healthz" and request.headers.get("Authorization") != f"Bearer {token}":
            raise web.HTTPUnauthorized(text="browser-session authentication required")
        return await handler(request)

    app.middlewares.append(auth)

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"ok": await manager.health()})

    async def acquire(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            handle = await manager.acquire(
                ProfileRequest(body["profile_id"], body.get("profile_name", body["profile_id"]), body["platform"]),
                persisted_state=body.get("persisted_state", "ready"),
            )
        except (SessionBusyError, ProfileUnavailableError, ValueError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response({"profile_id": handle.profile_id, "session_token": handle.token, "expires_at": handle.expires_at})

    def handle(body: dict[str, str]) -> SessionHandle:
        return SessionHandle(body["profile_id"], body["session_token"], Path("/profiles"), 0)

    async def release(request: web.Request) -> web.Response:
        body = await request.json()
        state = BrowserProfileStatus(body.get("next_state", "READY"))
        await manager.release(handle(body), next_state=state)
        return web.json_response({"ok": True})

    async def screenshot(request: web.Request) -> web.FileResponse:
        body = await request.json()
        try:
            image = await manager.screenshot(handle(body))
        except PermissionError as exc:
            raise web.HTTPForbidden(text=str(exc)) from exc
        # The name lets a caller record which private screenshot belongs to an event.
        return web.FileResponse(image, headers={"X-Screenshot-File": f"{image.parent.name}/{image.name}"})

    async def snapshot(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            result = await manager.snapshot(
                handle(body), body["url"], timeout_ms=int(body.get("timeout_ms", 30_000)),
                scrolls=int(body.get("scrolls", 0)),
            )
        except (PermissionError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(result)

    async def live_start(request: web.Request) -> web.Response:
        body = await request.json()
        try:
            screen = body.get("device") if isinstance(body.get("device"), dict) else {}
            device = Device.from_screen(screen.get("width"), screen.get("height"), screen.get("mobile")) if screen else None
            result = await live.start(
                ProfileRequest(body["profile_id"], body.get("profile_name", body["profile_id"]), body["platform"]),
                body["url"],
                int(body.get("minutes", 20)),
                device,
            )
        except (LiveViewError, SessionBusyError) as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        return web.json_response(result)

    async def live_stop(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        return web.json_response({"closed": await live.stop(body.get("profile_id"))})

    async def live_status(_: web.Request) -> web.Response:
        return web.json_response(live.status())

    async def live_check(request: web.Request) -> web.Response:
        """Whether the open live window's profile is signed in (a boolean, never a cookie)."""
        body = await request.json()
        try:
            signed = await live.logged_in(str(body.get("profile_id", "")))
        except LiveViewError as exc:
            raise web.HTTPConflict(text=str(exc)) from exc
        return web.json_response({"logged_in": signed})

    async def shutdown(_: web.Application) -> None:
        await live.stop()
        await manager.close()

    app.router.add_get("/healthz", health)
    app.router.add_post("/v1/live", live_start)
    app.router.add_delete("/v1/live", live_stop)
    app.router.add_get("/v1/live", live_status)
    app.router.add_post("/v1/live/check", live_check)
    app.router.add_post("/v1/sessions", acquire)
    app.router.add_delete("/v1/sessions", release)
    app.router.add_post("/v1/sessions/screenshot", screenshot)
    app.router.add_post("/v1/sessions/snapshot", snapshot)
    app.on_shutdown.append(shutdown)
    return app


def run() -> None:
    settings = BrowserSessionSettings()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    manager = BrowserSessionManager(
        client,
        profile_root=settings.profile_root,
        screenshot_root=settings.screenshot_root,
        lease_seconds=settings.lease_seconds,
        renew_seconds=settings.lease_renew_seconds,
        idle_seconds=settings.idle_seconds,
    )
    web.run_app(create_app(manager, settings.api_token), host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    run()
