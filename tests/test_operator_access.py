"""Access requests from Telegram, owner decisions and roles (helper / operator)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from bot.control_plane.access import AccessDesk, MemoryAccessStore, PostgresAccessStore
from bot.control_plane.models import IncomingMessage, Reply
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore
from bot.operators import OperatorSet

OWNER, SECOND_OWNER, HELPER, OPERATOR, STRANGER = 11, 12, 21, 22, 99


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[int, Reply]] = []

    async def __call__(self, chat_id: int, reply: Reply) -> None:
        self.sent.append((chat_id, reply))

    def to(self, chat_id: int) -> list[Reply]:
        return [r for c, r in self.sent if c == chat_id]


def plane() -> tuple[ControlPlane, AccessDesk, Outbox]:
    outbox = Outbox()
    operators = OperatorSet({OWNER, SECOND_OWNER})
    desk = AccessDesk(MemoryAccessStore(), operators, notify=outbox)
    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x", operator_user_ids=frozenset({OWNER, SECOND_OWNER}))
    return ControlPlane(settings, MemoryControlPlaneStore(), None, lambda _: None, access=desk), desk, outbox


_ids = iter(range(1, 10_000))


def text(user: int, body: str) -> IncomingMessage:
    return IncomingMessage(chat_id=user, user_id=user, message_id=next(_ids), text=body)


def request_id(reply: Reply) -> str:
    return reply.buttons[0].callback_data.rsplit(":", 1)[1]  # type: ignore[union-attr]


async def approve(control: ControlPlane, outbox: Outbox, user: int, role: str, name: str = "Ann") -> None:
    await control.handle_callback(user, "access:request", name, "ann")
    request = outbox.to(OWNER)[-1]
    reply = await control.handle_callback(OWNER, f"access:{role}:{request_id(request)}")
    assert reply.text.startswith(f"Approved as {role}")


# --- the set itself -----------------------------------------------------------------------


def test_operator_set_roles() -> None:
    operators = OperatorSet({OWNER}, {HELPER: "helper", OPERATOR: "operator"})
    assert OWNER in operators and HELPER in operators and STRANGER not in operators
    assert list(operators) == [OWNER, HELPER, OPERATOR] and len(operators) == 3
    assert OWNER in operators.controllers and OPERATOR in operators.controllers and HELPER not in operators.controllers
    assert operators.role(HELPER) == "helper" and operators.role(OWNER) == "owner" and operators.role(None) is None
    operators.discard(OPERATOR)
    assert OPERATOR not in operators and OPERATOR not in operators.controllers
    with pytest.raises(ValueError):
        operators.set(STRANGER, "admin")


# --- asking --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stranger_sees_the_request_button_wherever_access_is_refused() -> None:
    control, _, _ = plane()
    for body in ("/help", "/status", "/run website https://example.org"):
        reply = await control.handle_text(text(STRANGER, body))
        assert reply and reply.buttons and reply.buttons[-1].callback_data == "access:request", body
    voice = await control.handle_voice(IncomingMessage(STRANGER, STRANGER, next(_ids), voice_file_id="v", voice_size=1, voice_duration_seconds=1), lambda: None)  # type: ignore[arg-type]
    assert voice and voice.buttons[-1].callback_data == "access:request"
    owner = await control.handle_text(text(OWNER, "/help"))
    assert owner and not owner.buttons and "/operators" in owner.text


@pytest.mark.asyncio
async def test_a_request_goes_to_every_owner_once_with_role_choices() -> None:
    control, _, outbox = plane()
    reply = await control.handle_callback(STRANGER, "access:request", "Ann Smith", "ann")
    assert "Request sent" in reply.text
    for owner in (OWNER, SECOND_OWNER):
        [notice] = outbox.to(owner)
        assert "Ann Smith (@ann), ID 99" in notice.text
        assert [b.text for b in notice.buttons] == ["Approve as helper", "Approve as user (Пользователь)", "Approve as operator", "Deny"]
    again = await control.handle_callback(STRANGER, "access:request", "Ann Smith", "ann")
    assert "already waiting" in again.text and len(outbox.sent) == 2
    assert "already have" in (await control.handle_callback(OWNER, "access:request")).text


# --- deciding -------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_an_owner_decides_and_only_once() -> None:
    control, desk, outbox = plane()
    await control.handle_callback(STRANGER, "access:request", "Ann", None)
    rid = request_id(outbox.to(OWNER)[0])
    assert "Only an owner" in (await control.handle_callback(STRANGER, f"access:operator:{rid}")).text
    assert STRANGER not in desk.operators
    assert (await control.handle_callback(OWNER, f"access:helper:{rid}")).text == "Approved as helper: Ann, ID 99."
    assert "already decided" in (await control.handle_callback(SECOND_OWNER, f"access:operator:{rid}")).text
    assert desk.operators.role(STRANGER) == "helper"
    assert "Access granted as helper" in outbox.to(STRANGER)[-1].text
    for bad in ("access:helper:not-a-uuid", "access:admin:" + rid, "access:"):
        assert "no longer valid" in (await control.handle_callback(OWNER, bad)).text


@pytest.mark.asyncio
async def test_a_helper_verifies_but_does_not_control() -> None:
    control, desk, outbox = plane()
    await approve(control, outbox, HELPER, "helper")
    help_text = await control.handle_text(text(HELPER, "/help"))
    assert help_text and "You are a helper" in help_text.text and not help_text.buttons
    refused = await control.handle_text(text(HELPER, "/run website https://example.org"))
    assert refused and "Only operators" in refused.text and not refused.buttons  # no pointless request button
    status = await control.handle_text(text(HELPER, "/status"))
    assert status and "operators only" in status.text
    assert HELPER in desk.operators and HELPER not in desk.operators.controllers


@pytest.mark.asyncio
async def test_an_operator_controls_collection() -> None:
    control, desk, outbox = plane()
    await approve(control, outbox, OPERATOR, "operator")
    run = await control.handle_text(text(OPERATOR, "/run website https://example.org"))
    assert run and "Confirmation required" in run.text
    status = await control.handle_text(text(OPERATOR, "/status"))
    assert status and "Commands:" in status.text
    assert OPERATOR in desk.operators.controllers


@pytest.mark.asyncio
async def test_a_denial_is_final_for_a_day() -> None:
    control, desk, outbox = plane()
    await control.handle_callback(STRANGER, "access:request", "Ann", None)
    assert (await control.handle_callback(OWNER, f"access:deny:{request_id(outbox.to(OWNER)[0])}")).text.startswith("Denied")
    assert STRANGER not in desk.operators and "declined" in outbox.to(STRANGER)[-1].text
    assert "declined" in (await control.handle_callback(STRANGER, "access:request")).text
    store: MemoryAccessStore = desk.store  # type: ignore[assignment]
    for rid in store.decided_at:
        store.decided_at[rid] = datetime.now(UTC) - timedelta(days=2)
    assert "Request sent" in (await control.handle_callback(STRANGER, "access:request")).text


# --- managing --------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owners_list_change_and_revoke() -> None:
    control, desk, outbox = plane()
    await approve(control, outbox, HELPER, "helper")
    listing = await control.handle_text(text(OWNER, "/operators"))
    assert listing and "Ann (@ann), ID 21: helper" in listing.text
    assert "Only an owner" in (await control.handle_text(text(HELPER, "/operators"))).text  # type: ignore[union-attr]

    changed = await control.handle_text(text(OWNER, f"/role {HELPER} operator"))
    assert changed and changed.text == f"{HELPER} is now a operator."
    assert HELPER in desk.operators.controllers and "теперь вы оператор" in outbox.to(HELPER)[-1].text
    for bad in ("/role", f"/role {HELPER} admin", f"/role {OWNER} helper", f"/role {STRANGER} helper"):
        reply = await control.handle_text(text(OWNER, bad))
        assert reply and "now a" not in reply.text, bad

    assert "cannot be revoked" in (await control.handle_text(text(OWNER, f"/revoke {SECOND_OWNER}"))).text  # type: ignore[union-attr]
    assert "Only an owner" in (await control.handle_text(text(HELPER, f"/revoke {HELPER}"))).text  # type: ignore[union-attr]
    revoked = await control.handle_text(text(OWNER, f"/revoke {HELPER}"))
    assert revoked and revoked.text == f"Revoked: {HELPER}."
    assert HELPER not in desk.operators and "revoked" in outbox.to(HELPER)[-1].text
    after = await control.handle_text(text(HELPER, "/help"))
    assert after and after.buttons[-1].callback_data == "access:request"


@pytest.mark.asyncio
async def test_approvals_survive_a_restart() -> None:
    control, desk, outbox = plane()
    await approve(control, outbox, HELPER, "helper")
    fresh = OperatorSet({OWNER})
    await AccessDesk(desk.store, fresh).load()
    assert fresh.role(HELPER) == "helper"


# --- verification sees approvals ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_verification_service_picks_up_approved_people() -> None:
    from bot.verification.service import AccessDenied, FlowConfig, VerificationService
    from bot.verification.store import MemoryVerificationStore
    from tests.test_live_view import TOKEN, init_data
    from tests.test_verification_flow import FakeLive, FakeNotifier, FakeWatchdog, new_job, token_of

    store, notifier = MemoryVerificationStore(), FakeNotifier()
    store.roles = {HELPER: "helper"}
    service = VerificationService(store, FakeLive(), FakeWatchdog(), notifier,
                                  FlowConfig(public_url="https://x.sslip.io", operator_ids=OperatorSet({OWNER}), owner_id=OWNER, bot_token=TOKEN))
    job = store.add_job(new_job())
    await service.tick()
    [link] = notifier.links(HELPER)
    opened = await service.open(token_of(link), init_data(HELPER))
    assert opened.session.user_id == HELPER
    # Revoked in Telegram: gone after the next tick, page session included.
    store.roles = {}
    await service.tick()
    with pytest.raises(AccessDenied):
        await service.session(opened.cookie, job.id)


# --- on PostgreSQL ------------------------------------------------------------------------------

URL = os.environ.get("VERIFICATION_TEST_DATABASE_URL", "")


@pytest.mark.asyncio
@pytest.mark.skipif(not URL or not URL.split("?")[0].rstrip("/").endswith("_test"), reason="set VERIFICATION_TEST_DATABASE_URL to a *_test database")
async def test_the_postgres_access_store() -> None:
    from pathlib import Path

    import asyncpg

    from bot.control_plane.store import PostgresControlPlaneStore
    from bot.verification.store import PostgresVerificationStore

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in sorted((Path(__file__).resolve().parents[1] / "bot/services/db/migrations").glob("[0-9][0-9][0-9]_*.sql")):
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    base = PostgresControlPlaneStore(URL)
    await base.connect()
    verification = PostgresVerificationStore(URL)
    await verification.connect()
    try:
        store = PostgresAccessStore(base)
        request, status = await store.open_request(STRANGER, "Ann", "ann", timedelta(hours=24))
        assert status == "created" and request is not None
        assert (await store.open_request(STRANGER, "Ann", "ann", timedelta(hours=24)))[1] == "pending"
        decided = await store.decide(request.id, "helper", OWNER)
        assert decided is not None and decided.state == "approved"
        assert await store.decide(request.id, "operator", OWNER) is None
        assert await store.approved_roles() == {STRANGER: "helper"}
        assert await verification.approved_roles() == {STRANGER: "helper"}
        assert await store.set_role(STRANGER, "operator") and await store.operators() == [(STRANGER, "Ann", "ann", "operator")]
        assert await store.revoke(STRANGER, OWNER) and not await store.revoke(STRANGER, OWNER)
        assert await store.approved_roles() == {}

        denied, _ = await store.open_request(HELPER, None, None, timedelta(hours=24))
        await store.decide(denied.id, None, OWNER)  # type: ignore[union-attr]
        assert (await store.open_request(HELPER, None, None, timedelta(hours=24)))[1] == "recently_denied"
        assert (await store.open_request(HELPER, None, None, timedelta(seconds=0)))[1] == "created"
    finally:
        await verification.close()
        await base.close()
