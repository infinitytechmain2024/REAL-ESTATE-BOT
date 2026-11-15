"""Q70 (INTAKE_MUST_HAVE_EXPLICIT): must_have only what the person calls required.

Sim b6-g9-s7 («участок под застройку в Мадриде»): «Какой статус земли подходит?» — «urbano или urbanizable, под
застройку», «Что обязательно для участка по коммуникациям и подъезду?» — «коммуникации подведены или рядом, подъезд для
техники» went into «Обязательно»; the listings did not state them, no finding was exact. b6-g1-s7: «лучше urbano»
became a wish of weight 3. With the flag the prompt says so and the intake's safety net turns such must-haves into
wishes of weight 2. Off: as before. Mocks only (a scripted model, MockTransport).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from bot.agents.reviewer import parse_review
from bot.campaign.spec import TaskSpec
from bot.control_plane import interviewer as iv
from bot.control_plane.anthropic_interviewer import AnthropicInterviewer, system_prompt
from bot.control_plane.interviewer import OpenRouterInterviewer, explicit_must, must_said
from bot.control_plane.settings import ControlPlaneSettings
from tests.test_interviewer import MADRID, FakeInterviewer, ask, with_fake
from tests.test_user_intake import USER, enough, press, say

LAND = {"place": MADRID, "deal": "sale", "property_type": "land", "budget": {"max": 2000000, "currency": "EUR"},
        "area_m2": {"min": 2000}}
STATUS_Q = "Какой статус земли подходит? Например: только suelo urbano, urbano или urbanizable, не важно."
UTILITIES_Q = "Что обязательно для участка по коммуникациям и подъезду? Например: коммуникации подведены, не важно."
DEVIATION_Q = "Если точных вариантов не будет, что допустимо? Например: бюджет до +10 %, или только точные."
STATUS = "suelo urbano или urbanizable"
UTILITIES = ["коммуникации подведены или рядом", "подъезд для техники"]


def _g9_model() -> FakeInterviewer:
    """The model of the sim: every answer of part B into must_have."""
    return FakeInterviewer(
        ask(STATUS_Q, "context.answers", LAND),
        ask(UTILITIES_Q, "context.answers", {"must_have": [STATUS]}),
        ask(DEVIATION_Q, "deviations", {"must_have": [STATUS, *UTILITIES]}),
    )


async def _g9(flag: bool) -> tuple[str, TaskSpec]:
    control, _, _ = with_fake(_g9_model())
    control.intake.must_explicit = flag
    await press(control, USER, "mode:real_estate")
    assert (await say(control, USER, "участок под застройку в Мадриде от 2000 м²")).text.endswith(STATUS_Q)
    assert (await say(control, USER, "urbano или urbanizable, под застройку")).text.endswith(UTILITIES_Q)
    assert (await say(control, USER, "коммуникации подведены или рядом, подъезд для техники")).text.endswith(DEVIATION_Q)
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None
    return (await enough(control, USER)).text, draft.spec


@pytest.mark.asyncio
async def test_off_the_g9_answers_stay_must_haves() -> None:
    card, spec = await _g9(False)
    assert spec.must_have == [STATUS, *UTILITIES] and spec.wishes == []
    assert "Обязательно: suelo urbano или urbanizable, коммуникации подведены или рядом, подъезд для техники" in card


@pytest.mark.asyncio
async def test_on_the_g9_answers_are_wishes_of_weight_2() -> None:
    card, spec = await _g9(True)
    assert spec.must_have == []
    assert [(w.text, w.weight) for w in spec.wishes] == [(STATUS, 2), *((u, 2) for u in UTILITIES)]
    assert "Обязательно" not in card and "очень важно" not in card
    assert "Пожелания: suelo urbano или urbanizable, коммуникации подведены или рядом, подъезд для техники" in card
    assert len(spec.context.answers) == 2  # the answers themselves are kept


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["обязательно вода и свет", "только urbano", "вода imprescindible",
                                    "sin agua no", "без воды не рассматриваю"])
async def test_on_an_explicit_must_stays_a_must(answer: str) -> None:
    control, _, _ = with_fake(FakeInterviewer(
        ask(STATUS_Q, "context.answers", LAND), ask(DEVIATION_Q, "deviations", {"must_have": ["вода"]})))
    control.intake.must_explicit = True
    await press(control, USER, "mode:real_estate")
    await say(control, USER, "участок под застройку в Мадриде от 2000 м²")
    await say(control, USER, answer)
    card = (await enough(control, USER)).text
    assert "Обязательно: вода" in card and "Пожелания" not in card


@pytest.mark.asyncio
async def test_on_the_first_message_without_a_marker_gives_a_wish() -> None:
    control, _, _ = with_fake(FakeInterviewer(ask(STATUS_Q, "context.answers", {**LAND, "must_have": ["под застройку"]})))
    control.intake.must_explicit = True
    await press(control, USER, "mode:real_estate")
    await say(control, USER, "участок под застройку в Мадриде от 2000 м²")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None
    assert draft.spec.must_have == [] and [(w.text, w.weight) for w in draft.spec.wishes] == [("под застройку", 2)]


# --- the safety net ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("message", "question", "said"), [
    ("обязательно вода", "", True),
    ("вода обязательна", "", True),
    ("only with a pool", "", True),
    ("must have parking", "", True),
    ("нужна вода", "Что обязательно для участка?", True),
    ("нужна вода", "Что по коммуникациям?", False),  # «нужна» only answers a question about what is obligatory
    ("коммуникации подведены или рядом, подъезд для техники", "Что обязательно для участка по коммуникациям?", False),
    ("urbano или urbanizable, под застройку", "Какой статус земли подходит?", False),
    ("не обязательно, но желательно вода", "", False),
    ("необязательно", "", False),
    ("не только urbano", "", False),
])
def test_must_said(message: str, question: str, said: bool) -> None:
    assert must_said(message, question) is said


def test_the_safety_net_keeps_old_must_haves_and_caps_new_weight_3() -> None:
    before = TaskSpec.model_validate({"must_have": ["лифт"], "wishes": [{"text": "терраса", "weight": 3}]})
    after = before.merged({"must_have": ["лифт", "парковка"],
                           "wishes": [{"text": "терраса", "weight": 3}, {"text": "земля urbano", "weight": 3},
                                      {"text": "парковка", "weight": 1}]})
    out = explicit_must(before, after, "лучше urbano, парковка")
    assert out.must_have == ["лифт"]  # the earlier must stays, the new one has no marker
    # the old weight 3 stays, the new one without «очень важно» is 2, the existing wish keeps its weight
    assert [(w.text, w.weight) for w in out.wishes] == [("терраса", 3), ("земля urbano", 2), ("парковка", 1)]
    very = before.merged({"wishes": [{"text": "земля urbano", "weight": 3}]})
    assert [w.weight for w in explicit_must(before, very, "urbano, это очень важно").wishes] == [3]


@pytest.mark.asyncio
async def test_editing_and_the_rules_are_not_touched() -> None:
    spec = TaskSpec.model_validate({**LAND, "must_have": [STATUS]})
    control, _, _ = with_fake(FakeInterviewer(ask(DEVIATION_Q, "deviations", {"must_have": [STATUS, "вода"]})))
    control.intake.must_explicit = True
    draft = await control.intake._draft(USER, USER)
    draft.mode, draft.spec = "real_estate", spec
    turn = await control.intake._turn(draft, "real_estate", spec, "must_have: вода", True)
    assert turn.spec.must_have == [STATUS, "вода"]  # an edit of the field is the person's own word


# --- the prompt ----------------------------------------------------------------------------------------------------


def test_the_prompt_off_is_byte_for_byte_and_on_has_the_rule() -> None:
    assert iv.system_text() == iv.SYSTEM and iv.system_text(True) == iv.SYSTEM_SELLER
    assert iv.prompt_version() == "interview-v3" and iv.prompt_version(True) == "interview-v3-must"
    on = iv.system_text(must_explicit=True)
    assert on.endswith(iv.MUST_RULE) and "MUST-HAVE ONLY WHEN SAID" in on and "MUST-HAVE" not in iv.SYSTEM
    assert iv.MUST_EXAMPLE in on and '"must_have": ["можно с собакой"]' not in on
    assert '"must_have": ["лифт"]' in on  # «лифт обязательно» is still a must
    seller_on = iv.system_text(True, True)
    assert "ROLE selling" in seller_on and seller_on.endswith(iv.MUST_RULE)
    assert system_prompt("real_estate").startswith(iv.SYSTEM)
    assert system_prompt("real_estate", must_explicit=True).startswith(on)
    assert system_prompt("unknown", must_explicit=True) == on


@pytest.mark.asyncio
async def test_the_clients_send_the_must_prompt_only_with_the_flag() -> None:
    sent: list[dict[str, Any]] = []

    def openrouter(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        content = json.dumps({"spec": {}, "question": "Какой бюджет?", "done": False})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    for flag in (False, True):
        model = OpenRouterInterviewer(api_key="k", model="m", timeout_seconds=5, must_explicit=flag,
                                      client=httpx.AsyncClient(transport=httpx.MockTransport(openrouter)))
        await model.interview(mode="real_estate", spec=TaskSpec(mode="real_estate"), dialogue=[], message="участок")
    assert sent[0]["messages"][0]["content"] == iv.SYSTEM
    assert sent[1]["messages"][0]["content"] == iv.system_text(must_explicit=True)

    def anthropic(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"content": [{"type": "tool_use", "name": "submit_turn", "input": {
            "spec": {}, "question": "Какой бюджет?", "done": False}}], "usage": {}})

    for flag in (False, True):
        claude = AnthropicInterviewer(api_key="k", model="m", must_explicit=flag,
                                      client=httpx.AsyncClient(transport=httpx.MockTransport(anthropic)))
        await claude.interview(mode="real_estate", spec=TaskSpec(mode="real_estate"), dialogue=[], message="участок")
    assert "MUST-HAVE ONLY WHEN SAID" not in sent[2]["system"]
    assert "MUST-HAVE ONLY WHEN SAID" in sent[3]["system"]


# --- the review: an unconfirmed wish of weight 2 keeps the finding exact (RELEVANCE_SOFT_WISHES, Q9) ---------------


@pytest.mark.parametrize(("weight", "overall"), [(2, "match"), (3, "near")])
def test_an_unknown_wish_of_weight_2_does_not_make_a_finding_similar(weight: int, overall: str) -> None:
    wishes = [{"id": i, "text": t, "weight": weight} for i, t in enumerate([STATUS, *UTILITIES], 1)]
    answer = {"criteria": [{"name": n, "verdict": "pass", "quote": "Parcela en Madrid", "note_ru": "да"}
                           for n in ("place", "deal", "type", "budget", "area")],
              "overall": "near", "deviation_ru": "не подтверждены коммуникации", "confidence": 0.8,
              "wishes": [{"id": w["id"], "text": w["text"], "verdict": "unknown", "quote": None} for w in wishes],
              "near_reason": "wish"}
    review = parse_review(json.dumps(answer, ensure_ascii=False), expected=["place", "deal", "type", "budget", "area"],
                          soft_wishes=True, task_wishes=wishes)
    assert review.overall == overall  # weight 3 («очень важно») unconfirmed is still a compromise


# --- settings ------------------------------------------------------------------------------------------------------


def test_the_flag_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("INTAKE_MUST_HAVE_EXPLICIT", raising=False)
    assert ControlPlaneSettings.from_env().intake_must_have_explicit is False
    monkeypatch.setenv("INTAKE_MUST_HAVE_EXPLICIT", "true")
    assert ControlPlaneSettings.from_env().intake_must_have_explicit is True
