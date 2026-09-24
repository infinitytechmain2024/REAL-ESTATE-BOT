"""The verification page over HTTP: Tailscale identity, cookie, CSRF, live proxy."""

from __future__ import annotations

import re
from urllib.parse import unquote

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from bot.verification.web import COOKIE, LOGIN_HEADER, create_app
from tests.test_verification_flow import LOGIN, OPERATOR, announced, flow, token_of

TS = {LOGIN_HEADER: LOGIN}


async def fake_novnc(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Upgrade", "").lower() == "websocket":
        ws = web.WebSocketResponse(protocols=("binary",))
        await ws.prepare(request)
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                await ws.send_bytes(b"echo:" + message.data)
        return ws
    return web.Response(text=f"novnc:{request.match_info['path']}", content_type="text/html")


@pytest.fixture
async def page():
    upstream_app = web.Application()
    upstream_app.router.add_get("/{path:.*}", fake_novnc)
    upstream = TestServer(upstream_app)
    await upstream.start_server()
    service, store, notifier, _watchdog = flow()
    job = await announced(service, store)
    client = TestClient(TestServer(create_app(service, str(upstream.make_url("")))))
    await client.start_server()
    try:
        yield client, service, store, notifier, job
    finally:
        await client.close()
        await upstream.close()


async def login(client: TestClient, link: str) -> dict[str, str]:
    response = await client.get(f"/v/{token_of(link)}", headers=TS, allow_redirects=False)
    assert response.status == 303
    cookie = response.cookies[COOKIE]
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Strict"
    # Plain-HTTP test server on an IP: send the Secure cookie as a browser would over HTTPS.
    return {**TS, "Cookie": f"{COOKIE}={cookie.value}"}


def csrf_of(html: str) -> str:
    return re.search(r"name='csrf' value='([^']+)'", html).group(1)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_without_an_approved_tailscale_identity_nothing_is_served(page) -> None:
    client, _service, _store, notifier, _job = page
    link = notifier.links(OPERATOR)[0]
    for headers in ({}, {LOGIN_HEADER: "stranger@example.com"}):
        response = await client.get(f"/v/{token_of(link)}", headers=headers, allow_redirects=False)
        assert response.status == 403
    # The refused attempts did not burn the single-use token.
    assert (await client.get(f"/v/{token_of(link)}", headers=TS, allow_redirects=False)).status == 303
    assert (await client.get("/healthz")).status == 200
    assert (await client.get("/jobs/not-a-uuid", headers=TS)).status == 404


@pytest.mark.asyncio
async def test_the_full_page_flow_with_csrf(page) -> None:
    client, _service, store, notifier, job = page
    headers = await login(client, notifier.links(OPERATOR)[0])
    assert (await client.get(f"/v/{token_of(notifier.links(OPERATOR)[0])}", headers=TS, allow_redirects=False)).status == 403

    first = await client.get(f"/jobs/{job.id}", headers=headers)
    body = await first.text()
    assert first.status == 200 and ">Claim<" in body and ">Cancel<" in body and ">Solved<" not in body
    assert "script" not in body.lower() and "default-src 'none'" in first.headers["Content-Security-Policy"]
    token = csrf_of(body)

    assert (await client.post(f"/jobs/{job.id}/claim", data={"csrf": "wrong"}, headers=headers)).status == 400
    assert (await client.post(f"/jobs/{job.id}/claim", data={}, headers=headers)).status == 400
    assert store.jobs[job.id].state == "requested"

    claimed = await client.post(f"/jobs/{job.id}/claim", data={"csrf": token}, headers=headers, allow_redirects=False)
    assert claimed.status == 303 and "Claimed" in unquote(claimed.headers["Location"])
    body = await (await client.get(f"/jobs/{job.id}", headers=headers)).text()
    assert ">Open live browser<" in body and ">Solved<" in body and ">Failed<" in body

    viewed = await client.post(f"/jobs/{job.id}/view", data={"csrf": token}, headers=headers, allow_redirects=False)
    location = viewed.headers["Location"]
    framed = await (await client.get(location, headers=headers)).text()
    assert f"<iframe src='/jobs/{job.id}/live/vnc.html?" in framed and "password=vncpass1" in framed

    # A forged frame target is ignored.
    forged = await (await client.get(f"/jobs/{job.id}?live=https://evil.example/", headers=headers)).text()
    assert "<iframe" not in forged

    static = await client.get(f"/jobs/{job.id}/live/vnc.html", headers=headers)
    assert await static.text() == "novnc:vnc.html"
    async with client.ws_connect(f"/jobs/{job.id}/live/websockify", protocols=("binary",), headers=headers) as ws:
        await ws.send_bytes(b"frame")
        assert (await ws.receive()).data == b"echo:frame"

    solved = await client.post(f"/jobs/{job.id}/solve", data={"csrf": token}, headers=headers, allow_redirects=False)
    assert "Recovered" in unquote(solved.headers["Location"])
    # The window is closed now; the proxy refuses.
    assert (await client.get(f"/jobs/{job.id}/live/vnc.html", headers=headers)).status == 403
    resumed = await client.post(f"/jobs/{job.id}/resume", data={"csrf": token}, headers=headers, allow_redirects=False)
    assert f"batch {job.batch_id} is queued" in unquote(resumed.headers["Location"])
    final = await (await client.get(f"/jobs/{job.id}", headers=headers)).text()
    assert "verified (resumed)" in final and "<form" not in final
    for event in ("opened", "claim", "view", "solve", "recovery_confirmed", "resume"):
        assert f"<td>{event}</td>" in final


@pytest.mark.asyncio
async def test_the_live_proxy_needs_the_holders_session(page) -> None:
    client, _service, _store, notifier, job = page
    headers = await login(client, notifier.links(OPERATOR)[0])
    assert (await client.get(f"/jobs/{job.id}/live/vnc.html", headers=TS)).status == 403
    assert (await client.get(f"/jobs/{job.id}/live/vnc.html", headers=headers)).status == 403  # not claimed/open
    other_login = {**headers, LOGIN_HEADER: "op@example.com"}
    assert (await client.get(f"/jobs/{job.id}", headers=other_login)).status == 403


@pytest.mark.asyncio
async def test_unknown_actions_and_refusals_are_reported(page) -> None:
    client, _service, _store, notifier, job = page
    headers = await login(client, notifier.links(OPERATOR)[0])
    token = csrf_of(await (await client.get(f"/jobs/{job.id}", headers=headers)).text())
    assert (await client.post(f"/jobs/{job.id}/explode", data={"csrf": token}, headers=headers)).status == 404
    refused = await client.post(f"/jobs/{job.id}/solve", data={"csrf": token}, headers=headers, allow_redirects=False)
    assert "Claim the job first" in unquote(refused.headers["Location"])
