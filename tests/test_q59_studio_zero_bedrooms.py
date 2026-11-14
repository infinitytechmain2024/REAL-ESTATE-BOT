"""Q59: a studio has 0 bedrooms against the task's rooms (STUDIO_ZERO_BEDROOMS, off by default). Sim b5-g5-s6:
«от 1 спальни», card 9 «Estudio a estrenar … Dispone de una habitación» (type studio, rooms 1) went out as exact.
Mocks only, no network.
"""

from __future__ import annotations

import pytest

from bot.campaign.offers import unknown_of
from bot.campaign.tolerance import (
    STUDIO_ROOMS_NOTE,
    Request,
    classify,
    request_for,
    studio_zero_enabled,
)
from bot.utils.listing_text import task_names_studio
from tests.test_reviewer import settings

ROOMS_QUESTION = "Сколько спален нужно? Например: студия, 1, 2, от 2 до 3."

STUDIO = {"deal_type": "rent", "property_type": "studio", "price_amount": 1265, "price_currency": "EUR",
          "location": "La Maternitat i Sant Ramon, Barcelona", "area_m2": 50.0,
          "summary": "Estudio a estrenar de 50 m². Dispone de una habitación y un baño."}


def _spec(answer: str) -> dict:
    return {"context": {"answers": [{"question": ROOMS_QUESTION, "answer": answer}]}}


def barcelona(monkeypatch, flag: bool, rooms: int | None = 1, deviations: dict | None = None, *,
              answer: str = "от 1 спальни", text: str = "снять piso в Барселоне до 1500 в месяц") -> Request:
    monkeypatch.setenv("STUDIO_ZERO_BEDROOMS", "true" if flag else "false")
    constraints = {"deal": "rent", "max_price": 1500, "property_type": "apartment", "rooms": rooms}
    return request_for(constraints, location="Barcelona", vertical="real_estate", country="ES",
                       deviations=deviations, text=text, spec=_spec(answer))


@pytest.mark.parametrize("rooms", [None, 0])
def test_a_studio_without_rooms_is_other_for_one_bedroom(monkeypatch, rooms):
    request = barcelona(monkeypatch, True)
    assert request.studio_zero is True
    match = classify({**STUDIO, "rooms": rooms}, request)
    assert (match.bucket, match.why) == ("other", "rooms")


def test_sim_studio_with_one_room_is_similar_unconfirmed(monkeypatch):
    """Card 9: «Dispone de una habitación» may be the studio itself or a real bedroom («piso con estudio»)."""
    match = classify({**STUDIO, "rooms": 1}, barcelona(monkeypatch, True))
    assert (match.bucket, match.why, match.note) == ("similar", "unverified", STUDIO_ROOMS_NOTE)
    assert unknown_of(match.note) == ("число спален (студия)",)


@pytest.mark.parametrize("answer", ["студия", "студию или 1 спальню", "estudio", "loft"])
def test_a_task_asking_for_a_studio_is_unchanged(monkeypatch, answer):
    """The interview's rooms answer «студия» sets rooms.min=1 (bot/control_plane/rules.py): studios stay exact."""
    request = barcelona(monkeypatch, True, answer=answer)
    assert request.studio_zero is False
    assert classify({**STUDIO, "rooms": None}, request).bucket == "exact"
    assert classify({**STUDIO, "rooms": 1}, request).bucket == "exact"


def test_a_studio_in_the_task_text_is_unchanged(monkeypatch):
    request = barcelona(monkeypatch, True, answer="не важно", text="снять студию в Барселоне до 1500")
    assert request.studio_zero is False


@pytest.mark.parametrize(("words", "named"), [("студия", True), ("не студия", False), ("без студий", False),
                                              ("от 1 спальни", False), ("Studio flat", True), ("квартира", False)])
def test_task_names_studio(words, named):
    assert task_names_studio(None, words) is named


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
    assert classify({**STUDIO, "rooms": 1}, request).why == "rooms"  # 1 < 2: the ordinary rooms rule


def test_an_approved_studio_instead_of_one_bedroom_still_passes(monkeypatch):
    request = barcelona(monkeypatch, True, deviations={"rooms_delta": 1})
    match = classify({**STUDIO, "rooms": 1}, request)
    assert (match.bucket, match.why, match.covers) == ("exact", "approved_deviation", ("rooms",))
    assert match.note == "студия при запросе от 1 комн."
    two = barcelona(monkeypatch, True, rooms=2, deviations={"rooms_delta": 1})  # one room against 2: the usual rule
    assert classify({**STUDIO, "rooms": 1}, two).note == "комнат: 1 при запросе от 2"
    assert classify({**STUDIO, "rooms": None}, two).why == "rooms"  # 0 bedrooms is beyond the approved one fewer
