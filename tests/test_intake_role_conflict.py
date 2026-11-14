"""INTAKE_ROLE_CONFLICT_CHECK (Q52): a role answer against the task's own words is asked about once.

Scripted fake interviewer (no network) or the rules, the intake on its in-memory store.
"""

from __future__ import annotations

from typing import Any

import pytest

from bot.campaign.spec import TaskSpec
from bot.control_plane.intake import ROLE_CONFLICT_QUESTIONS, Draft, role_in_text
from bot.orchestra.parser import parse_campaign_spec
from tests.test_interviewer import FakeInterviewer, ask, with_fake
from tests.test_user_intake import USER, callbacks, enough, plane, press, say

MALAGA = {"name": "Málaga", "country": "ES", "names": {"ru": "Малага", "es": "Málaga"}}
TASK = "ищу соинвесторов в проекты в Малаге, тикет от 50 000 €"
BASE = {"place": MALAGA, "investor": {"ticket": {"min": 50000, "currency": "EUR"}}}
WHO_Q = "Каких инвесторов ищете? Например: частные инвесторы, фонды."
ROLE_Q = "Вы ищете деньги в свои проекты или сами хотите вложить вместе с другими?"
TERM_Q = "На какой срок готовы вкладываться в проект? Например: до 1 года, 1-2 года."
CLARIFY = ROLE_CONFLICT_QUESTIONS["raising"]


def _role(role: str) -> dict[str, Any]:
    return {"investor": {"user_role": role}}


async def _role_asked(answer_role: str | None, *, check: bool = True, task: str = TASK, after: Any = None,  # type: ignore[no-untyped-def]
                     mode: str = "investors"):
    """The role question is on screen; the model reads the next answer as ``answer_role``."""
    turns: list[Any] = [
        ask(WHO_Q, "investor.who", BASE),
        {**ask(ROLE_Q, "investor.user_role", {"investor": {"who": ["private"]}}),
         "options": ["Ищу деньги", "Хочу вкладывать"]},
        ask(TERM_Q, "context.answers", _role(answer_role) if answer_role else {}),
    ]
    if after is not None:
        turns.append(after)
    fake = FakeInterviewer(*turns)
    control, sink, _ = with_fake(fake)
    control.intake.role_conflict_check, control.intake.both_mode = check, True
    await press(control, USER, f"mode:{mode}")
    assert (await say(control, USER, task)).text.endswith(WHO_Q)
    assert (await say(control, USER, "частные инвесторы в недвижимость")).text.endswith(ROLE_Q)
    return control, sink, fake


async def _launched(control, sink) -> TaskSpec:  # type: ignore[no-untyped-def]
    await enough(control, USER)
    await press(control, USER, "task:launch")
    [envelope] = sink.envelopes
    return TaskSpec.model_validate(parse_campaign_spec(envelope.arguments))


def test_the_task_words_name_one_role_or_none() -> None:
    assert role_in_text(TASK) == "raising"
    assert role_in_text("ищу соинвесторов (частных инвесторов) в свои проекты реновации квартир в Малаге") == "raising"
    assert role_in_text("busco socios") == role_in_text("inversores para mi proyecto") == "raising"
    assert role_in_text("ищу финансирование для реновации") == "raising"
    assert role_in_text("хочу вложить 200 тыс в Малаге") == role_in_text("quiero invertir en Málaga") == "deploying"
    assert role_in_text("busco proyectos en Valencia") == "deploying"
    assert role_in_text("инвесторы для стартапа в Барселоне") is None
    assert role_in_text("частные инвесторы в недвижимость") is None
    # a negation up to 2 words before the words: no such role
    assert role_in_text("Я инвестор, не ищу инвесторов") is None
    assert role_in_text("не очень хочу вкладывать") is None
    assert role_in_text("no busco socios, quiero invertir") == "deploying"
    assert role_in_text("I am not looking for investors") is None


@pytest.mark.asyncio
async def test_a_contradicting_answer_is_asked_about_once_with_buttons() -> None:
    control, _, fake = await _role_asked("deploying")
    clarify = await say(control, USER, "хочу вкладывать")
    assert clarify.text == CLARIFY and "Понял" not in clarify.text
    assert callbacks(clarify) == ["task:role:raising", "task:role:deploying", "task:cancel"]  # no skip, no «enough»
    assert [b.text for b in clarify.buttons][:2] == ["Ищу деньги в свой проект", "Хочу вложить свои деньги"]
    assert len(fake.calls) == 3  # the model is not asked for the next question yet
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.asking == "investor.user_role" and draft.spec is not None
    assert draft.spec.investor.user_role is None and draft.role_check == "asking"
    loaded = Draft.load(USER, USER, draft.mode, draft.step, draft.payload())  # the Postgres store keeps the state
    assert loaded.role_check == "asking"


@pytest.mark.asyncio
async def test_the_button_answer_sets_the_role_even_if_the_model_reads_it_otherwise() -> None:
    control, sink, fake = await _role_asked("deploying", after=ask(TERM_Q, "context.answers", _role("deploying")))
    await say(control, USER, "хочу вкладывать")
    nxt = await press(control, USER, "task:role:raising")
    assert nxt.text.endswith(TERM_Q) and fake.calls[3]["asking"] == "investor.user_role"
    assert (await _launched(control, sink)).investor.user_role == "raising"


@pytest.mark.asyncio
async def test_a_typed_answer_sets_the_role_the_model_did_not_read_and_is_not_asked_again() -> None:
    control, sink, _ = await _role_asked("deploying", after=ask(TERM_Q, "context.answers", {}))
    await say(control, USER, "хочу вкладывать")
    nxt = await say(control, USER, "ищу соинвесторов для своих проектов в Малаге, тикет от 50 тыс €")
    assert nxt.text.endswith(TERM_Q) and CLARIFY not in nxt.text
    assert (await _launched(control, sink)).investor.user_role == "raising"


@pytest.mark.asyncio
async def test_confirming_the_new_role_keeps_it_without_a_second_question() -> None:
    control, sink, _ = await _role_asked("deploying", after=ask(TERM_Q, "context.answers", _role("deploying")))
    await say(control, USER, "хочу вкладывать")
    nxt = await press(control, USER, "task:role:deploying")
    assert nxt.text.endswith(TERM_Q)
    assert (await _launched(control, sink)).investor.user_role == "deploying"


@pytest.mark.asyncio
@pytest.mark.parametrize(("task", "role"), [
    (TASK, "raising"),  # the answer agrees with the task
    ("частные инвесторы в недвижимость в Малаге, тикет от 50 000 €", "deploying"),  # the task names no role
])
async def test_no_contradiction_no_question(task: str, role: str) -> None:
    control, sink, _ = await _role_asked(role, task=task)
    nxt = await say(control, USER, "хочу вкладывать" if role == "deploying" else "ищу деньги")
    assert nxt.text.endswith(TERM_Q) and "Вы писали" not in nxt.text
    assert (await _launched(control, sink)).investor.user_role == role


@pytest.mark.asyncio
async def test_flag_off_takes_the_answer_as_before() -> None:
    control, sink, _ = await _role_asked("deploying", check=False)
    nxt = await say(control, USER, "хочу вкладывать")
    assert nxt.text.endswith(TERM_Q) and "Вы писали" not in nxt.text
    assert (await _launched(control, sink)).investor.user_role == "deploying"


@pytest.mark.asyncio
async def test_the_rules_interviewer_asks_too() -> None:
    control, _, _ = plane()
    control.intake.role_conflict_check = True
    await press(control, USER, "mode:investors")
    await say(control, USER, "ищу соинвесторов в свои проекты в Малаге")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None
    draft.spec.investor.user_role, draft.asking = None, "investor.user_role"
    await control.intake.store.save(draft)
    clarify = await say(control, USER, "хочу вкладывать")
    assert clarify.text == CLARIFY
    await press(control, USER, "task:role:raising")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None and draft.spec.investor.user_role == "raising"
    assert draft.role_check == "done"


@pytest.mark.asyncio
async def test_typed_words_do_not_override_another_role_the_model_read() -> None:
    control, sink, _ = await _role_asked("deploying", after=ask(TERM_Q, "context.answers", _role("deploying")))
    await say(control, USER, "хочу вкладывать")
    await say(control, USER, "ищу соинвесторов, но и сам вложу")
    assert (await _launched(control, sink)).investor.user_role == "deploying"


@pytest.mark.asyncio
@pytest.mark.parametrize(("model_role", "expected"), [(None, None), ("raising", "raising")])
async def test_a_negated_answer_sets_no_role_from_its_words(model_role: str | None, expected: str | None) -> None:
    after = ask(TERM_Q, "context.answers", _role(model_role) if model_role else {})
    control, _, _ = await _role_asked("deploying", after=after)
    await say(control, USER, "хочу вкладывать")
    nxt = await say(control, USER, "не хочу вкладывать")
    assert CLARIFY not in nxt.text
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None and draft.spec.investor.user_role == expected


@pytest.mark.asyncio
async def test_a_negated_task_asks_nothing() -> None:
    control, _, _ = await _role_asked("raising", task="Я инвестор, не ищу инвесторов, Малага, тикет от 50 000 €")
    nxt = await say(control, USER, "ищу деньги")
    assert nxt.text.endswith(TERM_Q) and "Вы писали" not in nxt.text


@pytest.mark.asyncio
async def test_both_mode_asks_too() -> None:
    control, _, _ = await _role_asked("deploying", mode="both",
                                      after=ask(TERM_Q, "context.answers", _role("deploying")))
    assert (await say(control, USER, "хочу вкладывать")).text == CLARIFY
    await press(control, USER, "task:role:raising")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.mode == "both" and draft.spec is not None
    assert draft.spec.investor.user_role == "raising" and draft.role_check == "done"
