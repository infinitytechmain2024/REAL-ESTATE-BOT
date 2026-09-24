"""/status: a real summary for operators, a bare liveness line for everyone else."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from bot.control_plane.models import IncomingMessage, StatusSnapshot
from bot.control_plane.service import ControlPlane, format_status
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore


def control(store: MemoryControlPlaneStore) -> ControlPlane:
    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x", operator_user_ids=frozenset({11}))
    return ControlPlane(settings, store, None, lambda _: None)


def status(message_id: int, user_id: int | None) -> IncomingMessage:
    return IncomingMessage(chat_id=22, user_id=user_id, message_id=message_id, text="/status")


@pytest.mark.asyncio
async def test_operator_sees_the_real_counts() -> None:
    store = MemoryControlPlaneStore()
    store.snapshot = StatusSnapshot(
        commands_queued=1, batches_active=2, last_batch_finished_at=datetime(2026, 9, 24, 18, 2, tzinfo=UTC),
        sources_active=5, sources_paused=1, profiles_ready=1, posts_last_24h=42,
        voice_notes_30d=2, voice_cost_usd_30d=Decimal("0.0000359640"),
    )
    reply = await control(store).handle_text(status(1, 11))
    assert reply is not None
    text = reply.text
    assert text.startswith("Control plane is online.")
    assert "not implemented" not in text
    assert "Commands: 1 queued, 0 running." in text
    assert "Batches: 2 active, 0 need verification; last finished 2026-09-24 18:02 UTC." in text
    assert "Sources: 5 active, 1 paused" in text
    assert "Posts collected (24h): 42." in text
    assert "Voice (30 days): 2 notes, $0.000036." in text
    assert "Needs a human" not in text


def test_items_waiting_for_a_human_are_called_out() -> None:
    text = format_status(StatusSnapshot(runs_need_verification=1, profiles_need_attention=1))
    assert "Needs a human: 2 item(s)" in text
    assert "last finished never" in text and "Voice (30 days): 0 notes, $0." in text


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [999, None])
async def test_non_operators_only_learn_the_bot_is_online(user_id: int | None) -> None:
    store = MemoryControlPlaneStore()
    store.snapshot = StatusSnapshot(sources_active=5, voice_cost_usd_30d=Decimal("1.5"))
    reply = await control(store).handle_text(status(2, user_id))
    assert reply and reply.text == "Control plane is online. Detailed status is shown to operators only."


@pytest.mark.asyncio
async def test_a_failing_status_query_does_not_break_the_bot() -> None:
    class BrokenStore(MemoryControlPlaneStore):
        async def status_snapshot(self) -> StatusSnapshot:
            raise ConnectionError("database down")

    reply = await control(BrokenStore()).handle_text(status(3, 11))
    assert reply and "status query failed" in reply.text
