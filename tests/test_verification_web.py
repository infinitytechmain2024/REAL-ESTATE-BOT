"""The verification page over HTTP: Mini App signature, cookie, CSRF, live proxy."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from bot.verification.web import COOKIE, PENDING_COOKIE, create_app
from tests.test_live_view import init_data
from tests.test_verification_flow import OPERATOR, OTHER, announced, flow, token_of


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
        yield client, store, notifier, job
    finally:
        await client.close()
        await upstream.close()


def path_of(link: str) -> str:
    return urlsplit(link).path


async def login(client: TestClient, link: str, user: int = OPERATOR) -> dict[str, str]:
    response = await client.post(f"/verify/v/{token_of(link)}/auth", data=init_data(user))
    assert response.status == 200, await response.text()
    cookie = response.cookies[COOKIE]
    assert cookie["httponly"] and cookie["secure"] and cookie["samesite"] == "Strict" and cookie["path"] == "/verify/"
    # Plain-HTTP test server on an IP: send the Secure cookie as a browser would over HTTPS.
    return {"Cookie": f"{COOKIE}={cookie.value}"}


def csrf_of(html: str) -> str:
    return re.search(r"name='csrf' value='([^']+)'", html).group(1)  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_the_opener_is_a_mini_app_page_with_a_nonced_script(page) -> None:
    client, _store, notifier, _job = page
    link = notifier.links(OPERATOR)[0]
    opener = await client.get(path_of(link))
    body = await opener.text()
    csp = opener.headers["Content-Security-Policy"]
    assert opener.status == 200 and "telegram-web-app.js" in body
    nonce = re.search(r"nonce-([\w-]+)", csp).group(1)  # type: ignore[union-attr]
    assert f'nonce="{nonce}"' in body and "'unsafe-inline'" not in csp.split("script-src", 1)[1].split(";")[0]
    # Opening the page itself consumes nothing.
    assert (await client.get(path_of(link))).status == 200


@pytest.mark.asyncio
async def test_without_a_telegram_signature_nothing_is_served(page) -> None:
    client, _store, notifier, job = page
    link = notifier.links(OPERATOR)[0]
    for body in ("", "garbage", init_data(99)):
        refused = await client.post(f"/verify/v/{token_of(link)}/auth", data=body)
        assert refused.status == 403 and "error" in await refused.json()
    # Another operator cannot use (or burn) this operator's link.
    assert (await client.post(f"/verify/v/{token_of(link)}/auth", data=init_data(OTHER))).status == 403
    await login(client, link)
    assert (await client.post(f"/verify/v/{token_of(link)}/auth", data=init_data(OPERATOR))).status == 403
    assert (await client.get(f"/verify/jobs/{job.id}")).status == 403  # no cookie
    assert (await client.get("/verify/jobs/not-a-uuid")).status == 404
    assert (await client.get("/healthz")).status == 200


@pytest.mark.asyncio
async def test_the_full_page_flow_with_csrf(page) -> None:
    client, store, notifier, job = page
    headers = await login(client, notifier.links(OPERATOR)[0])

    first = await client.get(f"/verify/jobs/{job.id}", headers=headers)
    body = await first.text()
    assert first.status == 200 and ">Claim<" in body and ">Cancel<" in body and ">Solved<" not in body
    assert "<script" not in body.lower() and "default-src 'none'" in first.headers["Content-Security-Policy"]
    token = csrf_of(body)

    assert (await client.post(f"/verify/jobs/{job.id}/claim", data={"csrf": "wrong"}, headers=headers)).status == 400
    assert (await client.post(f"/verify/jobs/{job.id}/claim", data={}, headers=headers)).status == 400
    assert store.jobs[job.id].state == "requested"

    claimed = await client.post(f"/verify/jobs/{job.id}/claim", data={"csrf": token}, headers=headers, allow_redirects=False)
    assert claimed.status == 303 and "Claimed" in unquote(claimed.headers["Location"])
    body = await (await client.get(f"/verify/jobs/{job.id}", headers=headers)).text()
    assert ">Open live browser<" in body and ">Solved<" in body and ">Failed<" in body

    viewed = await client.post(f"/verify/jobs/{job.id}/view", data={"csrf": token}, headers=headers, allow_redirects=False)
    framed = await (await client.get(viewed.headers["Location"], headers=headers)).text()
    assert f"<iframe src='/verify/jobs/{job.id}/live/vnc.html?" in framed and "password=vncpass1" in framed
    assert f"path=verify/jobs/{job.id}/live/websockify" in unquote(framed)

    forged = await (await client.get(f"/verify/jobs/{job.id}?live=https://evil.example/", headers=headers)).text()
    assert "<iframe" not in forged

    static = await client.get(f"/verify/jobs/{job.id}/live/vnc.html", headers=headers)
    assert await static.text() == "novnc:vnc.html"
    async with client.ws_connect(f"/verify/jobs/{job.id}/live/websockify", protocols=("binary",), headers=headers) as ws:
        await ws.send_bytes(b"frame")
        assert (await ws.receive()).data == b"echo:frame"

    solved = await client.post(f"/verify/jobs/{job.id}/solve", data={"csrf": token}, headers=headers, allow_redirects=False)
    assert "Recovered" in unquote(solved.headers["Location"])
    assert (await client.get(f"/verify/jobs/{job.id}/live/vnc.html", headers=headers)).status == 403  # window closed
    resumed = await client.post(f"/verify/jobs/{job.id}/resume", data={"csrf": token}, headers=headers, allow_redirects=False)
    assert f"batch {job.batch_id} is queued" in unquote(resumed.headers["Location"])
    final = await (await client.get(f"/verify/jobs/{job.id}", headers=headers)).text()
    assert "verified (resumed)" in final and "<form" not in final
    for event in ("opened", "claim", "view", "solve", "recovery_confirmed", "resume"):
        assert f"<td>{event}</td>" in final
    assert f"<td>telegram:{OPERATOR}</td>" in final


@pytest.mark.asyncio
async def test_the_live_proxy_needs_the_holders_session(page) -> None:
    client, _store, notifier, job = page
    mine = await login(client, notifier.links(OPERATOR)[0])
    theirs = await login(client, notifier.links(OTHER)[0], OTHER)
    assert (await client.get(f"/verify/jobs/{job.id}/live/vnc.html", headers=mine)).status == 403  # nothing open yet
    token = csrf_of(await (await client.get(f"/verify/jobs/{job.id}", headers=mine)).text())
    await client.post(f"/verify/jobs/{job.id}/claim", data={"csrf": token}, headers=mine)
    await client.post(f"/verify/jobs/{job.id}/view", data={"csrf": token}, headers=mine)
    assert (await client.get(f"/verify/jobs/{job.id}/live/vnc.html", headers=mine)).status == 200
    assert (await client.get(f"/verify/jobs/{job.id}/live/vnc.html", headers=theirs)).status == 403


@pytest.mark.asyncio
async def test_unknown_actions_and_refusals_are_reported(page) -> None:
    client, _store, notifier, job = page
    headers = await login(client, notifier.links(OPERATOR)[0])
    token = csrf_of(await (await client.get(f"/verify/jobs/{job.id}", headers=headers)).text())
    assert (await client.post(f"/verify/jobs/{job.id}/explode", data={"csrf": token}, headers=headers)).status == 404
    refused = await client.post(f"/verify/jobs/{job.id}/solve", data={"csrf": token}, headers=headers, allow_redirects=False)
    assert "Claim the job first" in unquote(refused.headers["Location"])


async def open_in_browser(client: TestClient, link: str) -> dict[str, str]:
    """Safari: no Telegram signature, so ask for approval; returns the pending cookie header."""
    opener = await client.get(path_of(link))
    assert "Continue in this browser" in await opener.text()
    response = await client.post(f"/verify/v/{token_of(link)}/browser", allow_redirects=False,
                                 headers={"User-Agent": "Mozilla/5.0 (iPhone) Safari/605.1", "X-Forwarded-For": "203.0.113.9"})
    assert response.status == 303 and response.headers["Location"] == f"/verify/v/{token_of(link)}/wait"
    pending = response.cookies[PENDING_COOKIE]
    assert pending["httponly"] and pending["secure"] and pending["samesite"] == "Strict"
    return {"Cookie": f"{PENDING_COOKIE}={pending.value}"}


def approval_of(notifier, user: int) -> str:
    return next(url for url in reversed(notifier.links(user)) if "/verify/a/" in url)


@pytest.mark.asyncio
async def test_a_link_opened_in_safari_works_only_after_its_recipient_approves_in_telegram(page) -> None:
    client, store, notifier, job = page
    link = notifier.links(OPERATOR)[0]
    assert link in next(t for c, t, _ in notifier.sent if c == OPERATOR)  # the link is also in the text, to copy
    pending = await open_in_browser(client, link)
    ask = [t for c, t, b in notifier.sent if c == OPERATOR and b and "/verify/a/" in b[1]]
    assert ask and "iPhone" in ask[0] and "203.0.113.9" in ask[0]

    waiting = await client.get(f"/verify/v/{token_of(link)}/wait", headers=pending, allow_redirects=False)
    assert waiting.status == 200 and "Check Telegram" in await waiting.text()

    approval = path_of(approval_of(notifier, OPERATOR))
    assert (await client.post(f"{approval}/auth", data=init_data(OTHER))).status == 403  # not the recipient
    assert (await client.post(f"{approval}/auth", data="garbage")).status == 403
    assert (await client.post(f"{approval}/auth", data=init_data(OPERATOR))).status == 200

    done = await client.get(f"/verify/v/{token_of(link)}/wait", headers=pending, allow_redirects=False)
    assert done.status == 303 and done.headers["Location"] == f"/verify/jobs/{job.id}"
    session = {"Cookie": f"{COOKIE}={done.cookies[COOKIE].value}"}
    assert (await client.get(f"/verify/jobs/{job.id}", headers=session)).status == 200
    assert [e.detail.get("via") for e in store.log[job.id] if e.event == "opened"] == ["browser"]
    # The approval opened one browser once; the link itself is now used up.
    assert (await client.get(f"/verify/v/{token_of(link)}/wait", headers=pending)).status == 403
    assert (await client.post(f"/verify/v/{token_of(link)}/auth", data=init_data(OPERATOR))).status == 403


@pytest.mark.asyncio
async def test_an_approved_request_serves_only_the_browser_that_asked(page) -> None:
    client, _store, notifier, _job = page
    link = notifier.links(OPERATOR)[0]
    await open_in_browser(client, link)
    await client.post(f"{path_of(approval_of(notifier, OPERATOR))}/auth", data=init_data(OPERATOR))
    # Someone else holding the forwarded link, but not the waiting browser's cookie.
    assert (await client.get(f"/verify/v/{token_of(link)}/wait")).status == 403
    stranger = await client.get(f"/verify/v/{token_of(link)}/wait", headers={"Cookie": f"{PENDING_COOKIE}=x" * 1})
    assert stranger.status == 403


@pytest.mark.asyncio
async def test_a_link_cannot_be_used_to_flood_its_recipient(page) -> None:
    client, _store, notifier, _job = page
    link = notifier.links(OPERATOR)[0]
    for _ in range(3):
        await open_in_browser(client, link)
    refused = await client.post(f"/verify/v/{token_of(link)}/browser", allow_redirects=False)
    assert refused.status == 403 and "Too many attempts" in await refused.text()
    bogus = await client.post("/verify/v/not-a-token/browser", allow_redirects=False)
    assert bogus.status == 403
