"""Q25 (``OFFER_ALL_REASONS``): one «Одобрить» sends every held similar listing, so the «Показать?» question names
every distinct difference, not only the closest one's (synthetic plots and phrases; mocks only)."""

from __future__ import annotations

from bot.campaign import offers
from bot.campaign.relevance import Relevance
from bot.campaign.runner import RunnerConfig
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_campaign_relevance import FakeJudge, add, plot, setup

PHRASES = ("лот из нескольких участков", "участок занят", "промышленная зона", "коммерческий участок",
           "лот из нескольких участков", "дальше от метро", "без подъезда")


def test_reasons_question_lists_distinct_reasons_up_to_five() -> None:
    assert offers.reasons_question(["чуть дороже", "участок занят", "чуть дороже"], land=True, exact_found=False) == (
        "По вашим критериям пока ничего не нашёл, но есть участки с отличиями: чуть дороже; участок занят. Показать?")
    many = [f"отличие {i}" for i in range(7)]
    assert offers.reasons_question(many, exact_found=True) == (
        "Есть ещё похожие варианты с отличиями: отличие 0; отличие 1; отличие 2; отличие 3; отличие 4 и ещё 2. "
        "Показать?")


def test_reasons_of_each_deviation_are_short() -> None:
    assert offers.reasons_of(offers.Deviation("price", "~60 000 €", "~50 000 €")) == ("чуть дороже",)
    assert offers.reasons_of(offers.Deviation("area", "~1 600 м²", "от 2 000 м²")) == ("меньшей площади",)
    assert offers.reasons_of(offers.Deviation("area_unknown", phrase="площадь не указана")) == ("площадь не указана",)
    assert offers.reasons_of(offers.Deviation("unverified", unknown=("площадь", "место"))) == (
        "не подтверждено: площадь", "не подтверждено: место")
    assert offers.reasons_of(offers.Deviation("deal_kind", phrase="аукцион")) == ("аукцион",)
    assert offers.reasons_of(offers.Deviation("other", phrase="участок занят")) == ("участок занят",)
    assert offers.reasons_of(offers.Deviation("other")) == ()


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
    assert on.startswith("По вашим критериям пока ничего не нашёл, но есть участки с отличиями: ")
    listed = on.removeprefix("По вашим критериям пока ничего не нашёл, но есть участки с отличиями: ")
    listed = listed.removesuffix(" и ещё 1. Показать?")
    names = listed.split("; ")
    assert len(names) == 5 and len(set(names)) == 5 and set(names) < set(PHRASES)
    assert names[0] == "лот из нескольких участков"  # the closest one's first


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
