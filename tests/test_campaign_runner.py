"""Campaign runner with in-memory stores and fakes: windows, streaming, status, pauses, cancel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.discovery import DiscoveryRefused
from bot.campaign.models import CampaignLimits
from bot.campaign.runner import (
    ANALYSIS,
    PROFILE_BUSY,
    QUEUE_BUSY,
    SEARCHING,
    VERIFY,
    CampaignRunner,
    MessageGone,
    RunnerConfig,
    TelegramMessenger,
)
from bot.campaign.runs import MemoryRunStore
from bot.orchestra.dispatcher import OrchestraDispatcher
from bot.orchestra.models import CommandState
from bot.orchestra.parser import CommandValidationError, parse_campaign
from tests.test_orchestra_dispatcher import OPERATORS, FakeStore, claimed

GOAL = "Найди квартиры в аренду в Мадриде"
CHAT = -100


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 9, 25, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


SUMMARY = "📊 Итог поиска"


class FakeMessenger:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, str]] = []  # (chat, message id, text)
        self.edits: list[tuple[int, int, str]] = []
        self.gone = False
        self.fail = 0
        self.deleted: list[tuple[int, int]] = []
        self.timeline: list[str] = []  # status texts in the order they were shown (sent or edited)

    async def send(self, chat_id: int, text: str) -> int:
        if self.fail:
            self.fail -= 1
            raise httpx.ConnectError("telegram unreachable")
        self.sent.append((chat_id, len(self.sent) + 1, text))
        if "🔎" not in text and not text.startswith(SUMMARY):  # cards, the summary and status messages
            self.timeline.append(text)
        return len(self.sent)

    async def edit(self, chat_id: int, message_id: int, text: str) -> None:
        if self.gone:
            self.gone = False
            raise MessageGone("message to edit not found")
        self.edits.append((chat_id, message_id, text))
        self.timeline.append(text)

    async def delete(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))

    def findings(self) -> list[str]:
        return [t for _, _, t in self.sent if "🔎" in t]

    def summaries(self) -> list[str]:
        return [t for _, _, t in self.sent if t.startswith(SUMMARY)]

    def statuses(self) -> list[str]:
        return list(self.timeline)


class FakeDiscovery:
    """Queues ``groups`` groups, or stops at a challenge like FacebookDiscovery does."""

    def __init__(self, campaigns: MemoryCampaignStore, store: MemoryRunStore, groups: int = 50) -> None:
        self.campaigns, self.store, self.groups = campaigns, store, groups
        self.calls: list[str] = []
        self.challenge = False
        self.refuse: str | None = None

    async def run(self, campaign_id: str) -> None:
        self.calls.append(campaign_id)
        if self.refuse:
            raise DiscoveryRefused(self.refuse)
        await self.campaigns.set_state(campaign_id, "discovering", "campaign:discovery")
        if self.challenge:
            self.store.profile = "human_verification_required"
            await self.campaigns.set_state(campaign_id, "paused_verification", "campaign:discovery",
                                           reason="facebook_challenge:facebook_url:/checkpoint")
            return
        campaign = await self.campaigns.get(campaign_id)
        self.store.add_groups(campaign_id, self.groups, window_size=campaign.plan.limits.window_size)
        await self.campaigns.set_state(campaign_id, "running", "campaign:discovery")


def make(max_groups: int = 50, max_windows: int | None = None, groups: int = 50, **config: float):
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    discovery = FakeDiscovery(campaigns, store, groups)
    clock = Clock()
    runner = CampaignRunner(campaigns, store, messenger, discovery, now=clock, owner_ids={7},
                            config=RunnerConfig(**{"window_cooldown_seconds": 120, "analysis_grace_seconds": 600,
                                                   "refusal_retry_seconds": 300, **config}))
    limits = {"max_groups": max_groups}
    if max_windows is not None:
        limits["max_windows"] = max_windows
    plan = plan_campaign(GOAL).model_copy(update={"limits": CampaignLimits(**limits)})
    return campaigns, store, messenger, discovery, clock, runner, plan


async def create(campaigns: MemoryCampaignStore, plan) -> str:
    return await campaigns.create(plan, chat_id=CHAT, requested_by=7, source_text=GOAL, actor="telegram:7")


def only_batch(store: MemoryRunStore, cid: str) -> str:
    return next(b for _, b, s in store.windows[cid] if s == "active")


async def test_windows_hold_at_most_20_groups_chain_in_order_and_wait_for_the_cooldown() -> None:
    campaigns, store, messenger, discovery, clock, runner, plan = make(max_groups=50, groups=50)
    cid = await create(campaigns, plan)

    await runner.tick()
    assert discovery.calls == [cid] and (await campaigns.get(cid)).state == "running"
    first = store.batches[only_batch(store, cid)]
    assert first.urls == [f"https://www.facebook.com/groups/g{n:03d}/" for n in range(1, 21)]

    seen = [first.urls]
    for expected in (range(21, 41), range(41, 51)):
        store.finish_batch(first.id)
        await runner.tick()  # window closes, cooldown starts
        assert await store.open_window(cid) is None
        clock.advance(119)
        await runner.tick()
        assert await store.open_window(cid) is None, "no window before the cooldown passed"
        clock.advance(2)
        await runner.tick()
        first = store.batches[only_batch(store, cid)]
        assert first.urls == [f"https://www.facebook.com/groups/g{n:03d}/" for n in expected]
        seen.append(first.urls)
    assert all(len(urls) <= 20 for urls in seen)
    assert len({u for urls in seen for u in urls}) == 50

    store.finish_batch(first.id)
    await runner.tick()
    clock.advance(121)
    await runner.tick()
    campaign = await campaigns.get(cid)
    assert (campaign.state, campaign.stop_reason) == ("completed", "queue_exhausted")
    assert {g.state for g in store.groups[cid]} == {"collected"}
    assert messenger.statuses()[-1].endswith("Кампания завершена · найдено 0")
    assert discovery.calls == [cid]


async def test_max_windows_is_respected_even_with_groups_left() -> None:
    campaigns, store, _, _, clock, runner, plan = make(max_groups=60, max_windows=2, groups=60)
    cid = await create(campaigns, plan)
    for _ in range(2):
        await runner.tick()
        store.finish_batch(only_batch(store, cid))
        await runner.tick()
        clock.advance(121)
    await runner.tick()
    campaign = await campaigns.get(cid)
    assert (campaign.state, campaign.stop_reason) == ("completed", "max_windows")
    assert len(store.windows[cid]) == 2
    assert sum(g.state == "queued" for g in store.groups[cid]) == 20


async def test_window_size_below_20_is_used_and_never_exceeded() -> None:
    campaigns, store, _, _, _, runner, _ = make()
    plan = plan_campaign(GOAL).model_copy(update={"limits": CampaignLimits(max_groups=12, window_size=5)})
    cid = await create(campaigns, plan)
    await runner.tick()
    assert len(store.batches[only_batch(store, cid)].urls) == 5
    with pytest.raises(ValueError):
        await store.start_window(cid, [g for g in await store.next_groups(cid, 100)] * 3, vertical="both", actor="x")
    assert len(await store.next_groups(cid, 100)) <= 20


async def test_findings_stream_once_in_order_and_survive_a_restart() -> None:
    campaigns, store, messenger, discovery, clock, runner, plan = make()
    cid = await create(campaigns, plan)
    await runner.tick()
    store.add_finding(cid, "f1", "🏠 first")
    store.add_finding(cid, "f2", "🏠 second")
    await runner.tick()
    assert messenger.findings() == ["🏠 first\n\n🔎 Найдено: 1 · ищу дальше", "🏠 second\n\n🔎 Найдено: 2 · ищу дальше"]
    assert all(chat == CHAT for chat, _, _ in messenger.sent)
    assert store.findings_state == {"f1": "delivered", "f2": "delivered"}

    restarted = CampaignRunner(campaigns, store, messenger, discovery, now=clock, owner_ids={7})
    await restarted.tick()
    await restarted.tick()
    assert len(messenger.findings()) == 2

    store.add_finding(cid, "f3", "🏠 third")
    messenger.fail = 1  # Telegram down: nothing is recorded, it is retried
    await restarted.tick()
    assert len(messenger.findings()) == 2 and "f3" not in store.streamed[cid]
    await restarted.tick()
    assert messenger.findings()[-1] == "🏠 third\n\n🔎 Найдено: 3 · ищу дальше"
    assert await store.streamed_count(cid) == 3


async def test_status_is_one_message_edited_only_on_change_and_resent_when_lost() -> None:
    campaigns, store, messenger, discovery, _, runner, plan = make()
    store.profile = "in_use"  # facebook-runner holds the profile
    cid = await create(campaigns, plan)
    await runner.tick()
    campaign = await campaigns.get(cid)
    assert campaign.status_message_id == 1
    assert [t for _, _, t in messenger.sent] == [f"🎯 {plan.goal}\n{PROFILE_BUSY}"]
    await runner.tick()
    await runner.tick()
    assert messenger.edits == [] and len(messenger.sent) == 1  # unchanged text: no edit at all

    store.profile = "ready"
    discovery.refuse = "facebook_profile_not_ready"  # lost the race for the profile
    await runner.tick()
    assert [t for _, _, t in messenger.edits] == [f"🎯 {plan.goal}\n{SEARCHING}", f"🎯 {plan.goal}\n{PROFILE_BUSY}"]

    discovery.refuse = None
    messenger.gone = True  # the user deleted the status message
    await runner.tick()
    campaign = await campaigns.get(cid)
    assert campaign.status_message_id not in (None, 1)
    assert messenger.sent[-1][2] == f"🎯 {plan.goal}\n{SEARCHING}"
    # later edits go to the new message
    assert messenger.edits[-1][1] == campaign.status_message_id
    assert (await store.get_run(cid)).status_text == messenger.edits[-1][2]


async def test_a_challenged_window_pauses_the_campaign_until_verification_requeues_it() -> None:
    campaigns, store, messenger, _, _, runner, plan = make()
    cid = await create(campaigns, plan)
    await runner.tick()
    batch = only_batch(store, cid)
    store.batches[batch].state = "running"
    store.batches[batch].items[store.batches[batch].urls[3]] = "running"
    await runner.tick()
    assert messenger.statuses()[-1].endswith("Сейчас: Facebook · Group 4 · ищу дальше")

    store.batches[batch].state = "human_verification_required"
    store.profile = "human_verification_required"
    await runner.tick()
    campaign = await campaigns.get(cid)
    assert campaign.state == "paused_verification" and campaign.stop_reason.startswith("batch_verification:")
    assert messenger.statuses()[-1].endswith(VERIFY)
    await runner.tick()
    assert len(store.windows[cid]) == 1, "no new window while paused"

    store.batches[batch].state = "queued"  # the verification flow resumed it
    store.profile = "ready"
    await runner.tick()
    assert (await campaigns.get(cid)).state == "running"
    store.finish_batch(batch)
    await runner.tick()
    assert {g.state for g in store.groups[cid] if g.batch_id == batch} == {"collected"}


async def test_a_discovery_challenge_waits_for_the_profile_then_discovery_resumes() -> None:
    campaigns, store, messenger, discovery, _, runner, plan = make()
    discovery.challenge = True
    cid = await create(campaigns, plan)
    await runner.tick()
    assert (await campaigns.get(cid)).state == "paused_verification"
    assert messenger.statuses()[-1].endswith(VERIFY)
    await runner.tick()
    assert discovery.calls == [cid], "never retried while the profile waits for a human"

    discovery.challenge = False
    store.profile = "ready"
    await runner.tick()
    assert discovery.calls == [cid, cid]
    assert (await campaigns.get(cid)).state == "running" and await store.open_window(cid) is not None


async def test_a_breaker_before_discovery_finishes_the_search_without_facebook() -> None:
    campaigns, store, messenger, discovery, _, runner, plan = make()
    cid = await create(campaigns, plan)
    store.breaker = "safety breaker open: 2 facebook challenges in the last 6 h"
    await runner.tick()
    campaign = await campaigns.get(cid)
    assert discovery.calls == [] and store.batches == {}
    assert (campaign.state, campaign.stop_reason) == ("completed", "facebook_breaker")
    assert messenger.statuses()[-1].endswith("Кампания завершена · найдено 0 · лимит Facebook на сегодня исчерпан")


async def test_the_daily_quota_finishes_the_search_and_never_bypasses_it() -> None:
    campaigns, store, messenger, _, _, runner, plan = make()
    cid = await create(campaigns, plan)
    store.refusal = "daily quota reached: 6 of 6 Facebook batches in 24 h"
    await runner.tick()
    campaign = await campaigns.get(cid)
    assert store.batches == {}, "a refused window is never planned"
    assert (campaign.state, campaign.stop_reason) == ("completed", "facebook_daily_limit")
    assert messenger.statuses()[-1].endswith("лимит Facebook на сегодня исчерпан")


async def test_after_the_daily_quota_the_search_waits_for_the_sites_then_finishes() -> None:
    from bot.web_search.models import WebStatus

    class Web:
        status = WebStatus(True, "idealista.com", "сайты: страниц 3/60")

        async def web_status(self, campaign_id: str) -> WebStatus:
            return self.status

    campaigns, store, _, _, clock, runner, plan = make()
    web = Web()
    runner.web = web
    cid = await create(campaigns, plan)
    store.refusal = "daily quota reached: 60 of 60 Facebook group reads in 24 h"
    await runner.tick()
    assert (await campaigns.get(cid)).state == "running"  # the sites are still being read
    web.status = WebStatus(False)
    clock.advance(301)
    await runner.tick()
    campaign = await campaigns.get(cid)
    assert (campaign.state, campaign.stop_reason) == ("completed", "facebook_daily_limit")
    assert store.batches == {}


async def test_the_quota_goes_to_live_groups_and_skips_empty_ones() -> None:
    campaigns, store, _, _, _, runner, plan = make(max_groups=50, groups=30)
    cid = await create(campaigns, plan)
    await runner.tick()  # discovery queues 30 groups, window 1 is planned from the first 20
    first = store.batches[only_batch(store, cid)].urls
    assert first[0].endswith("/g001/")
    store.finish_batch(only_batch(store, cid))
    await runner.tick()

    store.empty_groups = {f"https://www.facebook.com/groups/g{n:03d}/" for n in (21, 22, 23)}  # read lately, nothing
    store.live_groups = {"https://www.facebook.com/groups/g030/"}                               # recent findings
    groups = await store.next_groups(cid, 20)
    assert groups[0].canonical_url.endswith("/g030/")
    assert not {g.canonical_url for g in groups} & store.empty_groups
    assert {g.group_key for g in store.groups[cid] if g.state == "skipped"} == {"g021", "g022", "g023"}


async def test_cancel_stops_new_windows_and_cancels_the_in_flight_batch() -> None:
    campaigns, store, messenger, _, clock, runner, plan = make()
    cid = await create(campaigns, plan)
    await runner.tick()
    batch = only_batch(store, cid)
    assert await campaigns.cancel(cid, "telegram:7") is True
    await runner.tick()
    assert store.cancelled_batches == [batch]
    assert await store.open_window(cid) is None
    assert {g.state for g in store.groups[cid] if g.batch_id == batch} == {"skipped"}
    assert messenger.statuses()[-1].endswith("Кампания остановлена")
    clock.advance(1000)
    await runner.tick()
    assert len(store.windows[cid]) == 1
    assert await campaigns.cancel(cid, "telegram:7") is False


async def test_completion_waits_for_analysis_within_a_bounded_grace() -> None:
    campaigns, store, messenger, _, clock, runner, plan = make(max_groups=20, groups=20)
    cid = await create(campaigns, plan)
    await runner.tick()
    store.finish_batch(only_batch(store, cid))
    store.normalised[cid] = 3
    await runner.tick()
    clock.advance(121)
    await runner.tick()
    assert (await campaigns.get(cid)).state == "running"
    assert messenger.statuses()[-1].endswith(ANALYSIS)
    store.add_finding(cid, "late", "🏠 late")
    clock.advance(600)
    await runner.tick()
    assert (await campaigns.get(cid)).state == "completed"
    assert messenger.findings() == ["🏠 late\n\n🔎 Найдено: 1 · ищу дальше"]
    assert messenger.statuses()[-1].endswith("Кампания завершена · найдено 1")


async def test_only_one_campaign_uses_facebook_at_a_time() -> None:
    campaigns, store, _, discovery, _, runner, plan = make()
    first = await create(campaigns, plan)
    second = await create(campaigns, plan)
    await runner.tick()
    assert discovery.calls == [first]
    assert (await campaigns.get(second)).state == "planned"
    assert (await store.get_run(second)).status_text.endswith(QUEUE_BUSY)


async def test_an_unexpected_error_fails_the_campaign_with_the_reason() -> None:
    campaigns, store, messenger, _, _, runner, plan = make()
    cid = await create(campaigns, plan)

    async def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    store.start_window = broken  # type: ignore[method-assign]
    await runner.tick()
    campaign = await campaigns.get(cid)
    assert (campaign.state, campaign.stop_reason) == ("failed", "runner_error:RuntimeError")
    assert messenger.statuses()[-1].endswith("Кампания остановлена · ошибка: runner_error:RuntimeError")


async def test_telegram_edit_treats_not_modified_as_success_and_missing_as_gone() -> None:
    replies = iter([
        httpx.Response(400, json={"ok": False, "description": "Bad Request: message is not modified"}),
        httpx.Response(400, json={"ok": False, "description": "Bad Request: message to edit not found"}),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 55}}),
    ])
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        return next(replies)

    messenger = TelegramMessenger("t", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await messenger.edit(1, 2, "same")
    with pytest.raises(MessageGone):
        await messenger.edit(1, 2, "other")
    assert await messenger.send(1, "new") == 55
    assert calls == ["editMessageText", "editMessageText", "sendMessage"]
    await messenger.aclose()


# --- /campaign in the Orchestra ------------------------------------------------------------


def test_campaign_arguments_parse_into_plan_status_and_cancel() -> None:
    assert parse_campaign("  квартиры в Мадриде ") == ("plan", "квартиры в Мадриде")
    assert parse_campaign("status") == ("status", "")
    assert parse_campaign("Cancel abc") == ("cancel", "abc")
    for bad in ("", "cancel", "cancel a b"):
        with pytest.raises(CommandValidationError):
            parse_campaign(bad)


async def _dispatch(command: str, arguments: str, campaigns: MemoryCampaignStore) -> tuple[FakeStore, list[str]]:
    store = FakeStore([claimed(command, arguments)])
    notices: list[str] = []

    async def notify(_chat: int, text: str) -> None:
        notices.append(text)

    dispatcher = OrchestraDispatcher(store, operator_ids=OPERATORS, notifier=notify, campaigns=campaigns)  # type: ignore[arg-type]
    assert await dispatcher.process_once()
    return store, notices


async def test_campaign_command_plans_stores_reports_and_cancels() -> None:
    campaigns = MemoryCampaignStore()
    store, notices = await _dispatch("campaign", GOAL, campaigns)
    (campaign,) = campaigns.campaigns.values()
    assert (campaign.chat_id, campaign.requested_by, campaign.state, campaign.source_text) == (10, 20, "planned", GOAL)
    assert notices[-1] == f"Кампания {campaign.id} запланирована: {campaign.plan.goal}"
    assert store.completed[-1][1] == CommandState.FINISHED

    _, notices = await _dispatch("campaign", "status", campaigns)
    assert notices[-1].startswith(f"Кампания {campaign.id}: запланирована")

    _, notices = await _dispatch("campaign", f"cancel {campaign.id}", campaigns)
    assert notices[-1] == f"Кампания {campaign.id} остановлена."
    assert (await campaigns.get(campaign.id)).state == "cancelled"


async def test_an_unplannable_goal_answers_with_the_architects_message() -> None:
    campaigns = MemoryCampaignStore()
    store, notices = await _dispatch("campaign", "найди что-нибудь", campaigns)
    assert campaigns.campaigns == {}
    assert store.completed[-1][3] == "invalid_goal"
    assert "Orchestra" not in notices[-1] and notices[-1]


async def test_startup_frees_a_profile_left_in_use_by_a_crashed_discovery_and_resumes() -> None:
    campaigns, store, _, discovery, _, runner, plan = make()
    cid = await create(campaigns, plan)
    await campaigns.set_state(cid, "discovering", "campaign:discovery")  # the old process died here
    store.profile = "in_use"
    store.collector_running = True  # facebook-runner really holds it: not freed
    assert await runner.recover() == 0 and store.profile == "in_use"
    store.collector_running = False
    assert await runner.recover() == 1 and store.profile == "ready"
    assert store.recovered == ["campaign:runner:recovery"]
    await runner.tick()
    assert discovery.calls == [cid] and (await campaigns.get(cid)).state == "running"


async def test_the_status_moves_below_each_new_card_and_the_old_one_is_deleted() -> None:
    campaigns, store, messenger, _, _, runner, plan = make()
    cid = await create(campaigns, plan)
    await runner.tick()
    first_status = (await campaigns.get(cid)).status_message_id
    await runner.tick()
    assert messenger.deleted == []  # no card: the status stays and is only edited

    store.add_finding(cid, "f1", "🏠 first")
    await runner.tick()
    after_first = (await campaigns.get(cid)).status_message_id
    kinds = ["card" if "🔎" in t else "status" if t.startswith("🎯") else "other" for _, _, t in messenger.sent]
    assert kinds[-2:] == ["card", "status"]  # the status is the last message in the chat
    assert messenger.deleted == [(CHAT, first_status)] and after_first != first_status

    store.add_finding(cid, "f2", "🏠 second")
    await runner.tick()
    assert messenger.deleted[-1] == (CHAT, after_first)
    assert [t for _, _, t in messenger.sent][-1].startswith("🎯")
    assert (await campaigns.get(cid)).status_message_id == messenger.sent[-1][1]
