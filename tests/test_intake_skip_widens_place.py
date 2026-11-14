"""INTAKE_SKIP_WIDENS_PLACE (Q42): «Не важно» to «only the city or around it» widens the place.

Scripted fake interviewer (no network), the intake on its in-memory store.
"""

from __future__ import annotations

import pytest

from bot.campaign.spec import TaskSpec
from bot.orchestra.parser import parse_campaign_spec
from tests.test_interviewer import MADRID, FakeInterviewer, ask, with_fake
from tests.test_user_intake import USER, enough, press, say

SCOPE_Q = ("Искать только в самом городе Мадрид или и в городах вокруг, по провинции? Например: «только город», "
           "«вся провинция Мадрид», «север: Алькобендас, Las Rozas».")
AROUND_Q = "Только сам город или и окрестности тоже? Например: «только город», «можно рядом»."
UTILITIES_Q = "Что обязательно по коммуникациям и подъезду? Например: «вода и свет обязательно», «не важно»."
LAND = {"place": MADRID, "deal": "sale", "property_type": "land", "budget": {"max": 3000000, "currency": "EUR"}}


async def _asked(question: str, asking: str, *, widen: bool):  # type: ignore[no-untyped-def]
    """The scope (or another) question is on screen; the model then resends the place as a city."""
    fake = FakeInterviewer(ask(question, asking, LAND),
                           ask(UTILITIES_Q, "context.answers", {"place": {**MADRID, "level": "city"}}))
    control, sink, _ = with_fake(fake)
    control.intake.skip_widens_place = widen
    await press(control, USER, "mode:real_estate")
    shown = await say(control, USER, "нужен участок в мадриде под покупку до 3 млн")
    assert shown.text.endswith(question)
    return control, sink, fake


async def _launched_spec(control, sink) -> tuple[str, TaskSpec]:  # type: ignore[no-untyped-def]
    card = await enough(control, USER)
    await press(control, USER, "task:launch")
    [envelope] = sink.envelopes
    return card.text, TaskSpec.model_validate(parse_campaign_spec(envelope.arguments))


@pytest.mark.asyncio
@pytest.mark.parametrize("asking", ["place.districts", "context.answers"])
async def test_not_important_to_the_scope_question_widens_the_city_to_its_province(asking: str) -> None:
    control, sink, fake = await _asked(SCOPE_Q, asking, widen=True)
    nxt = await press(control, USER, "task:skip")
    assert nxt.text.endswith(UTILITIES_Q) and fake.calls[1]["message"] == "Не важно"
    card, spec = await _launched_spec(control, sink)
    assert "Место: Мадрид и провинция" in card and "Город: Мадрид" not in card
    assert spec.place.level == "province" and spec.place.name == "Madrid" and spec.place.radius_km is None


@pytest.mark.asyncio
async def test_a_question_about_the_towns_around_without_a_province_widens_to_a_circle() -> None:
    control, sink, _ = await _asked(AROUND_Q, "place.districts", widen=True)
    await say(control, USER, "не важно")  # typed: the same as the button
    card, spec = await _launched_spec(control, sink)
    assert spec.place.level == "city" and spec.place.radius_km == 30
    assert "Город: Мадрид" in card and "Радиус: 30 км" in card


@pytest.mark.asyncio
async def test_flag_off_keeps_the_city() -> None:
    control, sink, _ = await _asked(SCOPE_Q, "place.districts", widen=False)
    await press(control, USER, "task:skip")
    card, spec = await _launched_spec(control, sink)
    assert spec.place.level == "city" and spec.place.radius_km is None
    assert "Город: Мадрид\n" in card and "провинция" not in card


@pytest.mark.asyncio
async def test_not_important_to_another_question_changes_no_place() -> None:
    control, sink, _ = await _asked("Какой статус земли подойдёт? Например: «только urbano», «не важно».",
                                    "context.answers", widen=True)
    await press(control, USER, "task:skip")
    _, spec = await _launched_spec(control, sink)
    assert spec.place.level == "city" and spec.place.radius_km is None
    control, sink, _ = await _asked("Сколько соток минимум?", "area_m2.min", widen=True)
    await press(control, USER, "task:skip")
    _, spec = await _launched_spec(control, sink)
    assert spec.place.level == "city" and spec.place.radius_km is None and spec.is_unspecified("area_m2.min")


@pytest.mark.asyncio
async def test_an_explicit_answer_only_the_city_stays_the_city() -> None:
    control, sink, fake = await _asked(SCOPE_Q, "place.districts", widen=True)
    await say(control, USER, "solo la ciudad")
    assert fake.calls[1]["message"] == "solo la ciudad"
    card, spec = await _launched_spec(control, sink)
    assert spec.place.level == "city" and spec.place.radius_km is None and "Город: Мадрид" in card
