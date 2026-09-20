#!/usr/bin/env python3
"""Acceptance check for the Telegram-link -> live-view path, without a browser.

The gate is the one piece of the Facebook stack whose failure mode is silent:
the admin taps the button, a page loads, and then nothing happens because the
WebSocket underneath never connected. That is hard to notice in code review
and obvious in thirty seconds of running this, which stands a pair of fake
noVNC/websockify endpoints behind a real gate and drives it like a phone would.

Checks, in order: the entry URL redirects to a viewer link carrying the
token-prefixed WebSocket path (noVNC defaults to ``/websockify`` at the root,
which this gate deliberately does not route); the page proxies; the socket
connects, keeps its subprotocol and passes frames both ways; closing one side
tears down the other instead of leaking a connection to websockify; and a bad
token opens neither a page nor a socket.

Usage:
    python scripts/gate_probe.py

Exits non-zero on the first failing expectation, so it works in a boot script
or a deploy check. Nothing here touches Chrome, Xvfb or the real noVNC -- for
that, tap the actual Telegram button.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, WSServerHandshakeError, web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.services.facebook.gate import build_gate_app
from bot.services.facebook.tokens import TokenStore

BACKEND_PORT = 16080
GATE_PORT = 18090
PAGE_BODY = "noVNC stand-in"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + detail) if detail else ''}")


async def _fake_page(request: web.Request) -> web.Response:
    return web.Response(text=PAGE_BODY)


async def _fake_websockify(request: web.Request) -> web.WebSocketResponse:
    """Echoes with a prefix, so a proxied frame is distinguishable from a fake."""
    ws = web.WebSocketResponse(protocols=("binary",))
    await ws.prepare(request)
    async for msg in ws:
        if msg.type == WSMsgType.BINARY:
            await ws.send_bytes(b"RFB:" + msg.data)
        elif msg.type == WSMsgType.TEXT:
            await ws.send_str("echo:" + msg.data)
    return ws


async def _serve(app: web.Application, port: int) -> web.AppRunner:
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


async def main() -> int:
    backend = web.Application()
    backend.router.add_get("/vnc.html", _fake_page)
    backend.router.add_get("/websockify", _fake_websockify)
    backend_runner = await _serve(backend, BACKEND_PORT)

    with tempfile.TemporaryDirectory() as tmp:
        store = TokenStore(str(Path(tmp) / "token.json"))
        token = await store.create(600)
        gate = build_gate_app(store, f"http://127.0.0.1:{BACKEND_PORT}", pin=None)
        gate_runner = await _serve(gate, GATE_PORT)
        base = f"http://127.0.0.1:{GATE_PORT}"

        async with ClientSession() as session:
            # What the Telegram button actually points at.
            async with session.get(f"{base}/s/{token}", allow_redirects=False) as resp:
                location = resp.headers.get("Location", "")
                check("entry redirects", resp.status == 302, f"status={resp.status}")
                check(
                    "viewer link carries the prefixed ws path",
                    f"path=s/{token}/websockify" in location,
                    location,
                )
                check("viewer link autoconnects", "autoconnect=true" in location, location)

            async with session.get(f"{base}{location}") as resp:
                body = await resp.text()
                check(
                    "viewer page proxied",
                    resp.status == 200 and body == PAGE_BODY,
                    f"status={resp.status}",
                )

            # The socket noVNC dials once the link above is honoured.
            try:
                async with session.ws_connect(
                    f"{base}/s/{token}/websockify", protocols=("binary",)
                ) as ws:
                    check("socket connects through the gate", True)
                    check(
                        "subprotocol preserved",
                        ws.protocol == "binary",
                        f"protocol={ws.protocol!r}",
                    )
                    await ws.send_bytes(b"hello")
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                    check("frames proxied both ways", msg.data == b"RFB:hello", repr(msg.data))
            except (TimeoutError, OSError, WSServerHandshakeError) as exc:
                check("socket connects through the gate", False, f"{type(exc).__name__}: {exc}")

            # Closing the viewer must not leave the upstream socket dangling:
            # if it does, this hangs rather than failing, so bound it.
            try:
                await asyncio.wait_for(gate_runner.cleanup(), timeout=10)
                check("closing the viewer tears down the upstream socket", True)
            except TimeoutError:
                check(
                    "closing the viewer tears down the upstream socket",
                    False,
                    "handler still running after the client went away",
                )
            gate_runner = await _serve(
                build_gate_app(store, f"http://127.0.0.1:{BACKEND_PORT}", None), GATE_PORT
            )

            # The gate still gates.
            async with session.get(f"{base}/s/not-a-real-token/vnc.html") as resp:
                check("bad token gets no page", resp.status == 410, f"status={resp.status}")
            try:
                async with session.ws_connect(f"{base}/s/not-a-real-token/websockify"):
                    check("bad token gets no socket", False, "it connected")
            except WSServerHandshakeError as exc:
                check("bad token gets no socket", exc.status == 410, f"status={exc.status}")

        await gate_runner.cleanup()
    await backend_runner.cleanup()

    failures = [name for name, ok, _ in _results if not ok]
    print(f"\n{len(_results) - len(failures)}/{len(_results)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
