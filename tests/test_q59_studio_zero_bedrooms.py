"""Q59: a studio has 0 bedrooms against the task's rooms (STUDIO_ZERO_BEDROOMS, off by default). Sim b5-g5-s6:
«от 1 спальни», card 9 «Estudio a estrenar … Dispone de una habitación» (type studio, rooms 1) went out as exact.
Mocks only, no network.
"""

from __future__ import annotations

import pytest

from bot.campaign.tolerance import Request, classify, request_for, studio_zero_enabled
from tests.test_reviewer import settings

STUDIO = {"deal_type": "rent", "property_type": "studio", "price_amount": 1265, "price_currency": "EUR",
          "location": "La Maternitat i Sant Ramon, Barcelona", "area_m2": 50.0,
          "summary": "Estudio a estrenar de 50 m². Dispone de una habitación y un baño."}


def barcelona(monkeypatch, flag: bool, rooms: int | None = 1, deviations: dict | None = None) -> Request:
    monkeypatch.setenv("STUDIO_ZERO_BEDROOMS", "true" if flag else "false")
    constraints = {"deal": "rent", "max_price": 1500, "property_type": "apartment", "rooms": rooms}
    return request_for(constraints, location="Barcelona", vertical="real_estate", country="ES",
                       deviations=deviations)


@pytest.mark.parametrize("rooms", [1, None, 0])
def test_sim_studio_is_not_exact_for_one_bedroom(monkeypatch, rooms):
    request = barcelona(monkeypatch, True)
    assert request.studio_zero is True
    match = classify({**STUDIO, "rooms": rooms}, request)
    assert (match.bucket, match.why) == ("other", "rooms")


def test_flag_off_as_before(monkeypatch):
    request = barcelona(monkeypatch, False)
    assert request.studio_zero is False
    assert classify({**STUDIO, "rooms": 1}, request).bucket == "exact"
    assert classify({**STUDIO, "rooms": None}, request).bucket == "exact"


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("STUDIO_ZERO_BEDROOMS", raising=False)
    assert studio_zero_enabled() is False and Request().studio_zero is False
    assert settings().studio_zero_bedrooms is False
    assert settings(STUDIO_ZERO_BEDROOMS="true").studio_zero_bedrooms is True


def test_no_rooms_in_the_task_is_unchanged(monkeypatch):
    request = barcelona(monkeypatch, True, rooms=None)
    assert classify({**STUDIO, "rooms": None}, request).bucket == "exact"


def test_a_flat_with_unknown_rooms_is_unchanged(monkeypatch):
    request = barcelona(monkeypatch, True)
    flat = {**STUDIO, "property_type": "apartment", "rooms": None, "summary": "Piso de 50 m²."}
    assert classify(flat, request).bucket == "exact"  # unknown count: the reviewer's «unknown» makes it similar
    assert classify({**flat, "rooms": 1}, request).bucket == "exact"


def test_a_studio_naming_two_rooms_keeps_its_count(monkeypatch):
    request = barcelona(monkeypatch, True, rooms=2)
    assert classify({**STUDIO, "rooms": 2}, request).bucket == "exact"
    assert classify({**STUDIO, "rooms": 1}, request).why == "rooms"


def test_an_approved_studio_instead_of_one_bedroom_still_passes(monkeypatch):
    request = barcelona(monkeypatch, True, deviations={"rooms_delta": 1})
    match = classify({**STUDIO, "rooms": 1}, request)
    assert (match.bucket, match.why, match.covers) == ("exact", "approved_deviation", ("rooms",))
    assert match.note == "студия при запросе от 1 комн."
    assert classify({**STUDIO, "rooms": 1}, barcelona(monkeypatch, True, rooms=2,
                                                       deviations={"rooms_delta": 1})).why == "rooms"
