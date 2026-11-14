"""Q40: the last status line of a finished search counts every delivery (STATUS_COUNTS_ALL_DELIVERIES).

Before, only object cards (``streamed_count``) counted: a sent reach contact or a lead from a post sent by link was
followed by «Пока ничего подходящего не нашёл.».
"""

from __future__ import annotations

from bot.campaign.runs import PostReadSummary
from bot.campaign.settings import CampaignRunnerSettings
from bot.campaign.status_text import NOTHING
from tests.test_empty_investor_report import _campaign, _run, needs_db
from tests.test_empty_investor_report import pool as pool
from tests.test_post_leads import POST, _person, _runner


async def _reach_search(flag: bool, *, sent: bool) -> str:
    campaigns, store, messenger, clock, web, runner, cid = await _campaign(status_counts_all_deliveries=flag)

    def deliver() -> None:
        if sent:
            store.deliveries[(cid, "reach:linkedin.com/in/someone")] = 99  # one reach contact sent, no lead

    await _run(campaigns, runner, clock, web, cid, deliver=deliver)
    assert (await campaigns.get(cid)).state == "completed"
    return messenger.statuses()[-1]


async def _post_link(flag: bool, *, leads: int) -> str:
    campaigns, store, messenger, clock, runner, cid = await _runner(status_counts_all_deliveries=flag)
    store.post_read_rows[cid] = [PostReadSummary(POST, "done", 30, leads, 0.09)]
    store.post_people[cid] = [_person(n) for n in range(leads)]
    for _ in range(4):
        clock.advance(30)
        await runner.step(cid)
    assert (await campaigns.get(cid)).state == "completed"
    assert sum("Профиль:" in t for _, _, t in messenger.sent) == leads
    return await runner._status_text(await campaigns.get(cid), "")  # the user's line (the runner's owner is 7)


async def test_flag_on_a_sent_reach_contact_is_counted() -> None:
    assert await _reach_search(True, sent=True) == "✅ Готово: отправлено 1"


async def test_flag_on_a_lead_of_a_post_sent_by_link_is_counted() -> None:
    assert await _post_link(True, leads=1) == "✅ Готово: отправлено 1"


async def test_flag_on_nothing_sent_is_still_nothing_found() -> None:
    assert await _reach_search(True, sent=False) == NOTHING
    assert await _post_link(True, leads=0) == NOTHING


async def test_flag_off_only_object_cards_count_as_before() -> None:
    assert await _reach_search(False, sent=True) == NOTHING
    assert await _post_link(False, leads=1) == NOTHING


def test_the_flag_is_off_by_default_and_reaches_the_runner(monkeypatch) -> None:
    monkeypatch.delenv("STATUS_COUNTS_ALL_DELIVERIES", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert CampaignRunnerSettings(_env_file=None).status_counts_all_deliveries is False
    monkeypatch.setenv("STATUS_COUNTS_ALL_DELIVERIES", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().status_counts_all_deliveries is True


async def test_flag_on_a_merged_reach_card_with_two_keys_is_one_message() -> None:
    from bot.campaign import MemoryCampaignStore, plan_campaign
    from bot.campaign.runner import CampaignRunner, RunnerConfig
    from bot.campaign.runs import MemoryRunStore
    from tests.test_campaign_runner import CHAT, Clock, FakeMessenger
    from tests.test_investor_people import SPEC, _contact

    campaigns = MemoryCampaignStore()
    store, messenger = MemoryRunStore(campaigns), FakeMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, now=Clock(),
                            config=RunnerConfig(relevance_fail_closed=False, max_people=10,
                                                status_counts_all_deliveries=True))
    goal = "Найди инвесторов в Мадриде"
    cid = await campaigns.create(plan_campaign(goal), chat_id=CHAT, requested_by=8, source_text=goal, actor="t",
                                 spec=SPEC)
    await campaigns.set_state(cid, "running", "campaign:test")
    store.contacts["Madrid"] = [  # the same person on two platforms: one card, two delivery keys
        _contact(5, "fund", "Juan Pérez", platform="linkedin", title="Madrid"),
        _contact(6, "fund", "juan perez", title="Madrid fondo", profile_text="Fondo", contacts={"emails": ["j@b.es"]}),
    ]
    await runner.step(cid)
    assert sum("Соответствие" in t for _, _, t in messenger.sent) == 1
    assert len([k for k in store.deliveries if k[0] == cid]) == 2 and await store.people_sent(cid) == 2
    assert await store.person_messages_sent(cid) == 1
    await campaigns.set_state(cid, "completed", "test")
    assert await runner._status_text(await campaigns.get(cid), "") == "✅ Готово: отправлено 1"


async def test_a_delivery_still_sending_is_no_message() -> None:
    from bot.campaign import MemoryCampaignStore
    from bot.campaign.runs import MemoryRunStore

    store = MemoryRunStore(MemoryCampaignStore())
    assert await store.claim_person("c", "reach:a")  # claimed, Telegram has not answered yet
    store.deliveries[("c", "fb:1")] = 7
    store.deliveries[("other", "fb:2")] = 8
    assert await store.person_messages_sent("c") == 1


@needs_db
async def test_postgres_person_messages_sent_counts_distinct_sent_messages(pool) -> None:
    from bot.campaign import PostgresCampaignStore, plan_campaign
    from bot.campaign.runs import PostgresRunStore
    from bot.orchestra.store import SafetyLimits

    store = PostgresRunStore(pool, SafetyLimits())
    plan = plan_campaign("Найди инвесторов в Мадриде", vertical="investors", location="Madrid")
    cid = await PostgresCampaignStore(pool).create(plan, chat_id=1, requested_by=1, source_text="x", actor="test")
    assert await store.person_messages_sent(cid) == 0
    for key in ("reach:a", "reach:b", "fb:1", "fb:2"):
        assert await store.claim_person(cid, key)
    await store.person_sent(cid, "reach:a", 10)  # one merged card: two keys, one message
    await store.person_sent(cid, "reach:b", 10)
    await store.person_sent(cid, "fb:1", 11)  # fb:2 is still sending
    assert await store.people_sent(cid) == 4
    assert await store.person_messages_sent(cid) == 2
