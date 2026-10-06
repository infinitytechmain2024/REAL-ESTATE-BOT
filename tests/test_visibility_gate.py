"""Admin vs user visibility: only the owner sees technical replies.

A normal user (role ``user``) and anyone without access get short Russian
lines for every technical command, voice refusal, stale button and duplicate
update; the owner keeps ids, English and the full controls; operators keep
their command rights. A user stops their own search with «стоп».
"""

from __future__ import annotations

import re

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.control_plane.models import CommandEnvelope, IncomingMessage, Reply
from bot.control_plane.service import NO_ACTIVE_SEARCH, SEARCH_STOPPED, ControlPlane
from bot.orchestra.dispatcher import OrchestraDispatcher
from bot.orchestra.models import ClaimedCommand, CommandReceipt, CommandState
from tests.test_orchestra_dispatcher import FakeStore
from tests.test_user_intake import (
    OPERATOR,
    OTHER_USER,
    OWNER,
    STRANGER,
    USER,
    FakeTranscriber,
    Sink,
    callbacks,
    plane,
    press,
    say,
    text,
)

TECHNICAL = (
    "/run website https://example.org", "/run facebook-groups https://facebook.com/groups/1",
    "/pause all", "/resume all", "/cancel all", "/cancel batch:0b7a1f5e-7c56-4c3b-9d0e-1a2b3c4d5e6f",
    "/login", "/status", "/operators", "/role 5 operator", "/revoke 5", "/auto on", "/auto status",
    "/settings", "/campaign", "/campaign status", "/campaign cancel 0b7a1f5e-7c56-4c3b-9d0e-1a2b3c4d5e6f",
    "/nonsense", "confirm 0123456789",
)
ALLOWED_LATIN = {"Facebook"}
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_URL = re.compile(r"https?://\S+")
FORBIDDEN = ("window", "batch", "campaign", "orchestra", "20 групп", "queue", "operator", "confirm", "Queue id")


def assert_plain_russian(reply: Reply | None, context: str = "") -> None:
    assert reply is not None, context
    body = _URL.sub("", reply.text).replace("/start", "")  # «Нажмите /start» is the one command users know
    latin = [w for w in re.findall(r"[A-Za-z]+", body) if len(w) > 3 and w not in ALLOWED_LATIN]
    assert not latin, (context, reply.text)
    assert not _UUID.search(reply.text), (context, reply.text)
    assert re.search(r"[А-Яа-яЁё]", reply.text), (context, reply.text)
    lowered = reply.text.lower()
    assert not any(word.lower() in lowered for word in FORBIDDEN), (context, reply.text)


async def download() -> bytes:
    return b"OggS"


def voice(user: int) -> IncomingMessage:
    return IncomingMessage(user, user, _next(), voice_file_id="v", voice_size=100, voice_duration_seconds=3)


_counter = iter(range(900_000, 1_000_000))


def _next() -> int:
    return next(_counter)


# --- a user and a stranger see only plain Russian --------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("who", [USER, STRANGER])
async def test_every_technical_command_gets_a_plain_russian_reply(who: int) -> None:
    control, sink, _ = plane()
    for body in TECHNICAL:
        reply = await say(control, who, body)
        assert_plain_russian(reply, body)
    # Only the user's /campaign status reaches the Orchestra (it answers with a user-safe label).
    assert [e.arguments for e in sink.envelopes] == (["status"] if who == USER else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("who", [USER, STRANGER])
async def test_voice_and_plain_text_are_plain_russian(who: int) -> None:
    control, _, _ = plane(FakeTranscriber("статус"))
    reply = await control.handle_voice(voice(who), download)
    assert_plain_russian(reply, "voice")  # never a transcript or a mapped command
    assert_plain_russian(await say(control, who, "привет"), "text")


@pytest.mark.asyncio
async def test_a_stranger_is_pointed_at_the_access_button_without_their_id() -> None:
    control, _, _ = plane()
    reply = await say(control, STRANGER, "/run website https://example.org")
    assert reply.text.startswith("Эта команда недоступна.") and str(STRANGER) not in reply.text
    assert "access:request" in callbacks(reply)
    refused = await control.handle_voice(voice(STRANGER), download)
    assert refused and "access:request" in callbacks(refused)
    user_reply = await say(control, USER, "/status")
    assert user_reply.text == "Эта команда недоступна. Опишите, что ищете, — я начну поиск."
    assert "access:request" not in callbacks(user_reply)


@pytest.mark.asyncio
@pytest.mark.parametrize("who", [USER, STRANGER])
async def test_stale_buttons_and_forwarded_live_buttons_are_russian(who: int) -> None:
    control, _, _ = plane()
    for data in ("live:done:not-a-uuid", "live:approve:abc", "access:bogus:x", "other:thing", ""):
        assert_plain_russian(await press(control, who, data), data)


@pytest.mark.asyncio
@pytest.mark.parametrize("who", [USER, STRANGER])
async def test_a_duplicate_update_is_silent_for_non_operators(who: int) -> None:
    control, _, _ = plane()
    message = text(who, "/status")
    assert await control.handle_text(message) is not None
    assert await control.handle_text(message) is None
    note = voice(who)
    assert await control.handle_voice(note, download) is not None
    assert await control.handle_voice(note, download) is None


@pytest.mark.asyncio
async def test_access_request_replies_to_the_requester_are_russian() -> None:
    control, _, outbox = plane()
    assert_plain_russian(await press(control, STRANGER, "access:request"), "request")
    assert_plain_russian(await press(control, STRANGER, "access:request"), "again")
    assert_plain_russian(await press(control, USER, "access:request"), "already")
    owner_notice = outbox.to(OWNER)[-1]
    assert "Access request" in owner_notice.text  # owners keep the technical request


# --- the owner and operators keep the technical controls --------------------------------------


@pytest.mark.asyncio
async def test_the_owner_still_gets_technical_replies() -> None:
    control, _, _ = plane()
    assert (await say(control, OWNER, "/status")).text.startswith("Control plane is online.")
    assert (await say(control, OWNER, "/campaign status")).text == "Campaign status requested."
    assert "Owners (from .env)" in (await say(control, OWNER, "/operators")).text
    cancel = await say(control, OWNER, "/campaign cancel 0b7a1f5e-7c56-4c3b-9d0e-1a2b3c4d5e6f")
    assert "Confirmation required for /campaign" in cancel.text
    assert "/operators" in (await say(control, OWNER, "/help")).text
    message = text(OWNER, "/status")
    await control.handle_text(message)
    assert (await control.handle_text(message)).text == "Duplicate update ignored."  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_an_operator_can_still_run() -> None:
    control, _, _ = plane()
    reply = await say(control, OPERATOR, "/run website https://example.org")
    assert "Confirmation required for /run" in reply.text
    token = reply.text.split("confirm ", 1)[1].split()[0]
    assert (await say(control, OPERATOR, f"confirm {token}")).text.startswith("Confirmed: /run")


# --- stopping one's own search -----------------------------------------------------------------


class Orchestra:
    """The control plane's sink wired to a real dispatcher over in-memory stores."""

    def __init__(self, campaigns: MemoryCampaignStore, control: ControlPlane) -> None:
        self.store = FakeStore([])
        self.notices: list[tuple[int, str]] = []
        self.envelopes: list[CommandEnvelope] = []

        async def notify(chat: int, message: str) -> None:
            self.notices.append((chat, message))

        roles = control.operators
        self.dispatcher = OrchestraDispatcher(self.store, operator_ids=roles.controllers, notifier=notify,  # type: ignore[arg-type]
                                              campaigns=campaigns, roles=roles)

    async def __call__(self, envelope: CommandEnvelope) -> CommandReceipt:
        self.envelopes.append(envelope)
        self.store.items.append(ClaimedCommand(f"cmd-{len(self.envelopes)}", envelope.command, envelope.arguments,
                                               envelope.chat_id, envelope.user_id))
        while await self.dispatcher.process_once():
            pass
        return CommandReceipt(f"cmd-{len(self.envelopes)}", CommandState.QUEUED)


async def stoppable(chat: int = USER, requested_by: int = USER) -> tuple[ControlPlane, MemoryCampaignStore, Orchestra, str]:
    control, _, _ = plane()
    campaigns = MemoryCampaignStore()
    campaign_id = await campaigns.create(plan_campaign("квартиры в аренду в Мадриде"), chat_id=chat,
                                         requested_by=requested_by, source_text="x", actor="t")
    await campaigns.set_state(campaign_id, "discovering", "t")
    orchestra = Orchestra(campaigns, control)
    control.command_sink = orchestra
    control.intake.sink = orchestra
    control.campaigns = campaigns
    return control, campaigns, orchestra, campaign_id


@pytest.mark.asyncio
@pytest.mark.parametrize("word", ["стоп", "Стоп!", "остановить поиск", "Отмена", "/campaign cancel"])
async def test_a_user_stops_their_own_running_search(word: str) -> None:
    control, campaigns, orchestra, campaign_id = await stoppable()
    reply = await say(control, USER, word)
    assert reply.text == SEARCH_STOPPED == "Поиск остановлен."
    assert campaigns.campaigns[campaign_id].state == "cancelled"
    assert orchestra.envelopes[-1].arguments == f"cancel {campaign_id}"
    assert orchestra.notices == []  # no second line: the runner's status message turns final
    # Nothing left to stop.
    assert (await say(control, USER, "стоп")).text == NO_ACTIVE_SEARCH == "Сейчас нет активного поиска."
    assert len(orchestra.envelopes) == 1


@pytest.mark.asyncio
async def test_otmena_while_writing_a_task_drops_the_draft_not_the_search() -> None:
    control, campaigns, orchestra, campaign_id = await stoppable()
    await press(control, USER, "mode:real_estate")
    assert "Сколько комнат" in (await say(control, USER, "квартиры в аренду в Мадриде до 1200 €")).text
    assert (await say(control, USER, "Отмена")).text.startswith("Черновик удалён.")
    assert campaigns.campaigns[campaign_id].state == "discovering" and orchestra.envelopes == []
    # «стоп» always means the search.
    assert (await say(control, USER, "стоп")).text == SEARCH_STOPPED


@pytest.mark.asyncio
async def test_nobody_else_can_stop_a_users_search() -> None:
    # A group chat: the latest campaign there belongs to USER.
    control, campaigns, orchestra, campaign_id = await stoppable(chat=-500)

    def in_group(user: int, body: str) -> IncomingMessage:
        return IncomingMessage(chat_id=-500, user_id=user, message_id=_next(), text=body)

    for someone in (OTHER_USER, STRANGER):
        reply = await control.handle_text(in_group(someone, "стоп"))
        assert reply is not None and reply.text != SEARCH_STOPPED
        assert_plain_russian(reply, str(someone))
    assert orchestra.envelopes == [] and campaigns.campaigns[campaign_id].state == "discovering"
    # Even a cancel queued by someone else is refused by the Orchestra's ownership check.
    await orchestra(CommandEnvelope("campaign", f"cancel {campaign_id}", OTHER_USER, OTHER_USER, _next()))
    assert campaigns.campaigns[campaign_id].state == "discovering"
    assert orchestra.notices == [(OTHER_USER, "Этот поиск уже завершён или недоступен.")]
    # The owner may stop it.
    reply = await control.handle_text(in_group(OWNER, "стоп"))
    assert reply is not None and reply.text == SEARCH_STOPPED
    assert campaigns.campaigns[campaign_id].state == "cancelled"


@pytest.mark.asyncio
async def test_a_finished_search_cannot_be_stopped_and_a_failure_is_russian() -> None:
    control, campaigns, orchestra, campaign_id = await stoppable()
    await campaigns.cancel(campaign_id, "t")
    assert (await say(control, USER, "стоп")).text == NO_ACTIVE_SEARCH
    assert orchestra.envelopes == []

    control, campaigns, orchestra, campaign_id = await stoppable()

    async def broken(_envelope: CommandEnvelope) -> CommandReceipt:
        raise ConnectionError("db down")

    control.command_sink = broken
    assert_plain_russian(await say(control, USER, "стоп"), "broken")
    assert campaigns.campaigns[campaign_id].state == "discovering"


@pytest.mark.asyncio
async def test_the_launch_reply_tells_a_user_how_to_stop() -> None:
    control, sink, _ = plane()
    assert isinstance(sink, Sink)
    await press(control, USER, "mode:real_estate")
    await say(control, USER, "квартиры в аренду в Мадриде до 1200 €")
    await press(control, USER, "task:enough")
    launched = await press(control, USER, "task:launch")
    assert "напишите «стоп»" in launched.text
    assert_plain_russian(launched, "launch")
