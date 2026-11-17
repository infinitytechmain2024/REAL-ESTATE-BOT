"""Q73 (INTAKE_STRICT_QUESTION_MUST, only with INTAKE_MUST_HAVE_EXPLICIT): an answer to a question asking for the
strict or required items is a must, and «Понял: …» follows the final spec.

Sim b7-g2-s8 («квартира в Валенсии»): to «Есть ли строгие требования к квартире: этаж, лифт, состояние?» the answer
«con ascensor, no bajo» has no marker of its own; Q70's safety net made «лифт» a wish, while the reply still said
«Понял: Лифт обязателен, не первый этаж». Mocks only (a scripted model).
"""

from __future__ import annotations

import pytest

from bot.campaign.spec import TaskSpec
from bot.control_plane.interviewer import explicit_must, strict_question, understood_lists
from bot.control_plane.settings import ControlPlaneSettings
from tests.test_interviewer import FakeInterviewer, ask, with_fake
from tests.test_user_intake import USER, press, say

VALENCIA = {"name": "Valencia", "country": "ES", "names": {"ru": "Валенсия", "es": "Valencia", "uk": "Валенсія",
                                                         "en": "Valencia"}}
FLAT = {"place": VALENCIA, "deal": "sale", "property_type": "apartment", "budget": {"max": 250000, "currency": "EUR"},
        "rooms": {"min": 2, "max": 3}}
STRICT_Q = ("Есть ли строгие требования к квартире: этаж, лифт, состояние? Например: «лифт обязателен», "
            "«не первый этаж», «после ремонта», «не важно».")
CONDITION_Q = "Какое состояние квартиры подходит? Например: «после ремонта», «можно под ремонт», «не важно»."
SAID = "Лифт обязателен, не первый этаж"


def _b7_model() -> FakeInterviewer:
    """The model of the sim: «con ascensor» into must_have, «no bajo» into the exclusions, «Лифт обязателен»."""
    return FakeInterviewer(
        ask(STRICT_Q, "context.answers", FLAT),
        ask(CONDITION_Q, "condition", {"must_have": ["лифт"], "exclude": ["первый этаж (bajo)"]}, SAID),
    )


async def _b7(must: bool, strict: bool, question: str = STRICT_Q) -> tuple[str, TaskSpec]:
    model = _b7_model()
    model.turns[0]["question"] = question  # type: ignore[index]
    control, _, _ = with_fake(model)
    control.intake.must_explicit, control.intake.strict_question_must = must, strict
    await press(control, USER, "mode:real_estate")
    assert (await say(control, USER, "Busco piso para comprar en Valencia, 2-3 habitaciones, hasta 250.000 €")
            ).text.endswith(question)
    reply = await say(control, USER, "con ascensor, no bajo")
    assert reply.text.endswith(CONDITION_Q)
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None
    return reply.text, draft.spec


@pytest.mark.asyncio
async def test_q70_alone_lowers_the_lift_and_the_reply_still_says_required() -> None:
    reply, spec = await _b7(True, False)  # the sim: as before Q73
    assert spec.must_have == [] and [(w.text, w.weight) for w in spec.wishes] == [("лифт", 2)]
    assert reply.startswith(f"Понял: {SAID}")


@pytest.mark.asyncio
async def test_on_an_answer_to_the_strict_question_is_a_must() -> None:
    reply, spec = await _b7(True, True)
    assert spec.must_have == ["лифт"] and spec.wishes == [] and spec.exclude == ["первый этаж (bajo)"]
    assert reply.startswith(f"Понял: {SAID}")  # it matches the spec: the model's words are kept


@pytest.mark.asyncio
async def test_on_a_lowered_must_rebuilds_the_reply_from_the_spec() -> None:
    reply, spec = await _b7(True, True, question="Какой этаж и лифт подходят? Например: «не первый этаж», «не важно».")
    assert spec.must_have == [] and [(w.text, w.weight) for w in spec.wishes] == [("лифт", 2)]
    assert reply.startswith("Понял: Пожелания: лифт; исключить: первый этаж (bajo)\n\n")
    assert "обязател" not in reply.split("\n\n")[0].casefold()


@pytest.mark.asyncio
async def test_without_q70_the_flag_does_nothing() -> None:
    reply, spec = await _b7(False, True, question="Какой этаж и лифт подходят? Например: «не первый этаж».")
    assert spec.must_have == ["лифт"] and reply.startswith(f"Понял: {SAID}")


@pytest.mark.parametrize(("question", "strict"), [
    (STRICT_Q, True), ("Что обязательно должно быть?", True), ("Any must-have features?", True),
    ("Which features are required?", True), ("¿Algo imprescindible?", True), ("¿Algo obligatorio?", True),
    ("Що обов'язково має бути?", True), ("Що обов’язково?", True),
    (CONDITION_Q, False), ("Какой этаж подходит?", False), ("", False),
    ("Что желательно, но необязательно?", False), ("Лифт не обязателен?", False),
    ("Что не обязательно, но было бы хорошо?", False), ("Що необов'язково, але бажано?", False),
    ("Anything nice to have, not required?", False), ("¿Algo no imprescindible?", False),
    ("Есть ли нестрогие пожелания?", False),
])
def test_strict_question(question: str, strict: bool) -> None:
    assert strict_question(question) is strict


def test_explicit_must_with_asked_must() -> None:
    before = TaskSpec(mode="real_estate")
    after = TaskSpec(mode="real_estate", must_have=["лифт"], wishes=[{"text": "балкон", "weight": 3}])
    kept = explicit_must(before, after.model_copy(deep=True), "con ascensor", asked_must=True)
    assert kept.must_have == ["лифт"] and kept.wishes[0].weight == 3
    lowered = explicit_must(before, after.model_copy(deep=True), "con ascensor")
    assert lowered.must_have == [] and {(w.text, w.weight) for w in lowered.wishes} == {("лифт", 2), ("балкон", 2)}


def test_understood_lists() -> None:
    before = TaskSpec(mode="real_estate", must_have=["парковка"], wishes=[{"text": "балкон", "weight": 2}])
    after = TaskSpec(mode="real_estate", must_have=["парковка", "лифт"],
                     wishes=[{"text": "балкон", "weight": 2}, {"text": "терраса", "weight": 3}], exclude=["bajo"])
    assert understood_lists(before, after) == "Обязательно: лифт; пожелания: терраса (очень важно); исключить: bajo"
    assert understood_lists(before, before) == ""


def test_the_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("INTAKE_STRICT_QUESTION_MUST", raising=False)
    assert ControlPlaneSettings.from_env().intake_strict_question_must is False
    monkeypatch.setenv("INTAKE_STRICT_QUESTION_MUST", "true")
    assert ControlPlaneSettings.from_env().intake_strict_question_must is True


def test_the_service_passes_the_flag() -> None:
    from bot.control_plane.service import ControlPlane
    from bot.control_plane.store import MemoryControlPlaneStore

    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x", intake_must_have_explicit=True,
                                    intake_strict_question_must=True)
    control = ControlPlane(settings, MemoryControlPlaneStore(), None, None)  # type: ignore[arg-type]
    assert control.intake.must_explicit is True and control.intake.strict_question_must is True
