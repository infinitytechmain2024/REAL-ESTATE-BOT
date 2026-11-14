"""INTAKE_ASK_CITY_OR_PROVINCE (Q62): a bare province capital is asked «only the city or the whole province?».

Scripted fake interviewer (no network) or the rules, the intake on its in-memory store.
"""

from __future__ import annotations

from typing import Any

import pytest

from bot.campaign.spec import TaskSpec
from bot.orchestra.parser import parse_campaign_spec
from tests.test_interviewer import MADRID, FakeInterviewer, ask, with_fake
from tests.test_user_intake import USER, callbacks, enough, press, say

SCOPE_Q = "Искать только в самом городе Мадрид или по всей провинции?"
UTILITIES_Q = "Что обязательно по коммуникациям и подъезду? Например: «вода и свет обязательно», «не важно»."
LAND = {"deal": "sale", "property_type": "land", "budget": {"max": 3000000, "currency": "EUR"}}
MALAGA = {"name": "Málaga", "country": "ES", "names": {"ru": "Малага", "es": "Málaga"}}
MARBELLA = {"name": "Marbella", "country": "ES", "names": {"ru": "Марбелья", "es": "Marbella"}}


async def _first(task: str, place: dict[str, Any] = MADRID, *, on: bool = True,
                 districts: list[str] | None = None):  # type: ignore[no-untyped-def]
    """The model reads the whole task (no hard field left) and asks a task question."""
    spec = {**LAND, "place": {**place, "level": "city", "districts": districts or []}}
    fake = FakeInterviewer(ask(UTILITIES_Q, "context.answers", spec),
                           ask(UTILITIES_Q, "context.answers", {"place": {**place, "level": "city"}}))
    control, sink, _ = with_fake(fake)
    control.intake.ask_city_or_province = on
    await press(control, USER, "mode:real_estate")
    shown = await say(control, USER, task)
    return control, sink, fake, shown


async def _launched(control, sink) -> tuple[str, TaskSpec]:  # type: ignore[no-untyped-def]
    card = await enough(control, USER)
    await press(control, USER, "task:launch")
    [envelope] = sink.envelopes
    return card.text, TaskSpec.model_validate(parse_campaign_spec(envelope.arguments))


@pytest.mark.asyncio
async def test_a_bare_madrid_is_asked_city_or_province_with_buttons() -> None:
    _, _, _, shown = await _first("участок под застройку в Мадриде до 3 млн, покупка")
    assert shown.text.endswith(SCOPE_Q)
    assert callbacks(shown)[:3] == ["task:scope:city", "task:scope:province", "task:skip"]


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["task:scope:province", "task:skip"])
async def test_whole_province_or_not_important_widens_to_the_province(answer: str) -> None:
    control, sink, fake, _ = await _first("участок под застройку в Мадриде до 3 млн, покупка")
    nxt = await press(control, USER, answer)
    assert nxt.text.endswith(UTILITIES_Q) and fake.calls[1]["message"] == "По всей провинции Мадрид"
    card, spec = await _launched(control, sink)
    assert spec.place.level == "province" and spec.place.name == "Madrid"
    assert "Место: Мадрид и провинция" in card


@pytest.mark.asyncio
@pytest.mark.parametrize(("typed", "level"), [("вся провинция", "province"), ("Малага и Коста-дель-Соль", "province"),
                                              ("только город", "city"), ("solo la ciudad", "city")])
async def test_a_typed_answer_sets_the_level(typed: str, level: str) -> None:
    place = MALAGA if "Малага" in typed else MADRID
    control, sink, _, shown = await _first("ищу участок в " + ("Малаге" if place is MALAGA else "Мадриде"), place)
    assert "по всей провинции" in shown.text
    await say(control, USER, typed)
    _, spec = await _launched(control, sink)
    assert spec.place.level == level


@pytest.mark.asyncio
async def test_only_the_city_keeps_the_city_and_is_not_asked_again() -> None:
    control, sink, _, _ = await _first("участок под застройку в Мадриде до 3 млн, покупка")
    nxt = await press(control, USER, "task:scope:city")
    assert nxt.text.endswith(UTILITIES_Q)
    again = await say(control, USER, "вода и свет")
    assert SCOPE_Q not in again.text
    card, spec = await _launched(control, sink)
    assert spec.place.level == "city" and "провинция" not in card


@pytest.mark.asyncio
@pytest.mark.parametrize(("task", "place", "districts"), [
    ("участок в городе Мадрид до 3 млн", MADRID, None),
    ("участок в провинции Малага до 3 млн", MALAGA, None),
    ("участок в Марбелье до 3 млн", MARBELLA, None),
    ("квартира в Чамбери, Мадрид до 3 млн", MADRID, None),  # another place by name, the model put no district
    ("квартира в Мадриде до 3 млн", MADRID, ["Chamberí"]),
    ("квартира на calle Alcalá в Мадриде", MADRID, None),
])
async def test_not_asked_when_the_person_said_which_or_named_another_place(task: str, place: dict[str, Any],
                                                                          districts: list[str] | None) -> None:
    _, _, _, shown = await _first(task, place, districts=districts)
    assert shown.text.endswith(UTILITIES_Q) and "по всей провинции" not in shown.text


@pytest.mark.asyncio
async def test_flag_off_asks_nothing_and_keeps_the_city() -> None:
    control, sink, _, shown = await _first("участок под застройку в Мадриде до 3 млн, покупка", on=False)
    assert shown.text.endswith(UTILITIES_Q)
    _, spec = await _launched(control, sink)
    assert spec.place.level == "city"


@pytest.mark.asyncio
async def test_not_asked_after_enough_or_when_the_round_cap_is_reached() -> None:
    control, _, _, _ = await _first("участок под застройку в Мадриде до 3 млн, покупка", on=False)
    control.intake.ask_city_or_province = True
    card = await enough(control, USER)
    assert "Проверьте задачу" in card.text
    control, _, _, _ = await _first("участок под застройку в Мадриде до 3 млн, покупка", on=False)
    control.intake.ask_city_or_province = True
    control.intake.max_rounds = 1
    nxt = await say(control, USER, "вода и свет")
    assert "по всей провинции" not in nxt.text


@pytest.mark.asyncio
async def test_the_rules_ask_it_too_and_the_province_reaches_the_card() -> None:
    from tests.test_user_intake import plane

    control, sink, _ = plane()
    control.intake.ask_city_or_province = True
    await press(control, USER, "mode:real_estate")
    reply = await say(control, USER, "купить участок в Мадриде до 300000 евро")
    for _ in range(6):
        if "по всей провинции" in reply.text:
            break
        reply = await press(control, USER, "task:skip")
    assert reply.text.endswith(SCOPE_Q)
    await press(control, USER, "task:scope:province")
    _, spec = await _launched(control, sink)
    assert spec.place.level == "province" and spec.place.name == "Madrid"


# --- review fixes ------------------------------------------------------------------------------


@pytest.mark.parametrize(("typed", "level"), [
    ("весь Мадрид", "city"), ("в любом районе", "city"), ("весь город", "city"), ("в центре", "city"),
    ("не провинция", "city"), ("solo la ciudad", "city"), ("только Мадрид", "city"),
    ("и город и провинция", "province"), ("не город", "province"), ("не только город", "province"),
    ("не только в самом городе", "province"), ("везде", "province"), ("побережье", "province"),
    ("Коста-дель-Соль", "province"), ("no solo la ciudad", "province"), ("вся провинция", "province"),
    ("север: Алькобендас", None),
])
def test_scope_in_text(typed: str, level: str | None) -> None:
    from bot.campaign.spec import Place
    from bot.control_plane.intake import _scope_in_text

    assert _scope_in_text(typed, Place.model_validate(MADRID)) == level


@pytest.mark.asyncio
@pytest.mark.parametrize("task", [
    "квартира в Мадриде как на Idealista, до 3 млн",
    "квартира в Мадриде, Испания, до 3 млн",
    "квартира в Мадриде рядом с метро Gran Vía до 3 млн",
    "квартира в Мадриде, видел на Fotocasa и Airbnb",
])
async def test_the_country_portals_and_metro_stations_are_no_other_place(task: str) -> None:
    _, _, _, shown = await _first(task)
    assert shown.text.endswith(SCOPE_Q)


@pytest.mark.asyncio
@pytest.mark.parametrize("task", ["квартира в центре Мадрида до 3 млн", "piso en el centro de Madrid"])
async def test_the_centre_means_the_city(task: str) -> None:
    _, _, _, shown = await _first(task)
    assert "по всей провинции" not in shown.text


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["Ceuta", "Melilla"])
async def test_ceuta_and_melilla_are_not_asked(name: str) -> None:
    place = {"name": name, "country": "ES", "names": {"ru": name, "es": name}}
    _, _, _, shown = await _first(f"участок в {name} до 3 млн", place)
    assert "по всей провинции" not in shown.text


async def _rules_scope_question():  # type: ignore[no-untyped-def]
    from tests.test_user_intake import plane

    control, sink, _ = plane()
    control.intake.ask_city_or_province = True
    await press(control, USER, "mode:real_estate")
    reply = await say(control, USER, "купить участок в Мадриде до 300000 евро")
    for _ in range(6):
        if "по всей провинции" in reply.text:
            break
        reply = await press(control, USER, "task:skip")
    assert reply.text.endswith(SCOPE_Q)
    return control, sink


@pytest.mark.asyncio
async def test_rules_the_whole_province_is_not_followed_by_a_district_question() -> None:
    control, sink = await _rules_scope_question()
    nxt = await press(control, USER, "task:scope:province")
    assert "Район" not in nxt.text and "район" not in nxt.text
    _, spec = await _launched(control, sink)
    assert spec.place.level == "province"


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["нет", "-"])
async def test_rules_no_to_the_scope_question_keeps_the_city(typed: str) -> None:
    control, sink = await _rules_scope_question()
    await say(control, USER, typed)
    _, spec = await _launched(control, sink)
    assert spec.place.level == "city"
