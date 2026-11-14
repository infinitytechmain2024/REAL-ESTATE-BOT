"""Q59: a seasonal, temporary or holiday let is excluded for a home rent that asked for no short term
(RENT_SEASONAL_EXCLUDED, off by default). Sim b5-g5-s6: «снять piso в Барселоне до 1500», «от 1 спальни», term «от 6
месяцев», «только точные»: 12 seasonal lets were offered as similar («другой вид сделки: сезонная аренда. Показать?»)
and approved. Mocks only, no network.
"""

from __future__ import annotations

import pytest

from bot.campaign import final_report, metrics
from bot.campaign.relevance import reason_category, review_match
from bot.campaign.tolerance import (
    SEASONAL_NOTE,
    Request,
    classify,
    request_for,
    seasonal_excluded_enabled,
)
from bot.utils.listing_text import task_asks_short
from tests.test_portal_rental_term import SOURCE_TEXT, TERM_QUESTION
from tests.test_q49_seasonal_rent import FLAT, SIM_SEASONAL
from tests.test_reviewer import settings


def _spec(answer: str | None) -> dict | None:
    return None if answer is None else {"context": {"answers": [{"question": TERM_QUESTION, "answer": answer}]}}


def barcelona(monkeypatch, flag: bool, answer: str | None = "от 6 месяцев", *, deal: str = "rent",
              kind: str | None = "apartment", q49: bool = True) -> Request:
    monkeypatch.setenv("RENT_SEASONAL_EXCLUDED", "true" if flag else "false")
    monkeypatch.setenv("RENT_DEFAULT_LONG_TERM", "true" if q49 else "false")
    constraints = {"deal": deal, "max_price": 1500 if deal == "rent" else 300_000, "property_type": kind, "rooms": 1}
    return request_for(constraints, location="Barcelona", vertical="real_estate", text=SOURCE_TEXT, country="ES",
                       spec=_spec(answer))


@pytest.mark.parametrize(("answer", "short"), [
    # open terms, long terms, none: no short let
    ("от 6 месяцев", False), ("от полугода", False), ("минимум 8 месяцев", False), ("8 месяцев минимум", False),
    ("не менее 3 месяцев", False), ("на год", False), ("на 12 месяцев", False), ("не знаю", False),
    ("долгосрочно", False),
    # a fixed term under a year is an «alquiler de temporada»; a short term beside a long one allows it too
    ("на 6 месяцев", True), ("на 10 месяцев", True), ("на полгода", True), ("на 3 месяца", True), ("на сезон", True),
    ("посуточно", True), ("на лето", True), ("на 6 месяцев или на лето", True), ("на год или на лето", True),
    # «temporada» in the person's words is read as a short sign, «larga temporada» too: the safe side, not excluded
    ("alquiler de larga temporada", True),
])
def test_task_asks_short(answer, short):
    assert task_asks_short(_spec(answer), SOURCE_TEXT) is short


def test_the_sim_seasonal_lets_are_excluded(monkeypatch):
    request = barcelona(monkeypatch, True)
    assert request.seasonal_excluded is True
    for term, summary in SIM_SEASONAL:
        match = classify({**FLAT, "rooms": 2, "rental_term": term, "summary": summary}, request)
        assert (match.bucket, match.why, match.note) == ("excluded", "seasonal", SEASONAL_NOTE), summary


@pytest.mark.parametrize("q49", [True, False])
def test_excluded_without_q49_too(monkeypatch, q49):
    request = barcelona(monkeypatch, True, q49=q49)
    found = {**FLAT, "rooms": 1, "summary": "Piso en alquiler de temporada, uso vacacional."}
    assert classify(found, request).bucket == "excluded"


@pytest.mark.parametrize("answer", ["от 6 месяцев", "минимум 8 месяцев", "не знаю", None, "на год", "долгосрочно"])
def test_no_short_term_excludes(monkeypatch, answer):
    request = barcelona(monkeypatch, True, answer)
    assert classify({**FLAT, "rental_term": "temporary"}, request).bucket == "excluded"


@pytest.mark.parametrize("answer", ["на сезон", "на 1–3 месяца", "посуточно", "на 6 месяцев", "на 10 месяцев",
                                    "на полгода", "на год или на лето", "larga temporada"])
def test_a_short_term_asked_for_keeps_the_old_bucket(monkeypatch, answer):
    request = barcelona(monkeypatch, True, answer)
    assert request.seasonal_excluded is False
    found = {**FLAT, "rental_term": "temporary", "summary": "alquiler de temporada"}
    before = classify(found, barcelona(monkeypatch, False, answer))  # Q49 as before: exact or similar, never excluded
    assert classify(found, barcelona(monkeypatch, True, answer)) == before and before.bucket != "excluded"


def test_long_lets_stay_exact(monkeypatch):
    request = barcelona(monkeypatch, True)
    assert classify({**FLAT, "rental_term": "long", "summary": "Alquiler de larga temporada"}, request).bucket == "exact"
    assert classify({**FLAT, "summary": "Piso en alquiler de larga temporada"}, request).bucket == "exact"
    assert classify(FLAT, request).bucket == "exact"


def test_flag_off_as_before(monkeypatch):
    request = barcelona(monkeypatch, False)
    assert request.seasonal_excluded is False
    match = classify({**FLAT, "rental_term": "temporary"}, request)
    assert (match.bucket, match.why, match.note) == ("similar", "deal_kind", SEASONAL_NOTE)  # Q49 as before
    assert classify({**FLAT, "rental_term": "temporary"}, barcelona(monkeypatch, False, q49=False)).bucket == "exact"


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("RENT_SEASONAL_EXCLUDED", raising=False)
    assert seasonal_excluded_enabled() is False and Request().seasonal_excluded is False
    assert settings().rent_seasonal_excluded is False
    assert settings(RENT_SEASONAL_EXCLUDED="true").rent_seasonal_excluded is True


def test_purchase_and_non_homes_are_untouched(monkeypatch):
    sale = barcelona(monkeypatch, True, deal="sale")
    assert sale.seasonal_excluded is False
    found = {**FLAT, "deal_type": "sale", "price_amount": 250_000, "summary": "alquiler de temporada"}
    assert classify(found, sale).bucket == "exact"
    for kind in ("commercial", "land"):
        assert barcelona(monkeypatch, True, kind=kind).seasonal_excluded is False
    assert barcelona(monkeypatch, True, kind=None).seasonal_excluded is True


def test_reviewer_match_cannot_lift_it_and_the_report_names_it(monkeypatch):
    request = barcelona(monkeypatch, True)
    rules = classify({**FLAT, "rental_term": "temporary"}, request)
    review = {"overall": "match", "criteria": [{"name": "deal", "verdict": "pass"}]}
    assert review_match(rules, review).bucket == "excluded"
    assert reason_category(rules.why) == "seasonal"
    assert final_report.REASONS["seasonal"] == metrics.REASONS["seasonal"] == "сезонная аренда"
