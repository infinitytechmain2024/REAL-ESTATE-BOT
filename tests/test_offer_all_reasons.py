"""Q25 (``OFFER_ALL_REASONS``): one «Одобрить» sends every held similar listing, so the «Показать?» question names
every distinct difference, not only the closest one's (synthetic plots and phrases; mocks only)."""

from __future__ import annotations

from bot.campaign import MemoryCampaignStore, offers, plan_campaign
from bot.campaign.offers import Deviation
from bot.campaign.relevance import Relevance
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_campaign_relevance import CHAT, USER, FakeJudge, add, plot, setup
from tests.test_near_match import ButtonMessenger

PHRASES = ("лот из нескольких участков", "участок занят", "промышленная зона", "коммерческий участок",
           "лот из нескольких участков", "дальше от метро", "без подъезда")
LEAD = "По вашим критериям пока ничего не нашёл, но есть участки с отличиями — "


def test_reasons_question_lists_distinct_reasons_up_to_five() -> None:
    assert offers.reasons_question(["участок занят", "дороже (до ~60 000 €)", "участок занят"], land=True,
                                   exact_found=False) == f"{LEAD}участок занят; дороже (до ~60 000 €). Показать?"
    many = [f"отличие {i}" for i in range(7)]
    assert offers.reasons_question(many, exact_found=True) == (
        "Есть ещё похожие варианты с отличиями — отличие 0; отличие 1; отличие 2; отличие 3; отличие 4 и ещё 2. "
        "Показать?")


def test_reasons_substantial_first_worst_value_one_unconfirmed_reason() -> None:
    deviations = [
        Deviation("unverified", unknown=("место",)),
        Deviation("price", "~60 000 €", "~50 000 €", value=60_000),
        Deviation("area", "~1 700 м²", "от 2 000 м²", value=1700),
        Deviation("area_unknown", phrase="площадь не указана"),
        Deviation("price", "~75 000 €", "~50 000 €", value=75_000),
        Deviation("deal_kind", phrase="аукцион"),
        Deviation("area", "~1 600 м²", "от 2 000 м²", value=1600),
        Deviation("unverified", unknown=("район", "место")),
        Deviation("other", phrase="участок занят"),
        Deviation("other"),
    ]
    assert offers.reasons_of(deviations) == [
        "участок занят", "дороже (до ~75 000 €)", "площадь меньше (от ~1 600 м²)", "другой вид сделки: аукцион",
        "не подтверждено: место, площадь, район"]
    assert offers.reasons_of([Deviation("unverified"), Deviation("other", phrase="участок занят")]) == [
        "участок занят", "не удалось проверить автоматически"]
    assert offers.reasons_of([Deviation("other")]) == []


async def _question(flag: bool) -> list[str]:
    judge = FakeJudge(*(Relevance("near", "Отличие.", phrase) for phrase in PHRASES))
    _, store, messenger, runner, cid = await setup(judge, offer_all_reasons=flag)
    for i in range(len(PHRASES)):
        add(store, cid, f"p{i}", plot(2500))
    await runner.tick()
    assert all(store.buckets[f"p{i}"][0] == "similar" for i in range(len(PHRASES)))
    assert messenger.findings() == []
    return [text for _, text, _ in messenger.asks]


async def test_the_question_names_every_difference_only_with_the_flag() -> None:
    (off,) = await _question(False)
    assert off == "По вашим критериям пока ничего не нашёл, но есть участки лот из нескольких участков. Показать?"
    (on,) = await _question(True)
    assert on.startswith(LEAD) and on.endswith(" и ещё 1. Показать?")
    names = on.removeprefix(LEAD).removesuffix(" и ещё 1. Показать?").split("; ")
    assert len(names) == 5 and len(set(names)) == 5 and set(names) < set(PHRASES)
    assert names[0] == "лот из нескольких участков"  # the closest one's first


async def _mixed(**config) -> tuple[MemoryRunStore, ButtonMessenger, CampaignRunner, str]:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, config=RunnerConfig(
        window_cooldown_seconds=0, relevance_fail_closed=False, **config))
    goal = "Купить участок в Мадриде от 2000 м² до 50 000 €"
    cid = await campaigns.create(plan_campaign(goal, vertical="real_estate"), chat_id=CHAT, requested_by=USER,
                                 source_text=goal, actor="t")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    for fid, payload in (("dear", plot(2500, price=58_000)), ("dearer", plot(2500, price=60_000)),
                         ("small", plot(1700, price=45_000)), ("smaller", plot(1600, price=45_000)),
                         ("bare", plot(None, price=45_000))):
        add(store, cid, fid, payload)
    return store, messenger, runner, cid


async def test_price_area_and_unconfirmed_through_the_runner() -> None:
    store, messenger, runner, _ = await _mixed(offer_all_reasons=True)
    await runner.tick()
    assert {store.buckets[f][0] for f in ("dear", "dearer", "small", "smaller", "bare")} == {"similar"}
    (text,) = [text for _, text, _ in messenger.asks]
    assert text.startswith(LEAD) and text.endswith(". Показать?")
    names = text.removeprefix(LEAD).removesuffix(". Показать?").split("; ")
    assert names == ["дороже (до ~60 000 €)", "площадь меньше (от ~1 600 м²)", "не подтверждено: площадь"]
    assert text.count(":") == 1  # no double colon


async def test_one_reason_with_a_worse_value_names_the_worst() -> None:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, config=RunnerConfig(
        window_cooldown_seconds=0, relevance_fail_closed=False, offer_all_reasons=True))
    goal = "Купить участок в Мадриде до 50 000 €"
    cid = await campaigns.create(plan_campaign(goal, vertical="real_estate"), chat_id=CHAT, requested_by=USER,
                                 source_text=goal, actor="t")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    add(store, cid, "p57", plot(None, price=57_000))
    add(store, cid, "p60", plot(None, price=60_000))
    await runner.tick()
    assert store.buckets["p57"][0] == store.buckets["p60"][0] == "similar"
    # the closest alone would say «~57 000 €»: the worst price of the batch is named
    assert [text for _, text, _ in messenger.asks] == [f"{LEAD}дороже (до ~60 000 €). Показать?"]


async def test_a_failure_while_collecting_reasons_keeps_the_old_question() -> None:
    store, messenger, runner, cid = await _mixed(offer_all_reasons=True)
    _, old_messenger, old_runner, _ = await _mixed()
    await old_runner.tick()

    async def broken(*_args, **_kw):
        raise RuntimeError("store down")

    runner._reasons = broken  # type: ignore[method-assign]
    await runner.tick()
    assert [t for _, t, _ in messenger.asks] == [t for _, t, _ in old_messenger.asks] and len(messenger.asks) == 1
    assert await store.offer_state(cid, "similar") == "asked"  # sent, not hanging unasked


async def test_one_difference_keeps_the_old_question() -> None:
    judge = FakeJudge(Relevance("near", "Отличие.", "дальше от метро"))
    _, store, messenger, runner, cid = await setup(judge, offer_all_reasons=True)
    add(store, cid, "a", plot(2500))
    add(store, cid, "b", plot(2500))
    await runner.tick()
    assert [text for _, text, _ in messenger.asks] == [
        "По вашим критериям пока ничего не нашёл, но есть участки дальше от метро. Показать?"]


def test_settings_pass_the_flag(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.delenv("OFFER_ALL_REASONS", raising=False)
    assert RunnerConfig().offer_all_reasons is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().offer_all_reasons is False
    monkeypatch.setenv("OFFER_ALL_REASONS", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().offer_all_reasons is True
