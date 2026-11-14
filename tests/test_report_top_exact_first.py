"""Q58 (``REPORT_TOP_EXACT_FIRST``): «Лучшие варианты» of the final report list the cards sent as exact before the
similar ones sent after «Одобрить» (shares, lots the reviewer held, which the rules alone call exact: the bench run
b5-g4-s6 had them on top, being the cheapest); within each group the previous order. Mocks only."""

from __future__ import annotations

from bot.campaign.final_report import FinalReporter, rank_cards
from bot.campaign.runs import SentFinding, StreamFinding
from bot.campaign.settings import CampaignRunnerSettings
from bot.campaign.tolerance import Request


def card(fid: str, price: int, number: int, bucket: str) -> SentFinding:
    payload = {"price_amount": price, "price_currency": "EUR", "deal_type": "sale", "property_type": "land",
               "area_m2": 20_000, "plot_m2": 20_000, "location": "Aspe, Alicante",
               "original_post_link": f"https://www.idealista.com/inmueble/{number}/"}
    return SentFinding(StreamFinding(fid, "t", payload=payload, vertical="real_estate"), None, number, bucket=bucket)


REQUEST = Request(amount=300_000, deal="sale", location="Alicante", min_area=5000)
SENT = [card("exact_dear", 90_000, 1, "exact"), card("exact_cheap", 36_000, 2, "exact"),
        card("share", 4_600, 3, "similar"), card("lot", 15_600, 4, "similar"), card("exact_mid", 49_000, 5, "exact"),
        card("other", 18_000, 6, "other")]


def ids(ranked) -> list[str]:
    return [r.card.finding.id for r in ranked]


def test_without_the_flag_the_order_is_as_before() -> None:
    assert ids(rank_cards(SENT, REQUEST)) == ["share", "lot", "other", "exact_cheap", "exact_mid", "exact_dear"]
    assert ids(rank_cards(SENT, REQUEST, exact_first=False)) == ids(rank_cards(SENT, REQUEST))


def test_exact_cards_come_first_with_the_flag() -> None:
    ranked = ids(rank_cards(SENT, REQUEST, exact_first=True))
    assert ranked == ["exact_cheap", "exact_mid", "exact_dear", "share", "lot", "other"]  # each group cheaper first
    assert ids(rank_cards(SENT, REQUEST, limit=2, exact_first=True)) == ["exact_cheap", "exact_mid"]
    exact_only = [c for c in SENT if c.bucket == "exact"]
    assert ids(rank_cards(exact_only, REQUEST, exact_first=True)) == ids(rank_cards(exact_only, REQUEST))


async def test_the_report_lists_exact_first_only_with_the_flag() -> None:
    async def best(reporter: FinalReporter) -> list[str]:
        text = await reporter.build("участок", REQUEST, [], SENT, [], [])
        lines = text.split("Лучшие варианты", 1)[1].split("\n\n", 1)[0].splitlines()[1:]
        return [line.split("карточка №")[1] for line in lines if "карточка №" in line]

    assert await best(FinalReporter()) == ["3", "4", "6", "2", "5", "1"]
    assert await best(FinalReporter(exact_first=True)) == ["2", "5", "1", "3", "4", "6"]


def test_settings_pass_the_flag(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.setenv("TELEGRAM_TOKEN", "test-token")
    monkeypatch.setenv("CAMPAIGN_FINAL_REPORT", "true")
    monkeypatch.delenv("REPORT_TOP_EXACT_FIRST", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert FinalReporter().exact_first is False
    assert CampaignRunnerSettings(_env_file=None).final_reporter().exact_first is False
    monkeypatch.setenv("REPORT_TOP_EXACT_FIRST", "true")
    assert CampaignRunnerSettings(_env_file=None).final_reporter().exact_first is True
