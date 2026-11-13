"""Q27: held similar findings are merged with the same object (``DEDUP_HELD_FINDINGS``) and the «Показать?» question
waits for the end of the search (``OFFER_AFTER_SEARCH``). Synthetic flats, in-memory stores only."""

from __future__ import annotations

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.final_report import FinalReporter, tally
from bot.campaign.offers import MemoryOffer
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.settings import CampaignRunnerSettings
from bot.campaign.tolerance import Match
from tests.test_campaign_dedup import FOTOCASA, GOAL, IDEALISTA, payload
from tests.test_near_match import ButtonMessenger

PISOS = "https://www.pisos.com/comprar/piso-valencia/3/"
OTHER = "https://www.habitaclia.com/comprar-piso-valencia-4.htm"


async def setup(**config):
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, final_report=FinalReporter(), config=RunnerConfig(
        relevance_fail_closed=False, window_cooldown_seconds=0, **config))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=-100, requested_by=42, source_text=GOAL,
                                 actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)  # the search is still running
    return campaigns, store, messenger, runner, cid


def add(store: MemoryRunStore, cid: str, fid: str, link: str, **kw) -> None:
    store.add_finding(cid, fid, f"🏠 {fid}", payload=payload(link=link, **kw), original=f"post {fid}",
                      vertical="real_estate", url=link)


def three_copies_and_another(store: MemoryRunStore, cid: str) -> None:
    """One flat 15 % over the budget on three portals (similar), and another flat (other rooms)."""
    add(store, cid, "a1", IDEALISTA, price=290_000, area=85)
    add(store, cid, "a2", FOTOCASA, price=291_000, area=86)
    add(store, cid, "a3", PISOS, price=290_000, area=85)
    add(store, cid, "b1", OTHER, price=290_000, area=85, rooms=2)


def test_flags_off_by_default_and_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("DEDUP_HELD_FINDINGS", raising=False)
    monkeypatch.delenv("OFFER_AFTER_SEARCH", raising=False)
    assert RunnerConfig().dedup_held_findings is False and RunnerConfig().offer_after_search is False
    config = CampaignRunnerSettings(_env_file=None).runner_config()
    assert config.dedup_held_findings is False and config.offer_after_search is False
    monkeypatch.setenv("DEDUP_HELD_FINDINGS", "true")
    monkeypatch.setenv("OFFER_AFTER_SEARCH", "true")
    config = CampaignRunnerSettings(_env_file=None).runner_config()
    assert config.dedup_held_findings is True and config.offer_after_search is True


async def test_off_every_copy_is_held_and_the_question_comes_at_once() -> None:
    _, store, messenger, runner, cid = await setup()
    three_copies_and_another(store, cid)
    await runner.step(cid)
    assert [store.buckets[f][0] for f in ("a1", "a2", "a3", "b1")] == ["similar"] * 4
    assert store.duplicates == {} and len(messenger.asks) == 1  # asked while the search runs
    assert tally(await store.outcome_counts(cid)).held_similar == 4  # «похожие 4» for two objects


async def test_on_three_copies_are_one_finding_and_two_repeats() -> None:
    _, store, messenger, runner, cid = await setup(dedup_held_findings=True)
    three_copies_and_another(store, cid)
    await runner.step(cid)
    assert store.duplicates == {"a2": "a1", "a3": "a1"}
    assert [f.id for f in await store.held_findings(cid, "similar", 10)] == ["a1", "b1"]
    assert store.cluster_links["a1"] == [{"url": FOTOCASA, "site": "fotocasa.es"},
                                         {"url": PISOS, "site": "pisos.com"}]
    counts = tally(await store.outcome_counts(cid))
    assert (counts.held_similar, counts.duplicates) == (2, 2)  # the report shows the copies as repeats
    assert messenger.findings() == []

    store.desk.offers[(cid, "similar")] = MemoryOffer(42, "approved")
    await runner.step(cid)
    sent = messenger.findings()
    assert len(sent) == 2  # one card per object
    head = next(card for card in sent if "Также на" in card)
    assert FOTOCASA in head and PISOS in head
    # A fourth copy coming after the approval is attached to the sent card, not sent again.
    add(store, cid, "a4", "https://www.milanuncios.com/pisos/5.htm", price=290_000, area=85)
    await runner.step(cid)
    assert len(messenger.findings()) == 2 and store.duplicates["a4"] == "a1"


async def test_on_a_copy_of_a_sent_card_is_attached_not_held() -> None:
    _, store, messenger, runner, cid = await setup(dedup_held_findings=True)
    add(store, cid, "e1", IDEALISTA, price=290_000, area=85)
    await runner.step(cid)
    store.desk.offers[(cid, "similar")].state = "approved"
    await runner.step(cid)
    assert len(messenger.findings()) == 1  # e1 sent after approval: a cluster head
    add(store, cid, "e2", FOTOCASA, price=291_000, area=86)  # the same flat, similar too
    await runner.step(cid)
    assert store.duplicates == {"e2": "e1"} and await store.held_findings(cid, "similar", 10) == []
    assert len(messenger.findings()) == 1 and "Также на: Fotocasa" in messenger.edits[-1][2]


async def test_question_once_at_the_end_of_the_search_before_the_report() -> None:
    campaigns, store, messenger, runner, cid = await setup(dedup_held_findings=True, offer_after_search=True)
    three_copies_and_another(store, cid)
    for _ in range(3):
        await runner.step(cid)
    assert messenger.asks == [] and messenger.findings() == []  # the search still runs: no question yet
    await campaigns.set_state(cid, "completed", "campaign:test")
    for _ in range(3):
        await runner.step(cid)
    assert len(messenger.asks) == 1
    texts = [text for _, _, text in messenger.sent]
    report = next(i for i, text in enumerate(texts) if text.startswith("📋 Отчёт по поиску"))
    assert texts.index(messenger.asks[0][1]) < report  # no exact ones: the question comes before the report
    store.desk.offers[(cid, "similar")].state = "approved"
    await runner.step(cid)
    assert len(messenger.findings()) == 2 and len(messenger.asks) == 1  # one card per object


async def test_question_waits_until_every_finding_was_checked() -> None:
    campaigns, store, messenger, runner, cid = await setup(offer_after_search=True, max_stream_per_step=2)
    three_copies_and_another(store, cid)
    await campaigns.set_state(cid, "completed", "campaign:test")
    await runner.step(cid)  # two of four checked: not yet
    assert messenger.asks == []
    await runner.step(cid)  # the last two: the full batch, one more step tells nothing is left
    await runner.step(cid)
    assert len(messenger.asks) == 1
    await runner.step(cid)
    assert len(messenger.asks) == 1


async def test_a_rejected_copy_of_a_sent_card_keeps_its_reason() -> None:
    """Only held buckets (similar/other) are merged: a rejected copy stays rejected and adds no «Также на» link."""
    _, store, messenger, runner, cid = await setup(dedup_held_findings=True)
    add(store, cid, "e1", IDEALISTA, price=240_000, area=85)
    await runner.step(cid)
    assert len(messenger.findings()) == 1
    judge = runner._judge

    async def reject_x2(campaign, request, finding, *, final=False):
        if finding.id == "x2":
            return Match("excluded", 0.0, "type")
        return await judge(campaign, request, finding, final=final)

    runner._judge = reject_x2  # type: ignore[method-assign]
    add(store, cid, "x2", FOTOCASA, price=240_000, area=85)
    await runner.step(cid)
    assert store.buckets["x2"][0] == "excluded" and store.duplicates == {} and "e1" not in store.cluster_links
    assert messenger.edits == []
    counts = tally(await store.outcome_counts(cid))
    assert counts.excluded == {"type": 1} and counts.duplicates == 0


def _report_after_question(messenger: ButtonMessenger) -> bool:
    texts = [text for _, _, text in messenger.sent]
    report = [i for i, text in enumerate(texts) if text.startswith("📋 Отчёт по поиску")]
    return len(report) == 1 and len(messenger.asks) == 1 and texts.index(messenger.asks[0][1]) < report[0]


async def test_a_full_batch_the_report_waits_for_the_question() -> None:
    campaigns, store, messenger, runner, cid = await setup(offer_after_search=True, max_stream_per_step=2)
    three_copies_and_another(store, cid)
    await campaigns.set_state(cid, "completed", "campaign:test")
    reports = []
    for _ in range(4):
        await runner.step(cid)
        reports.append(sum(text.startswith("📋 Отчёт по поиску") for _, _, text in messenger.sent))
    assert reports == [0, 0, 1, 1]  # two full batches, then the question and the report in one step
    assert _report_after_question(messenger)


async def test_a_failed_card_send_holds_the_report_back() -> None:
    """The early ``return deferred`` (an exact card not sent): nothing is settled, so neither question nor report."""
    campaigns, store, messenger, runner, cid = await setup(offer_after_search=True)
    add(store, cid, "e1", IDEALISTA, price=240_000, area=85)
    add(store, cid, "b1", OTHER, price=290_000, area=85, rooms=2)
    await campaigns.set_state(cid, "completed", "campaign:test")
    messenger.fail = 1
    await runner.step(cid)
    assert messenger.asks == [] and not any(t.startswith("📋 Отчёт по поиску") for _, _, t in messenger.sent)
    await runner.step(cid)
    await runner.step(cid)
    assert len(messenger.findings()) == 1 and _report_after_question(messenger)


async def test_a_failed_question_holds_the_report_back() -> None:
    campaigns, store, messenger, runner, cid = await setup(offer_after_search=True)
    add(store, cid, "b1", OTHER, price=290_000, area=85)
    await campaigns.set_state(cid, "completed", "campaign:test")
    messenger.ask_fail = 1
    await runner.step(cid)
    assert messenger.asks == [] and not any(t.startswith("📋 Отчёт по поиску") for _, _, t in messenger.sent)
    await runner.step(cid)
    assert _report_after_question(messenger)


async def test_off_the_report_does_not_wait() -> None:
    campaigns, store, messenger, runner, cid = await setup(max_stream_per_step=2)
    three_copies_and_another(store, cid)
    await campaigns.set_state(cid, "completed", "campaign:test")
    await runner.step(cid)
    assert any(t.startswith("📋 Отчёт по поиску") for _, _, t in messenger.sent)
