"""Q40: the last status line of a finished search counts every delivery (STATUS_COUNTS_ALL_DELIVERIES).

Before, only object cards (``streamed_count``) counted: a sent reach contact or a lead from a post sent by link was
followed by «Пока ничего подходящего не нашёл.».
"""

from __future__ import annotations

from bot.campaign.runs import PostReadSummary
from bot.campaign.settings import CampaignRunnerSettings
from bot.campaign.status_text import NOTHING
from tests.test_empty_investor_report import _campaign, _run
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
