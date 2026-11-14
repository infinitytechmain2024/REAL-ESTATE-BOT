"""Q44: the final report waits for the answer to «Показать?» and the approved similar cards (``REPORT_AFTER_OFFER``),
at most ``REPORT_WAIT`` after the campaign ended. Synthetic flats, in-memory stores only."""

from __future__ import annotations

from datetime import timedelta

from bot.campaign.final_report import FinalReporter
from bot.campaign.runner import REPORT_WAIT, CampaignRunner, RunnerConfig
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_campaign_dedup import FOTOCASA, IDEALISTA
from tests.test_dedup_held_findings import OTHER, PISOS, add, setup

REPORT = "📋 Отчёт по поиску"


def test_flag_off_by_default_and_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("REPORT_AFTER_OFFER", raising=False)
    assert RunnerConfig().report_after_offer is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().report_after_offer is False
    monkeypatch.setenv("REPORT_AFTER_OFFER", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().report_after_offer is True


def one_exact_three_similar(store, cid: str) -> None:
    """One flat inside the budget (sent at once) and three different flats 15 % over it (held as similar)."""
    add(store, cid, "e1", IDEALISTA, price=240_000, area=85)
    add(store, cid, "s1", FOTOCASA, price=290_000, area=70)
    add(store, cid, "s2", PISOS, price=291_000, area=95)
    add(store, cid, "s3", OTHER, price=292_000, area=120)


def reports(messenger) -> list[str]:
    return [text for _, _, text in messenger.sent if text.startswith(REPORT)]


async def ended(**config):
    campaigns, store, messenger, runner, cid = await setup(offer_after_search=True, **config)
    one_exact_three_similar(store, cid)
    await campaigns.set_state(cid, "completed", "campaign:test")
    finished = (await campaigns.get(cid)).finished_at
    runner.now = lambda: finished + timedelta(minutes=1)
    return campaigns, store, messenger, runner, cid, finished


def fresh_runner(campaigns, store, messenger, **config) -> CampaignRunner:
    return CampaignRunner(campaigns, store, messenger, None, final_report=FinalReporter(), config=RunnerConfig(
        relevance_fail_closed=False, window_cooldown_seconds=0, offer_after_search=True, **config))


async def test_off_the_report_goes_right_after_the_question() -> None:
    _, store, messenger, runner, cid, _ = await ended()
    await runner.step(cid)
    assert len(messenger.asks) == 1 and len(reports(messenger)) == 1
    assert "Отправлено вам: 1" in reports(messenger)[0]  # the approved cards come after it: the report is stale
    store.desk.offers[(cid, "similar")].state = "approved"
    await runner.step(cid)
    assert len(messenger.findings()) == 4 and len(reports(messenger)) == 1


async def test_approved_the_report_counts_the_approved_cards() -> None:
    _, store, messenger, runner, cid, _ = await ended(report_after_offer=True)
    await runner.step(cid)
    await runner.step(cid)
    assert len(messenger.asks) == 1 and reports(messenger) == []  # asked, not answered: the report waits
    store.desk.offers[(cid, "similar")].state = "approved"
    await runner.step(cid)
    assert len(messenger.findings()) == 4
    [report] = reports(messenger)
    assert "Отправлено вам: 4" in report and "не показаны" not in report
    texts = [text for _, _, text in messenger.sent]
    assert texts.index(report) > max(texts.index(card) for card in messenger.findings())  # after the cards
    await runner.step(cid)
    assert len(reports(messenger)) == 1


async def test_approved_cards_over_several_steps_the_report_waits_for_the_last() -> None:
    _, store, messenger, runner, cid, _ = await ended(report_after_offer=True, max_stream_per_step=2)
    for _ in range(3):
        await runner.step(cid)
    assert len(messenger.asks) == 1 and reports(messenger) == []
    store.desk.offers[(cid, "similar")].state = "approved"
    await runner.step(cid)  # two of the three held cards
    assert len(messenger.findings()) == 3 and reports(messenger) == []
    await runner.step(cid)
    assert len(messenger.findings()) == 4 and len(reports(messenger)) == 1
    assert "Отправлено вам: 4" in reports(messenger)[0]


async def test_declined_the_report_goes_as_before() -> None:
    _, store, messenger, runner, cid, _ = await ended(report_after_offer=True)
    await runner.step(cid)
    assert reports(messenger) == []
    store.desk.offers[(cid, "similar")].state = "declined"
    await runner.step(cid)
    [report] = reports(messenger)
    assert len(messenger.findings()) == 1 and "Отправлено вам: 1" in report and "Похожие, не показаны: 3" in report


async def test_no_answer_the_report_goes_after_report_wait_once() -> None:
    _, store, messenger, runner, cid, finished = await ended(report_after_offer=True)
    await runner.step(cid)
    runner.now = lambda: finished + REPORT_WAIT - timedelta(seconds=1)
    await runner.step(cid)
    assert reports(messenger) == []
    runner.now = lambda: finished + REPORT_WAIT
    await runner.step(cid)
    await runner.step(cid)
    [report] = reports(messenger)
    assert "Отправлено вам: 1" in report and "Похожие, не показаны: 3" in report  # as before
    store.desk.offers[(cid, "similar")].state = "approved"  # a late answer: the cards, no second report
    await runner.step(cid)
    assert len(messenger.findings()) == 4 and len(reports(messenger)) == 1


async def test_restart_waits_from_the_store_and_reports_once() -> None:
    campaigns, store, messenger, runner, cid, finished = await ended(report_after_offer=True)
    await runner.step(cid)
    assert len(messenger.asks) == 1 and reports(messenger) == []
    runner = fresh_runner(campaigns, store, messenger, report_after_offer=True)  # a new process remembers nothing
    runner.now = lambda: finished + timedelta(minutes=2)
    await runner.step(cid)
    assert reports(messenger) == [] and len(messenger.asks) == 1  # still waits, the question is not repeated
    store.desk.offers[(cid, "similar")].state = "approved"
    await runner.step(cid)
    runner = fresh_runner(campaigns, store, messenger, report_after_offer=True)
    runner.now = lambda: finished + timedelta(minutes=3)
    await runner.step(cid)
    [report] = reports(messenger)
    assert "Отправлено вам: 4" in report and len(messenger.findings()) == 4


async def test_restart_past_report_wait_the_report_goes() -> None:
    campaigns, _, messenger, runner, cid, finished = await ended(report_after_offer=True)
    await runner.step(cid)
    assert reports(messenger) == []
    runner = fresh_runner(campaigns, runner.store, messenger, report_after_offer=True)
    runner.now = lambda: finished + REPORT_WAIT + timedelta(minutes=1)
    await runner.step(cid)
    await runner.step(cid)
    assert len(reports(messenger)) == 1
