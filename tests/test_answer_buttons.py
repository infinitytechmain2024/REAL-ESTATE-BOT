"""INTAKE_ANSWER_BUTTONS: answer variants under a question, the control keys on the bottom keyboard, multi-select.

Memory only: the rule interviewer (no network, no AI), ``MemoryIntakeStore`` through ``ControlPlane``.
"""

from __future__ import annotations

import pytest

from bot.control_plane.delivery import KeyboardTracker
from bot.control_plane.intake import (
    ASK_KEYBOARD,
    DRAFT_KEYBOARD,
    ENOUGH_KEY,
    KEYS_NOTE,
    OWN_ANSWER_HINT,
    PLACE_KEYBOARD,
    SKIP_KEY,
    UNDECIDED,
)
from bot.control_plane.models import Button, Reply
from bot.control_plane.settings import ControlPlaneSettings
from tests.test_user_intake import USER, callbacks, plane, press, say

CONTROL_LABELS = {"Не важно", "Хватит, ищи", "Отмена"}


async def on(mode: str = "real_estate"):
    control, sink, _ = plane()
    control.intake.answer_buttons = True
    await press(control, USER, f"mode:{mode}")
    return control, sink


def labels(reply: Reply) -> list[str]:
    return [b.text for b in reply.buttons]


@pytest.mark.asyncio
async def test_under_a_question_only_answers_and_the_control_keys_are_at_the_bottom() -> None:
    control, _ = await on()
    reply = await say(control, USER, "квартира в Мадриде")
    assert reply.text.startswith("Понял: Мадрид")
    assert labels(reply) == ["Аренда", "Покупка", UNDECIDED]
    assert not CONTROL_LABELS & set(labels(reply))
    assert OWN_ANSWER_HINT in reply.text
    assert reply.keyboard == ASK_KEYBOARD and reply.keyboard_note == KEYS_NOTE
    assert (await press(control, USER, "task:deal:rent")).buttons  # the next question has variants too


@pytest.mark.asyncio
async def test_the_bottom_keys_skip_and_stop_the_interview() -> None:
    control, _ = await on()
    await say(control, USER, "снять квартиру в Мадриде до 1200 €")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.asking == "rooms.min"
    reply = await say(control, USER, SKIP_KEY)
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None and draft.spec.is_unspecified("rooms.min")
    card = await say(control, USER, ENOUGH_KEY)
    assert card.text.startswith("Проверьте задачу") and reply.text != card.text


@pytest.mark.asyncio
async def test_undecided_deal_counts_as_does_not_matter() -> None:
    control, _ = await on()
    await say(control, USER, "квартира в Мадриде")
    await press(control, USER, "task:skip")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None and draft.spec.is_unspecified("deal")


@pytest.mark.asyncio
async def test_a_missing_place_keeps_no_skip_key() -> None:
    control, _ = await on()
    reply = await say(control, USER, "снять квартиру до 1200 €")
    assert reply.keyboard == PLACE_KEYBOARD and SKIP_KEY not in sum(reply.keyboard or (), ())


@pytest.mark.asyncio
async def test_a_budget_variant_is_an_answer() -> None:
    control, _ = await on()
    reply = await say(control, USER, "купить квартиру в Мадриде")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.asking == "budget.max"
    assert labels(reply) == ["до 150 000 €", "до 300 000 €", "до 500 000 €"]
    await press(control, USER, "task:opt:1")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None and draft.spec.budget.max == 300000


@pytest.mark.asyncio
async def test_must_have_is_a_multi_select_with_done() -> None:
    control, _ = await on()
    await say(control, USER, "снять квартиру в Мадриде до 1200 €")
    await say(control, USER, "2")  # rooms
    for _ in range(4):
        draft = await control.intake.store.get(USER)
        assert draft is not None
        if draft.asking == "must_have":
            break
        await press(control, USER, "task:skip")
    assert draft.asking == "must_have"
    first = await press(control, USER, "task:pick:0")
    assert first.edit_buttons and labels(first)[0].startswith("✅") and labels(first)[-1] == "Готово"
    second = await press(control, USER, "task:pick:2")
    assert [t[0] for t in labels(second)[:3]] == ["✅", "▫", "✅"]
    untick = await press(control, USER, "task:pick:2")
    assert labels(untick)[2].startswith("▫")
    await press(control, USER, "task:pick:2")
    await press(control, USER, "task:done")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None and draft.spec.must_have == ["Парковка", "Лифт"]


@pytest.mark.asyncio
async def test_typed_words_in_a_multi_select_join_the_ticked_variants() -> None:
    control, _ = await on("investors")
    await say(control, USER, "нужны деньги на проект в Валенсии")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.asking == "investor.who" and draft.choices
    await press(control, USER, "task:pick:1")  # «Фонды»
    await say(control, USER, "бизнес-ангелы")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None and "fund" in draft.spec.investor.who


@pytest.mark.asyncio
async def test_nothing_ticked_and_done_is_does_not_matter() -> None:
    control, _ = await on("investors")
    await say(control, USER, "нужны деньги на проект в Валенсии")
    await press(control, USER, "task:done")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None and draft.spec.is_unspecified("investor.who")


@pytest.mark.asyncio
async def test_a_bottom_key_with_no_question_is_not_a_task() -> None:
    control, sink = await on()
    reply = await say(control, USER, SKIP_KEY)
    assert reply.text.startswith("Сейчас нет вопроса") and sink.envelopes == []
    draft = await control.intake.store.get(USER)
    assert draft is None or not draft.task


@pytest.mark.asyncio
async def test_without_the_flag_the_question_keeps_its_inline_controls() -> None:
    control, _, _ = plane()
    await press(control, USER, "mode:real_estate")
    reply = await say(control, USER, "снять квартиру в Мадриде до 1200 €")
    assert callbacks(reply) == ["task:skip", "task:enough", "task:cancel"]
    assert reply.keyboard == DRAFT_KEYBOARD and not reply.keyboard_note


def test_the_keyboard_goes_as_a_second_message_only_when_it_changes() -> None:
    tracker = KeyboardTracker()
    question = Reply("Вопрос?", (Button("A", callback_data="task:opt:0"),), keyboard=ASK_KEYBOARD, keyboard_note=KEYS_NOTE)
    first = tracker.parts(1, question)
    assert [p.text for p in first] == ["Вопрос?", KEYS_NOTE]
    assert first[0].keyboard is None and first[1].keyboard == ASK_KEYBOARD and not first[1].buttons
    assert [p.text for p in tracker.parts(1, question)] == ["Вопрос?"]  # the same keyboard is already there
    assert [p.text for p in tracker.parts(2, question)] == ["Вопрос?", KEYS_NOTE]  # another chat
    tracker.parts(1, Reply("Карточка", keyboard=(("x",),)))
    assert len(tracker.parts(1, question)) == 2  # the card changed it: sent again
    old = Reply("Старый", (Button("A", callback_data="a"),), keyboard=DRAFT_KEYBOARD)
    assert tracker.parts(1, old) == [old]  # no note: unchanged behaviour


def test_the_flag_is_read_from_the_environment_and_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("INTAKE_ANSWER_BUTTONS", raising=False)
    assert ControlPlaneSettings.from_env().intake_answer_buttons is False
    monkeypatch.setenv("INTAKE_ANSWER_BUTTONS", "true")
    assert ControlPlaneSettings.from_env().intake_answer_buttons is True
