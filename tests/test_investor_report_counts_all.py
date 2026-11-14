"""Q60 (INVESTOR_REPORT_COUNTS_ALL): the investor report's «Отправлено вам» counts the finding cards too.

Before (sim b5-g7-s6), only ``campaign_lead_deliveries`` counted: 3 finding cards and 1 reach contact were
«людей 0, доставок из поиска 1». In-memory stores and fakes only."""

from __future__ import annotations

from bot.campaign.final_report import LeadsSummary, investors_section
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_empty_investor_report import _campaign, _reports, _run


async def _search(*, cards: int, reach: bool, **config) -> list[str]:
    campaigns, store, messenger, clock, web, runner, cid = await _campaign(investor_report=True, **config)
    for n in range(cards):
        url = f"https://club{n}.example.org/about"
        store.add_finding(cid, f"f{n}", f"📈 {n}", payload={"who": f"Club {n}", "summary": "Investor club"},
                          original=f"page {n}", vertical="investors", url=url)

    def deliver() -> None:
        if reach:
            store.deliveries[(cid, "reach:linkedin.com/in/someone")] = 99

    await _run(campaigns, runner, clock, web, cid, deliver=deliver)
    assert (await campaigns.get(cid)).state == "completed"
    return _reports(messenger)


async def test_on_the_report_counts_finding_cards_people_and_reach() -> None:
    reports = await _search(cards=3, reach=True, investor_report_counts_all=True)
    assert len(reports) == 1
    assert "Отправлено вам: всего 4 — находок 3, людей 0, доставок из поиска 1" in reports[0]


async def test_off_the_report_counts_people_and_reach_only_as_before() -> None:
    reports = await _search(cards=3, reach=True)
    assert len(reports) == 1 and "Отправлено вам: людей 0, доставок из поиска 1" in reports[0]


async def test_on_cards_alone_are_no_empty_report() -> None:
    on = await _search(cards=2, reach=False, investor_report_counts_all=True, empty_report=True)
    assert len(on) == 1 and "Ничего не нашёл" not in on[0] and "всего 2 — находок 2" in on[0]
    off = await _search(cards=2, reach=False, empty_report=True)
    assert len(off) == 1 and "Ничего не нашёл" in off[0], "before: the cards were not seen by the report"


def test_the_section_line() -> None:
    assert investors_section(LeadsSummary(people_sent=2, reach_sent=1))[-1] == (
        "Отправлено вам: людей 2, доставок из поиска 1")
    assert investors_section(LeadsSummary(people_sent=2, reach_sent=1, cards_sent=0))[-1] == (
        "Отправлено вам: всего 3 — находок 0, людей 2, доставок из поиска 1")
    assert not LeadsSummary(cards_sent=1).empty and LeadsSummary(cards_sent=0).empty


def test_the_flag_is_off_by_default_and_reaches_the_runner(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("INVESTOR_REPORT_COUNTS_ALL", raising=False)
    assert CampaignRunnerSettings(_env_file=None).runner_config().investor_report_counts_all is False
    monkeypatch.setenv("INVESTOR_REPORT_COUNTS_ALL", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().investor_report_counts_all is True
