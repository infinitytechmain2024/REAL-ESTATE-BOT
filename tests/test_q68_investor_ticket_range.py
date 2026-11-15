"""Q68: an investor's ticket range is shown and compared by its upper bound (INVESTOR_TICKET_RANGE, off by default).

Sim b6-g7-s7 (co-investors in Málaga, ticket from 50 000 €): Angel Investment Network profiles «€100 - €150,000» and
«$10,000 - $100,000» went out as «Сумма: 100 €» and «Сумма: 10 000 $» (the lower bound). Mocks only, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from bot.analysis_pipeline.cards import render_card
from bot.campaign.people import score
from bot.campaign.reach import Contact
from bot.campaign.tolerance import classify, request_for
from bot.utils.ticket_range import investor_ticket_range_enabled, ticket_range_of, ticket_ranges
from tests.test_reviewer import settings

NOW = datetime(2026, 10, 1, tzinfo=UTC)
FEDERICO = {"who": "Federico D.", "location": "Marbella, Málaga", "country": "ES", "listing_kind": "offer",
            "price_amount": 100.0, "price_currency": "EUR", "price_signals": ["€100 - €150,000"],
            "summary_ru": "Профиль бизнес-ангела Federico D.", "evidence": {"price": "Nivel de Inversión €100 - €150,000"}}
JOSE = {**FEDERICO, "who": "Jose M.", "price_amount": 10000.0, "price_currency": "USD",
        "price_signals": ["Nivel de Inversión $10,000 - $100,000"]}
FRANCISCO = {**FEDERICO, "who": "Francisco C.", "price_amount": 10000.0, "price_signals": ["€10,000 - €100,000"]}
SPEC = {"investor": {"who": ["private"], "ticket": {"min": 50000.0, "max": None, "currency": "EUR"},
                     "user_role": "raising"}}


def _flag(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("INVESTOR_TICKET_RANGE", "true" if on else "false")


def _sum_line(card: str) -> str | None:
    return next((line for line in card.splitlines() if line.startswith("Сумма:")), None)


@pytest.mark.parametrize(("text", "low", "high", "currency", "shown"), [
    ("Nivel de Inversión $10,000 - $100,000", 10_000, 100_000, "USD", "10 000 – 100 000 $"),
    ("€10.000 – €100.000", 10_000, 100_000, "EUR", "10 000 – 100 000 €"),
    ("de 10 000 a 100 000 €", 10_000, 100_000, "EUR", "10 000 – 100 000 €"),
    ("€1M - €5M", 1_000_000, 5_000_000, "EUR", "1 000 000 – 5 000 000 €"),
    ("$500 - $5,000", 500, 5_000, "USD", "500 – 5 000 $"),  # 500 000 would pass 5 000: not ambiguous
    ("$70,000 - $116,000,000", 70_000, 116_000_000, "USD", "70 000 – 116 000 000 $"),
    # a bare lower bound that may be thousands: not guessed, shown as written
    ("Nivel de Inversión €100 - €150,000", None, 150_000, "EUR", "€100 - €150,000"),
    ("€10-50k", None, 50_000, "EUR", "€10-50k"),
])
def test_ranges_are_read_without_guessing(text, low, high, currency, shown) -> None:
    [found] = ticket_ranges(text)
    assert (found.low, found.high, found.currency, found.display()) == (low, high, currency, shown)


@pytest.mark.parametrize("text", ["100 - 200", "€100 - $150,000", "€150,000 - €100", "1,5M - 3M €",
                                  "2 - 3 habitaciones, 100 €", "Inversión mínima: 500 €"])
def test_no_range_without_one_currency_and_a_rising_upper_bound(text) -> None:
    assert ticket_ranges(text) == []


def test_card_shows_the_range_with_the_flag_and_the_lower_bound_without(monkeypatch) -> None:
    _flag(monkeypatch, False)
    assert _sum_line(render_card(FEDERICO, vertical="investors")) == "Сумма: 100 €"
    assert _sum_line(render_card(JOSE, vertical="investors")) == "Сумма: 10 000 $"
    _flag(monkeypatch, True)
    assert _sum_line(render_card(FEDERICO, vertical="investors")) == "Сумма: €100 - €150,000"
    assert _sum_line(render_card(JOSE, vertical="investors")) == "Сумма: 10 000 – 100 000 $"
    single = {**FEDERICO, "price_amount": 50000.0, "price_signals": ["50 000 €"]}
    assert _sum_line(render_card(single, vertical="investors")) == "Сумма: 50 000 €"  # no range: as before
    listing = {**JOSE, "deal_type": "sale", "property_type": "apartment"}
    assert "Цена: 10 000 $" in render_card(listing, vertical="real_estate")  # a listing's price is untouched


def _request(monkeypatch, on: bool, spec=SPEC):
    _flag(monkeypatch, on)
    return request_for({}, location="Malaga", vertical="investors", country="ES", spec=spec)


def test_the_upper_bound_meets_the_tasks_minimum_ticket(monkeypatch) -> None:
    request = _request(monkeypatch, True)
    assert (request.ticket_min, request.ticket_currency) == (50000.0, "EUR")
    assert classify(FEDERICO, request, vertical="investors").bucket == "exact"  # 150 000 >= 50 000
    assert classify(FRANCISCO, request, vertical="investors").bucket == "exact"  # 100 000 >= 50 000
    small = {**FRANCISCO, "price_signals": ["€5,000 - €20,000"]}
    match = classify(small, request, vertical="investors")
    assert (match.bucket, match.why, match.note) == ("excluded", "price", "Не подходит: тикет до 20 000 €")
    # another currency or no range: not compared
    assert classify({**small, "price_signals": ["$5,000 - $20,000"]}, request, vertical="investors").bucket == "exact"
    assert classify({**small, "price_signals": ["20 000 €"]}, request, vertical="investors").bucket == "exact"
    # the task names no minimum: nothing to compare
    no_min = _request(monkeypatch, True, {"investor": {"ticket": {"min": None, "max": 80000, "currency": "EUR"}}})
    assert classify(small, no_min, vertical="investors").bucket == "exact"


def test_flag_off_investors_are_exact_as_before(monkeypatch) -> None:
    request = _request(monkeypatch, False)
    assert request.ticket_min is None
    small = {**FRANCISCO, "price_signals": ["€5,000 - €20,000"]}
    assert classify(small, request, vertical="investors").bucket == "exact"


def test_flag_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("INVESTOR_TICKET_RANGE", raising=False)
    assert investor_ticket_range_enabled() is False
    assert settings().investor_ticket_range is False
    assert settings(INVESTOR_TICKET_RANGE="true").investor_ticket_range is True
    assert ticket_range_of({"price_signals": ["x", "€10,000 - €100,000"]}).high == 100_000


def test_lead_score_counts_a_range_that_overlaps_the_ticket(monkeypatch) -> None:
    angel = Contact("0" * 64, "https://example.com/p1", "web", "investor", "Jose M.",
                    profile_text="Business angel. Nivel de Inversión €10,000 - €100,000")
    spec = {"who": ["private"], "ticket": {"min": 50000, "max": 80000}}
    _flag(monkeypatch, False)
    assert "тикет в вашем диапазоне" not in score(angel, spec, now=NOW)[1]  # neither bound within 50 000–80 000
    _flag(monkeypatch, True)
    assert "тикет в вашем диапазоне" in score(angel, spec, now=NOW)[1]
    unsure = Contact("1" * 64, "https://example.com/p2", "web", "investor", "Federico D.",
                     profile_text="Nivel de Inversión €100 - €150,000")
    assert "тикет в вашем диапазоне" not in score(unsure, {"ticket": {"max": 80000}}, now=NOW)[1]  # lower bound unknown
    assert "тикет в вашем диапазоне" in score(unsure, {"ticket": {"min": 50000}}, now=NOW)[1]
