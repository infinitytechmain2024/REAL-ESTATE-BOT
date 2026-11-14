"""INTAKE_SKIP_WIDENS_PLACE (Q42): «Не важно» to «only the city or around it» widens the place.

Scripted fake interviewer (no network), the intake on its in-memory store.
"""

from __future__ import annotations

from typing import Any

import pytest

from bot.campaign.spec import TaskSpec
from bot.orchestra.parser import parse_campaign_spec
from tests.test_interviewer import MADRID, FakeInterviewer, ask, with_fake
from tests.test_user_intake import USER, enough, press, say

SCOPE_Q = ("Искать только в самом городе Мадрид или и в городах вокруг, по провинции? Например: «только город», "
           "«вся провинция Мадрид», «север: Алькобендас, Las Rozas».")
UTILITIES_Q = "Что обязательно по коммуникациям и подъезду? Например: «вода и свет обязательно», «не важно»."
LAND = {"deal": "sale", "property_type": "land", "budget": {"max": 3000000, "currency": "EUR"}}
MARBELLA = {"name": "Marbella", "country": "ES", "names": {"ru": "Марбелья", "es": "Marbella"}}
VALENCIA = {"name": "Valencia", "country": "ES", "names": {"ru": "Валенсия", "es": "Valencia"}}


async def _asked(question: str, asking: str, *, widen: bool = True, place: dict[str, Any] = MADRID,
                 then: Any = None):  # type: ignore[no-untyped-def]
    """The question is on screen; the model then resends the place as a city (or ``then``: e.g. it fails)."""
    nxt = then if then is not None else ask(UTILITIES_Q, "context.answers", {"place": {**place, "level": "city"}})
    fake = FakeInterviewer(ask(question, asking, {**LAND, "place": place}), nxt)
    control, sink, _ = with_fake(fake)
    control.intake.skip_widens_place = widen
    await press(control, USER, "mode:real_estate")
    shown = await say(control, USER, "нужен участок под покупку до 3 млн")
    assert shown.text.endswith(question)
    return control, sink, fake


async def _launched_spec(control, sink) -> tuple[str, TaskSpec]:  # type: ignore[no-untyped-def]
    card = await enough(control, USER)
    await press(control, USER, "task:launch")
    [envelope] = sink.envelopes
    return card.text, TaskSpec.model_validate(parse_campaign_spec(envelope.arguments))


async def _skipped(question: str, asking: str = "context.answers", **kwargs: Any) -> tuple[str, TaskSpec]:
    control, sink, _ = await _asked(question, asking, **kwargs)
    await press(control, USER, "task:skip")
    return await _launched_spec(control, sink)


@pytest.mark.asyncio
@pytest.mark.parametrize("asking", ["place.districts", "context.answers"])
async def test_not_important_to_the_scope_question_widens_madrid_to_its_province(asking: str) -> None:
    control, sink, fake = await _asked(SCOPE_Q, asking)
    nxt = await press(control, USER, "task:skip")
    assert nxt.text.endswith(UTILITIES_Q) and fake.calls[1]["message"] == "Не важно"
    card, spec = await _launched_spec(control, sink)
    assert "Место: Мадрид и провинция" in card and "Город: Мадрид" not in card
    assert spec.place.level == "province" and spec.place.name == "Madrid" and spec.place.radius_km is None


@pytest.mark.asyncio
async def test_the_widening_survives_a_failed_interviewer() -> None:
    card, spec = await _skipped(SCOPE_Q, "place.districts", then=RuntimeError("model down"))
    assert spec.place.level == "province" and "Место: Мадрид и провинция" in card


@pytest.mark.asyncio
@pytest.mark.parametrize(("question", "asking", "place"), [
    # Marbella has no province of its name: «Марбелья и провинция» would lie.
    ("Искать только в самом городе Марбелья или по всей провинции Малага?", "context.answers", MARBELLA),
    # the rules' district question is no scope question
    ("Район или вся Валенсия? Например: «центр», «Руссафа и Кабаньял» или «вся Валенсия».", "place.districts",
     VALENCIA),
    ("Нужен ли огород или сад вокруг дома? Например: «нужен огород», «не важно».", "context.answers", MADRID),
    # about the land class, not the scope: it does not name the city
    ("Рассматриваете только рустико в провинции или участок в черте города?", "context.answers", MADRID),
    ("Какой статус земли подойдёт? Например: «только urbano», «не важно».", "context.answers", MADRID),
    ("Сколько соток минимум? Например: от 20.", "area_m2.min", MADRID),
])
async def test_not_important_to_another_question_changes_no_place(question: str, asking: str,
                                                                  place: dict[str, Any]) -> None:
    card, spec = await _skipped(question, asking, place=place)
    assert spec.place.level == "city" and spec.place.radius_km is None and "провинция" not in card


@pytest.mark.asyncio
async def test_flag_off_keeps_the_city() -> None:
    card, spec = await _skipped(SCOPE_Q, "place.districts", widen=False)
    assert spec.place.level == "city" and spec.place.radius_km is None
    assert "Город: Мадрид\n" in card and "провинция" not in card


@pytest.mark.asyncio
async def test_a_province_asked_for_explicitly_keeps_the_old_card_line() -> None:
    control, sink, _ = await _asked(SCOPE_Q, "place.districts",
                                    then=ask(UTILITIES_Q, "context.answers", {"place": {**MADRID, "level": "province"}}))
    await say(control, USER, "вся провинция")
    card, spec = await _launched_spec(control, sink)
    assert spec.place.level == "province" and "Город: Мадрид (область)" in card and "и провинция" not in card


@pytest.mark.asyncio
async def test_an_explicit_answer_only_the_city_stays_the_city() -> None:
    control, sink, fake = await _asked(SCOPE_Q, "place.districts")
    await say(control, USER, "solo la ciudad")
    assert fake.calls[1]["message"] == "solo la ciudad"
    card, spec = await _launched_spec(control, sink)
    assert spec.place.level == "city" and spec.place.radius_km is None and "Город: Мадрид" in card
