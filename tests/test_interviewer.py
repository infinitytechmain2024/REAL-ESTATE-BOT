"""The interviewer ("grill me"): TaskSpec, the AI client, and the intake dialogue with a scripted fake model.

No network: ``FakeInterviewer`` hands out scripted JSON exactly as the model would return it (parsed by the
real ``parse_turn``), and the intake runs on its in-memory store.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.models import CampaignPlan
from bot.campaign.spec import Rooms, TaskSpec, parse_number
from bot.control_plane.intake import (
    DRAFT_KEYBOARD,
    TASK_KEYBOARD,
    Draft,
    MemoryIntakeStore,
    TaskIntake,
    encode_spec,
)
from bot.control_plane.interviewer import (
    SYSTEM,
    InterviewError,
    InterviewTurn,
    OpenRouterInterviewer,
    parse_turn,
)
from bot.control_plane.models import IncomingMessage
from bot.control_plane.rules import RuleInterviewer
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.orchestra.parser import CommandValidationError, parse_campaign_goal, parse_campaign_spec
from tests.test_user_intake import (
    OWNER,
    USER,
    FakeTranscriber,
    callbacks,
    claimed,
    dispatch,
    enough,
    plane,
    press,
    say,
)

MADRID = {"name": "Madrid", "country": "ES", "names": {"ru": "Мадрид", "es": "Madrid", "uk": "Мадрид",
                                                        "ru_in": "Мадриде", "uk_in": "Мадриді"}}


class FakeInterviewer:
    """Scripted model answers (dicts as the model's JSON, or exceptions); records every call."""

    model = "fake/interview"

    def __init__(self, *turns: dict[str, Any] | BaseException) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    async def interview(self, *, mode: str, spec: TaskSpec, dialogue: list[dict[str, str]], message: str,
                        asking: str | None = None, editing: bool = False) -> InterviewTurn:
        self.calls.append({"mode": mode, "spec": spec.model_copy(deep=True), "dialogue": [dict(t) for t in dialogue],
                           "message": message, "asking": asking, "editing": editing})
        item = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
        if isinstance(item, BaseException):
            raise item
        return parse_turn(json.dumps(item, ensure_ascii=False), spec)


def ask(question: str, asking: str, spec: dict[str, Any], understood: str = "") -> dict[str, Any]:
    return {"spec": spec, "question": question, "done": False, "understood_ru": understood, "asking": asking}


def finish(spec: dict[str, Any], understood: str = "") -> dict[str, Any]:
    return {"spec": spec, "question": None, "done": True, "understood_ru": understood, "asking": None}


def with_fake(fake: FakeInterviewer, transcriber: FakeTranscriber | None = None):  # type: ignore[no-untyped-def]
    control, sink, outbox = plane(transcriber)
    control.intake.interviewer = fake
    return control, sink, outbox


EVERYTHING = {"place": MADRID, "deal": "rent", "property_type": "apartment", "budget": {"max": 1200, "currency": "EUR"},
              "rooms": {"min": 2}, "wishes": [{"text": "рядом с метро", "weight": 3}], "must_have": ["лифт"]}


# --- TaskSpec ------------------------------------------------------------------------------------


def test_missing_hard_follows_the_order_per_mode_and_unspecified_counts_as_answered() -> None:
    spec = TaskSpec(mode="real_estate")
    assert spec.missing_hard() == ["place", "deal", "property_type", "budget.max"]  # no rooms until the type needs them
    spec = spec.merged({"place": {"name": "Valencia"}, "deal": "any", "property_type": "house"})
    assert spec.missing_hard() == ["budget.max", "rooms.min"]
    spec = spec.mark_unspecified("budget").mark_unspecified("rooms.min")  # a parent path covers its children
    assert spec.missing_hard() == [] and spec.is_unspecified("budget.max")
    assert TaskSpec(mode="real_estate", property_type="land").merged({}).missing_hard() == [
        "place", "deal", "budget.max"]  # a plot has no rooms

    investors = TaskSpec(mode="investors")
    assert investors.missing_hard() == ["place", "investor.who", "investor.ticket", "investor.user_role"]
    investors = investors.merged({"investor": {"geography": ["Spain"], "who": ["fund"], "user_role": "raising"}})
    assert investors.missing_hard() == ["investor.ticket"]  # the geography stands in for the place
    assert investors.mark_unspecified("investor.ticket").missing_hard() == []
    # The place can never be waved away.
    assert "place" in TaskSpec(mode="real_estate").mark_unspecified("place").missing_hard()


def test_merge_keeps_what_is_known_ignores_drift_and_never_changes_the_mode() -> None:
    base = TaskSpec(mode="real_estate").merged({"place": MADRID, "deal": "rent", "budget": {"max": 900}})
    merged = base.merged({
        "mode": "investors", "budget": {"min": "500", "currency": "eur"}, "deal": None, "wishes": ["балкон", "тихо"],
        "property_type": "castle",  # not allowed: this key is skipped, the rest still applies
        "rooms": {"min": "abc"}, "place": {"districts": ["Руссафа"]}, "bogus": 1,
        "unspecified": ["area_m2.min"],
    })
    assert merged.mode == "real_estate" and merged.deal == "rent" and merged.property_type is None
    assert (merged.budget.min, merged.budget.max, merged.budget.currency) == (500, 900, "EUR")
    assert merged.place.name == "Madrid" and merged.place.districts == ["Руссафа"] and merged.place.names["ru"] == "Мадрид"
    assert [w.text for w in merged.wishes] == ["балкон", "тихо"] and merged.wishes[0].weight == 2
    assert merged.unspecified == ["area_m2.min"]
    # A field that gets a value leaves «unspecified»; the spec round-trips through JSON.
    filled = merged.mark_unspecified("rooms.min").merged({"rooms": {"min": 2}})
    assert filled.unspecified == ["area_m2.min"] and TaskSpec.model_validate_json(filled.model_dump_json()) == filled


def test_the_card_is_structured_russian() -> None:
    spec = TaskSpec(mode="real_estate").merged({**EVERYTHING, "exclude": ["первый этаж"], "area_m2": {"min": 50},
                                                "sources": {"required": ["idealista"], "blocked": ["olx"]},
                                                "delivery": {"max_results": 10, "show_similar": True},
                                                "place": {**MADRID, "districts": ["Чамберь"]}})
    assert spec.summary_ru().splitlines() == [
        "Режим: 🏡 Участки и объекты", "Город: Мадрид", "Районы: Чамберь", "Сделка: аренда", "Тип: квартира",
        "Бюджет: до 1200 €", "Комнаты: от 2", "Площадь: от 50 м²", "Обязательно: лифт",
        "Пожелания: рядом с метро (очень важно)", "Исключить: первый этаж",
        "Источники: обязательно idealista; не использовать olx", "Выдача: до 10 вариантов, показывать похожие"]
    investors = TaskSpec(mode="investors").merged({
        "place": MADRID, "investor": {"who": ["fund", "family_office"], "ticket": {"min": 100000, "max": 1000000, "currency": "EUR"},
                                      "user_role": "raising", "geography": ["Spain"]}})
    assert "Тикет: от 100000 до 1000000 €" in investors.summary_ru() and "Кого ищем: фонды, семейные офисы" in investors.summary_ru()


def test_the_planner_takes_its_constraints_from_the_spec_not_from_the_text() -> None:
    spec = TaskSpec(mode="real_estate").merged({
        "place": {"name": "Ubud, Bali", "country": "id", "names": {"ru": "Убуд, Бали", "ru_in": "Убуде"}},
        "deal": "sale", "property_type": "land", "budget": {"min": 50000, "max": 120000}, "area_m2": {"min": 1000, "max": 5000},
        "rooms": {"min": 3}, "place_extra": 1})
    spec = spec.merged({"place": {"districts": ["Чангу", "Санур"]}})
    plan = plan_campaign("что угодно, до 5 комнат и 10 € в месяц, аренда", vertical="real_estate", spec=spec)
    assert (plan.location, plan.country, plan.location_aliases["ru"]) == ("Ubud, Bali", "ID", "Убуд, Бали")
    assert plan.constraints == {"deal": "sale", "max_price": 120000, "rooms": 3, "min_price": 50000, "min_area": 1000,
                                "max_area": 5000, "property_type": "land", "districts": "Чангу, Санур"}
    assert CampaignPlan.model_validate_json(plan.model_dump_json()) == plan
    # Without a spec nothing changes; an explicit location still wins over the spec's place.
    assert plan_campaign("аренда квартир в Мадриде до 900 €").constraints == {"deal": "rent", "max_price": 900, "rooms": None}
    assert plan_campaign("x", vertical="real_estate", location="Lisbon", spec=spec).location == "Lisbon"
    # 'any' means no deal constraint; investors carry no real estate constraints.
    assert plan_campaign("x", vertical="real_estate", spec=spec.merged({"deal": "any"})).constraints["deal"] is None
    assert plan_campaign("x", vertical="investors", spec=spec).constraints == {"deal": None, "max_price": None, "rooms": None}
    with pytest.raises(ValueError):
        CampaignPlan.model_validate({**plan.model_dump(), "constraints": {"property_type": "castle"}})


# --- the intake with a scripted model --------------------------------------------------------------


def ask_with(question: str, asking: str, spec: dict[str, Any], options: list[str], understood: str = "") -> dict[str, Any]:
    return {**ask(question, asking, spec, understood), "options": options}


FLOOR_Q = "Какой этаж и нужен ли лифт? Например: «не ниже 3 этажа, лифт обязателен», «любой»."
DEVIATION_Q = "Если точных вариантов не будет, что допустимо? Например: бюджет до +10 % (до 1320 €), соседние районы."
DEVIATION_OPTIONS = ["бюджет до +10 % (до 1320 €)", "соседние районы: Чамартин", "нет, только точные"]


@pytest.mark.asyncio
async def test_a_task_that_says_everything_is_still_asked_task_questions_and_the_deviation_before_the_card() -> None:
    fake = FakeInterviewer(
        ask(FLOOR_Q, "context.answers", EVERYTHING, "Квартира в аренду в Мадриде, до 1200 €, от 2 комнат"),
        ask_with(DEVIATION_Q, "deviations", {"context": {"answers": [{"question": FLOOR_Q, "answer": "не ниже 3 этажа"}]}},
                 DEVIATION_OPTIONS, "Не ниже 3 этажа"),
        finish({"deviations": {"budget_pct": 10, "nearby_areas": ["Чамартин"], "asked": True}}, "Допустимо: бюджет +10 %"),
    )
    control, sink = (await _mode(fake))
    first = await say(control, USER, "снять квартиру от двух комнат в Мадриде рядом с метро, лифт обязательно, до 1200 евро")
    assert first.text.endswith(FLOOR_Q) and first.keyboard == DRAFT_KEYBOARD and "Проверьте" not in first.text
    assert fake.calls[0]["dialogue"] == [] and fake.calls[0]["asking"] is None and fake.calls[0]["editing"] is False
    second = await say(control, USER, "не ниже 3 этажа")
    assert second.text.endswith(DEVIATION_Q) and fake.calls[1]["asking"] == "context.answers"
    # The model's options are inline buttons (up to 4) next to the usual ones; free text stays possible.
    assert callbacks(second) == ["task:opt:0", "task:opt:1", "task:opt:2", "task:skip", "task:enough", "task:cancel"]
    assert [b.text for b in second.buttons[:3]] == DEVIATION_OPTIONS
    card = await press(control, USER, "task:opt:0")
    assert fake.calls[2]["message"] == DEVIATION_OPTIONS[0] and fake.calls[2]["asking"] == "deviations"
    assert card.keyboard == TASK_KEYBOARD and not card.buttons
    assert card.text.startswith("Проверьте задачу:\nРежим: 🏡 Участки и объекты\nГород: Мадрид\n")
    for line in ("Сделка: аренда", "Тип: квартира", "Бюджет: до 1200 €", "Комнаты: от 2", "Обязательно: лифт",
                 "Пожелания: рядом с метро (очень важно)",
                 "Допустимые отступления: бюджет ±10 %; соседние районы: Чамартин",
                 "• Какой этаж и нужен ли лифт? — не ниже 3 этажа"):
        assert line in card.text, line
    assert "?" not in card.text.replace("Всё верно?", "").replace("Какой этаж и нужен ли лифт?", "")
    assert sink.envelopes == []
    # Launch: the requirements travel with the queued command and land on the campaign.
    await press(control, USER, "task:launch")
    [envelope] = sink.envelopes
    assert envelope.arguments.startswith("mode=real_estate city=Madrid spec=")
    spec = TaskSpec.model_validate(parse_campaign_spec(envelope.arguments))
    assert spec.rooms.min == 2 and spec.must_have == ["лифт"] and spec.place.names["ru"] == "Мадрид"
    assert spec.deviations.budget_pct == 10 and spec.deviations.nearby_areas == ["Чамартин"] and spec.deviations.asked
    assert [(a.question, a.answer) for a in spec.context.answers] == [(FLOOR_Q, "не ниже 3 этажа")]
    assert parse_campaign_goal(envelope.arguments)[:3] == (
        "аренда до 1200 € Тип: квартира. Задача: снять квартиру от двух комнат в Мадриде рядом с метро, "
        "лифт обязательно, до 1200 евро Главное: лифт. Дополнительно: рядом с метро.", "real_estate", "Madrid")
    _, campaigns, _ = await dispatch(claimed("campaign", envelope.arguments))
    [campaign] = campaigns.campaigns.values()
    assert campaign.spec == spec.model_dump(mode="json")
    assert campaign.plan.constraints["rooms"] == 2 and campaign.plan.constraints["property_type"] == "apartment"


async def _mode(fake: FakeInterviewer, mode: str = "real_estate", user: int = USER):  # type: ignore[no-untyped-def]
    control, sink, _ = with_fake(fake)
    await press(control, user, f"mode:{mode}")
    return control, sink


@pytest.mark.asyncio
async def test_only_a_city_is_followed_by_questions_in_order_until_done() -> None:
    fake = FakeInterviewer(
        ask("Аренда или покупка? Например: аренда, покупка.", "deal", {"place": MADRID}, "Мадрид"),
        ask("Что ищете? Например: квартира, дом, участок.", "property_type", {"deal": "rent"}, "Аренда"),
        ask("Какой бюджет? Например: до 1200 €, 800–1000 €.", "budget.max", {"property_type": "apartment"}, "Квартира"),
        ask("Сколько комнат? Например: студия, 2, 2–3.", "rooms.min", {"budget": {"max": 1200}}, "До 1200 €"),
        finish({"rooms": {"min": 2}}, "От 2 комнат"),  # done, but the deviation question is still asked
        finish({"deviations": {"asked": True}}),
    )
    control, _ = await _mode(fake)
    replies = [await say(control, USER, text) for text in ("Мадрид", "аренда", "квартира", "до 1200", "две", "только точные")]
    assert [r.text.split("\n\n")[-1] for r in replies[:4]] == [
        "Аренда или покупка? Например: аренда, покупка.", "Что ищете? Например: квартира, дом, участок.",
        "Какой бюджет? Например: до 1200 €, 800–1000 €.", "Сколько комнат? Например: студия, 2, 2–3."]
    assert replies[0].text.startswith("Понял: Мадрид\n\n") and replies[3].text.startswith("Понял: До 1200 €\n\n")
    assert all(callbacks(r)[-3:] == ["task:skip", "task:enough", "task:cancel"] for r in replies[:4])
    assert "Если точных вариантов не найду" in replies[4].text and callbacks(replies[4])[:3] == [
        "task:dev:10", "task:dev:20", "task:dev:0"]  # the code never lets the card appear before the deviation question
    assert replies[5].text.startswith("Проверьте задачу:") and replies[5].keyboard == TASK_KEYBOARD
    assert "Комнаты: от 2" in replies[5].text and "Бюджет: до 1200 €" in replies[5].text
    assert "Допустимые отступления: нет, только точные" not in replies[5].text  # the model set asked without a value
    # The model saw what it asked: the asked field, the growing spec and the dialogue (user and assistant turns).
    assert [c["asking"] for c in fake.calls] == [None, "deal", "property_type", "budget.max", "rooms.min", "deviations"]
    assert fake.calls[2]["spec"].deal == "rent" and fake.calls[2]["spec"].place.name == "Madrid"
    assert [t["role"] for t in fake.calls[4]["dialogue"]] == ["user", "assistant"] * 4
    assert fake.calls[4]["dialogue"][-1] == {"role": "assistant", "text": "Сколько комнат? Например: студия, 2, 2–3."}
    assert fake.calls[4]["message"] == "две"


@pytest.mark.asyncio
async def test_not_important_moves_the_asked_field_to_unspecified_and_the_interview_goes_on() -> None:
    fake = FakeInterviewer(
        ask("Какой бюджет? Например: до 1200 €.", "budget.max",
            {"place": MADRID, "deal": "rent", "property_type": "apartment"}),
        ask("Сколько комнат? Например: студия, 2.", "rooms.min", {}, "Бюджет не важен"),  # the model forgot to mark it
        finish({"unspecified": ["rooms.min"]}),
    )
    control, _ = await _mode(fake)
    budget = await say(control, USER, "квартиру в аренду в Мадриде")
    assert "task:skip" in callbacks(budget)
    rooms = await press(control, USER, "task:skip")
    assert rooms.text.startswith("Понял: Бюджет не важен\n\nСколько комнат?")
    call = fake.calls[1]
    assert call["message"] == "Не важно" and call["asking"] == "budget.max"
    assert call["spec"].unspecified == ["budget.max"]  # marked by the bot, not by the model
    deviation = await press(control, USER, "task:skip")  # the model says done: the deviation question comes first
    assert "Если точных вариантов не найду" in deviation.text
    card = await press(control, USER, "task:skip")
    assert "Бюджет: не важно" in card.text and "Комнаты: не важно" in card.text
    # A typed «не важно» does the same as the button; the place has no such button and cannot be skipped.
    fake2 = FakeInterviewer(ask("Где искать?", "place", {"deal": "rent"}), ask("Какой бюджет?", "budget.max", {}),
                            finish({}))
    control, _ = await _mode(fake2)
    where = await say(control, USER, "хочу снять")
    assert "task:skip" not in callbacks(where)
    assert (await say(control, USER, "не важно")).text == "Какой бюджет?"  # the place stays open: the model decides
    assert fake2.calls[1]["spec"].unspecified == []


@pytest.mark.asyncio
async def test_enough_ends_the_interview_but_a_missing_place_is_still_asked() -> None:
    fake = FakeInterviewer(ask("Какой бюджет?", "budget.max", {"place": MADRID, "deal": "sale"}))
    control, _ = await _mode(fake)
    await say(control, USER, "купить в Мадриде")
    card = await enough(control, USER)
    assert len(fake.calls) == 1  # «Хватит, ищи» needs no model call
    assert card.text.startswith("Проверьте задачу:") and "Сделка: покупка" in card.text
    assert "Бюджет: не указан" in card.text and "Тип: не указан" in card.text and card.keyboard == TASK_KEYBOARD
    # Typed «хватит, ищи» works too; without a place it asks for the place first.
    fake = FakeInterviewer(ask("Какой бюджет?", "budget.max", {"deal": "sale"}))
    control, _ = await _mode(fake)
    await say(control, USER, "купить")
    place = await say(control, USER, "Хватит, ищи")
    assert place.text.startswith("Место нужно в любом случае. В каком городе искать?")
    assert "task:skip" not in callbacks(place)
    fake.turns = [finish({"place": MADRID})]
    card = await say(control, USER, "Мадрид")
    assert card.text.startswith("Проверьте задачу:") and "Город: Мадрид" in card.text  # the stop stands: no more questions


@pytest.mark.asyncio
async def test_the_edit_button_changes_one_field_and_returns_to_the_card() -> None:
    fake = FakeInterviewer(finish(EVERYTHING), finish({"budget": {"max": 1500}}, "Бюджет до 1500 €"))
    control, sink = await _mode(fake)
    await say(control, USER, "снять квартиру в Мадриде")
    menu = await press(control, USER, "task:edit")
    assert menu.text == "Что изменить?" and callbacks(menu) == [
        "task:field:place", "task:field:deal", "task:field:type", "task:field:budget", "task:field:rooms",
        "task:field:area", "task:field:districts", "task:field:must_have", "task:field:wishes", "task:field:exclude", "task:field:sources", "task:back"]
    prompt = await press(control, USER, "task:field:budget")
    assert prompt.text.startswith("Бюджет: напишите новое значение.") and callbacks(prompt) == ["task:back"]
    card = await say(control, USER, "до 1500")
    call = fake.calls[1]
    assert (call["message"], call["editing"], call["asking"]) == ("бюджет: до 1500", True, "budget.max")
    assert call["spec"].budget.max == 1200  # the model gets the whole current spec, not a blank one
    for line in ("Город: Мадрид", "Сделка: аренда", "Тип: квартира", "Бюджет: до 1500 €", "Комнаты: от 2",
                 "Обязательно: лифт", "Пожелания: рядом с метро (очень важно)"):
        assert line in card.text, line
    assert card.keyboard == TASK_KEYBOARD and "Принято" in (await press(control, USER, "task:launch")).text
    assert len(sink.envelopes) == 1
    # «Назад» leaves the list without a model call; investors get their own fields.
    fake = FakeInterviewer(finish({"place": MADRID, "investor": {"who": ["fund"], "ticket": {"max": 1000000},
                                                                  "user_role": "deploying"}}))
    control, _ = await _mode(fake, "investors")
    await say(control, USER, "фонды в Мадриде")
    investor_menu = await press(control, USER, "task:edit")
    assert callbacks(investor_menu)[:4] == ["task:field:who", "task:field:ticket", "task:field:role", "task:field:geo"]
    assert "Режим: 💼" in (await press(control, USER, "task:back")).text and len(fake.calls) == 1
    assert (await press(control, USER, "task:field:nonsense")).text == "Эта кнопка устарела."


@pytest.mark.asyncio
async def test_investors_without_a_ticket_are_asked_for_it_even_if_the_model_says_done() -> None:
    forgetful = finish({"place": MADRID, "investor": {"who": ["private"], "user_role": "raising"}}, "Инвесторы в Мадриде")
    fake = FakeInterviewer(forgetful, finish({"investor": {"ticket": {"min": 50000, "currency": "EUR"}}}))
    control, _ = await _mode(fake, "investors")
    question = await say(control, USER, "ищу частных инвесторов в Мадриде для моего проекта")
    assert question.text == ("Понял: Инвесторы в Мадриде\n\nКакой размер вложения (тикет)? "
                             "Например: от 100 тыс. €, 500 тыс. – 2 млн €.")
    card = await say(control, USER, "от 50 тысяч")
    assert "Тикет: от 50000 €" in card.text and "Кого ищем: частные инвесторы" in card.text
    assert "Ваша роль: привлекаю деньги" in card.text


@pytest.mark.asyncio
async def test_the_round_cap_forces_the_card() -> None:
    always = FakeInterviewer(ask("Ещё что-нибудь?", "budget.max", {"place": MADRID}))
    store = MemoryIntakeStore()
    intake = TaskIntake(store, _Sink(), interviewer=always, max_rounds=3)
    await intake.choose_mode(USER, USER, "real_estate")
    message = IncomingMessage(USER, USER, 1, text="x")
    replies = [await intake.on_text(message, "ищу квартиру в Мадриде")]
    replies += [await intake.on_text(message, f"ответ {n}") for n in range(1, 4)]
    assert [r.keyboard for r in replies[:3]] == [DRAFT_KEYBOARD] * 3 and all("Ещё что-нибудь?" in r.text for r in replies[:3])
    assert replies[3].text.startswith("Проверьте задачу:") and replies[3].keyboard == TASK_KEYBOARD  # the 4th turn is cut
    draft = await store.get(USER)
    assert draft is not None and (draft.rounds, draft.step) == (3, "summary")
    assert len(always.calls) == 4
    # The default is fourteen.
    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x")
    assert settings.interview_max_rounds == 14
    assert ControlPlane(settings, _store(), None, _Sink()).intake.max_rounds == 14


def _store():  # type: ignore[no-untyped-def]
    from bot.control_plane.store import MemoryControlPlaneStore

    return MemoryControlPlaneStore()


class _Sink:
    def __init__(self) -> None:
        self.envelopes: list[Any] = []

    async def __call__(self, envelope: Any) -> object:
        self.envelopes.append(envelope)
        return object()


@pytest.mark.asyncio
async def test_a_model_failure_hands_the_turn_to_the_ordered_rules(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeInterviewer(InterviewError("timeout"))
    control, _ = await _mode(fake)
    with caplog.at_level(logging.WARNING):
        place = await say(control, USER, "хочу снять квартиру до 900 евро")
    assert "telegram.intake.interview_failed" in caplog.text
    assert place.text.startswith("Понял: аренда, квартира, до 900 €\n\nВ каком городе искать?")
    fake.turns = [ValueError("bad json")]
    rooms = await say(control, USER, "Малага")
    assert rooms.text.startswith("Понял: Малага\n\nСколько комнат")
    # The model is tried on every message: when it is back, it interviews again.
    fake.turns = [finish({"rooms": {"min": 1}})]
    assert "Если точных вариантов" in (await say(control, USER, "студия")).text
    assert (await say(control, USER, "только точные")).text.startswith("Проверьте задачу:")


@pytest.mark.asyncio
async def test_the_rules_ask_the_same_ordered_fields_one_at_a_time() -> None:
    control, _, _ = plane()
    await press(control, USER, "mode:real_estate")
    assert (await say(control, USER, "жильё")).text.endswith("В каком городе искать? Напишите город, район или регион в любой стране.")
    steps = [("Валенсия", "Аренда или покупка?"), ("аренда", "Что ищете:"), ("дом", "Какой бюджет?"),
             ("до 2000 €", "Сколько комнат нужно?"), ("3", "Район или вся Валенсия?"), ("Руссафа", "Что обязательно должно быть?")]
    for answer, next_question in steps:
        assert next_question in (await say(control, USER, answer)).text, answer
    assert "На какой срок?" in (await say(control, USER, "бассейн, гараж")).text  # one generic question for a rent
    deviation = await say(control, USER, "на год")
    assert "Если точных вариантов не найду" in deviation.text
    assert callbacks(deviation)[:3] == ["task:dev:10", "task:dev:20", "task:dev:0"]
    assert [b.text for b in deviation.buttons[:3]] == ["±10 %", "±20 %", "Только точные"]
    card = await say(control, USER, "±20 %")
    assert card.text.startswith("Проверьте задачу:")
    for line in ("Допустимые отступления: бюджет ±20 %; площадь −20 %", "Город: Валенсия", "Районы: Руссафа", "Тип: дом", "Бюджет: до 2000 €", "Комнаты: от 3",
                 "Обязательно: бассейн, гараж"):
        assert line in card.text, line

    # Investors: who, ticket, role, in this order (the place comes first).
    control, _, _ = plane()
    await press(control, USER, "mode:investors")
    for answer, next_question in (("нужны контакты", "В каком городе"), ("Малага", "Кого ищете?"),
                                  ("фонды", "размер вложения"), ("500 тыс - 2 млн €", "Вы ищете деньги")):
        assert next_question in (await say(control, USER, answer)).text, answer
    assert "Какие проекты и стадии" in (await say(control, USER, "хочу вкладывать")).text
    card = await say(control, USER, "стартапы на ранней стадии")
    assert "Тикет: от 500000 до 2000000 €" in card.text and "Ваша роль: вкладываю деньги" in card.text
    assert "• Какие проекты и стадии вам интересны?" in card.text and "Допустимые отступления" not in card.text
    # The rules never invent: nothing understood -> the same question again, with a hint.
    control, _, _ = plane()
    await press(control, USER, "mode:real_estate")
    await say(control, USER, "квартира в Мадриде")
    assert (await say(control, USER, "ммм")).text.startswith("Не понял ответ. Аренда или покупка?")


@pytest.mark.asyncio
async def test_a_voice_task_is_shown_back_before_the_first_question() -> None:
    fake = FakeInterviewer(ask("Аренда или покупка?", "deal", {"place": MADRID}, "Мадрид"))
    control, _, _ = with_fake(fake, FakeTranscriber("шукаю квартиру в Мадрид"))
    await press(control, USER, "mode:real_estate")

    async def audio() -> bytes:
        return b"OggS"

    reply = await control.handle_voice(IncomingMessage(USER, USER, 900, voice_file_id="v", voice_size=10,
                                                       voice_duration_seconds=3), audio)
    assert reply is not None
    assert reply.text == "Я услышал: «шукаю квартиру в Мадрид»\n\nПонял: Мадрид\n\nАренда или покупка?"
    typed = await say(control, USER, "аренда")  # typed text is not echoed
    assert "Я услышал" not in typed.text
    # The owner already sees the transcript block, so no second echo.
    await press(control, OWNER, "mode:real_estate")
    owner = await control.handle_voice(IncomingMessage(OWNER, OWNER, 901, voice_file_id="v", voice_size=10,
                                                       voice_duration_seconds=3), audio)
    assert owner is not None and owner.text.count("шукаю квартиру в Мадрид") == 1 and "Я услышал" not in owner.text


@pytest.mark.asyncio
async def test_a_correction_typed_under_the_card_keeps_the_task() -> None:
    fake = FakeInterviewer(finish(EVERYTHING), finish({"deal": "sale"}, "Покупка"))
    control, _ = await _mode(fake)
    await say(control, USER, "снять квартиру в Мадриде")
    card = await say(control, USER, "нет, лучше купить")
    assert fake.calls[1]["spec"].rooms.min == 2 and "Сделка: покупка" in card.text and "Город: Мадрид" in card.text
    assert fake.calls[1]["message"] == "нет, лучше купить"


# --- the draft and the queue token ------------------------------------------------------------------


def test_draft_payload_and_spec_round_trip_and_the_command_token_is_decodable() -> None:
    spec = TaskSpec(mode="investors").merged({"place": {"name": "Ubud, Bali", "country": "ID", "names": {"ru": "Убуд, Бали"}},
                                              "investor": {"who": ["fund"], "ticket": {"max": 5e6, "currency": "USD"}}})
    draft = Draft(USER, USER, "investors", "summary", task="фонды на Бали", message_id=5, spec=spec,
                  dialogue=[{"role": "user", "text": "фонды на Бали"}, {"role": "assistant", "text": "Тикет?"}],
                  rounds=2, asking="investor.ticket", editing=None, forced=True)
    loaded = Draft.load(USER, USER, "investors", "summary", json.loads(json.dumps(draft.payload())), None,
                        json.loads(draft.spec_json() or "null"))
    assert loaded == draft and loaded.copy().spec is not loaded.spec
    arguments = draft.command_arguments()
    assert arguments.startswith("mode=investors place=") and " spec=" in arguments and " " not in encode_spec(spec)
    assert TaskSpec.model_validate(parse_campaign_spec(arguments)) == spec
    assert parse_campaign_spec("квартиры в Мадриде") is None and parse_campaign_spec("mode=investors city=Madrid x") is None
    for bad in ("spec=%%% x", "spec=" + encode_spec(spec)[:-3] + "!!! x", "spec=" + "A" * 30_000 + " x"):
        with pytest.raises(CommandValidationError):
            parse_campaign_spec(bad)


@pytest.mark.asyncio
async def test_a_bad_spec_token_is_rejected_without_a_campaign() -> None:
    import base64

    def token(data: object) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()

    bad_type, not_object, nonsense = token({"mode": "real_estate", "deal": 5, "budget": {"max": "1 500"}}), token([1]), token({"budget": "lots"})
    for arguments in (f"mode=real_estate city=Madrid spec={not_object} квартиры",
                      f"mode=real_estate city=Madrid spec={nonsense} квартиры"):
        _unused, campaigns, notices = await dispatch(claimed("campaign", arguments))
        assert campaigns.campaigns == {} and "failed validation" in notices[-1]
    # A spec that is merely sloppy is tolerated and validated into a usable one.
    _unused, campaigns, _ = await dispatch(claimed("campaign", f"mode=real_estate city=Madrid spec={bad_type} квартиры"))
    [campaign] = campaigns.campaigns.values()
    assert campaign.spec is not None and campaign.spec["deal"] is None and campaign.spec["budget"]["max"] == 1500
    store = MemoryCampaignStore()
    cid = await store.create(plan_campaign("квартиры в Мадриде"), chat_id=1, requested_by=2, source_text="x", actor="t")
    assert (await store.get(cid)).spec is None  # type: ignore[union-attr]


# --- the OpenRouter client --------------------------------------------------------------------------


def _completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def _client(*responses: httpx.Response | Exception) -> tuple[OpenRouterInterviewer, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"body": json.loads(request.content), "auth": request.headers.get("authorization")})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenRouterInterviewer(api_key="sk-test-secret", model="anthropic/claude-sonnet-4.5", timeout_seconds=5,
                                 client=client), sent


@pytest.mark.asyncio
async def test_the_client_makes_one_call_with_state_and_parses_drifting_json(caplog: pytest.LogCaptureFixture) -> None:
    drifty = "```json\n" + json.dumps({
        "spec": {"place": {"name": "Valencia", "country": "es"}, "deal": "аренда", "budget": {"max": "900"},
                 "wishes": "балкон; тихо", "mode": "investors"},
        "question": "Сколько комнат? Например: 1, 2, 3.", "done": False, "understood_ru": "Валенсия, до 900 €",
        "asking": "rooms.min", "extra": 1}, ensure_ascii=False) + "\n```"
    client, sent = await _client(httpx.Response(400, json={"error": "no json mode"}), _completion(drifty))
    spec = TaskSpec(mode="real_estate")
    dialogue = [{"role": "user" if n % 2 == 0 else "assistant", "text": f"реплика {n}" + "я" * 700} for n in range(20)]
    with caplog.at_level(logging.DEBUG):
        turn = await client.interview(mode="real_estate", spec=spec, dialogue=dialogue, message="Валенсия", asking="place")
    assert len(sent) == 2 and sent[0]["body"]["response_format"] == {"type": "json_object"}
    assert "response_format" not in sent[1]["body"]  # a model without JSON mode: retried once without it
    assert sent[0]["body"]["model"] == "anthropic/claude-sonnet-4.5" and sent[0]["auth"] == "Bearer sk-test-secret"
    assert sent[0]["body"]["messages"][0] == {"role": "system", "content": SYSTEM}
    data = json.loads(sent[0]["body"]["messages"][1]["content"].split("\n", 1)[1])
    assert data["mode"] == "real_estate" and data["message"] == "Валенсия" and data["asking"] == "place"
    assert data["editing"] is False and "mode" not in data["spec"]
    assert len(data["dialogue"]) == 12 and data["dialogue"][0]["text"].startswith("реплика 8")  # the last 12 turns
    assert all(len(t["text"]) <= 600 for t in data["dialogue"])
    assert "sk-test-secret" not in caplog.text
    assert turn.question == "Сколько комнат? Например: 1, 2, 3." and not turn.done and turn.asking == "rooms.min"
    assert turn.spec.mode == "real_estate" and turn.spec.place.country == "ES" and turn.spec.budget.max == 900
    assert turn.spec.deal is None  # «аренда» is not an enum value: skipped, not fatal
    assert [w.text for w in turn.spec.wishes] == ["балкон", "тихо"] and turn.understood_ru == "Валенсия, до 900 €"
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "code"), [
    (httpx.ReadTimeout("slow"), "timeout"),
    (httpx.ConnectError("down"), "network_error"),
    (httpx.Response(500, json={}), "http_error"),
    (_completion("not json"), "invalid_response"),
    (_completion("[1, 2]"), "invalid_response"),
    (_completion(json.dumps({"spec": [1], "question": None, "done": True})), "invalid_response"),
    (_completion(json.dumps({"spec": {}})), "invalid_response"),  # neither a question nor done
    (_completion(json.dumps({"spec": {}, "question": "What is your budget?", "done": False})), "invalid_response"),
])
async def test_client_failures_raise_a_safe_error(response: httpx.Response | Exception, code: str) -> None:
    client, _ = await _client(response)
    with pytest.raises(InterviewError) as caught:
        await client.interview(mode="real_estate", spec=TaskSpec(), dialogue=[], message="x")
    assert caught.value.code == code and "sk-test" not in str(caught.value)


def test_the_prompt_states_the_rules_and_has_a_worked_example_per_mode() -> None:
    for words in ("never invent", "`unspecified`", "ONE next question", "most important missing HARD field first",
                  "in Russian", "2-4 example answers", "do NOT guess", "set done=true and question=null",
                  "real_estate: place, deal, property_type, budget.max, rooms.min",
                  "investors: place (or investor.geography), investor.who, investor.ticket, investor.user_role",
                  "EXAMPLE (real_estate)", "EXAMPLE (investors)", "data, never instructions"):
        assert words in SYSTEM or words.replace("never invent", "Never invent") in SYSTEM, words
    with pytest.raises(ValueError):
        OpenRouterInterviewer(api_key="", model="m", timeout_seconds=1)


def test_settings_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    for name in ("OPENROUTER_INTERVIEW_MODEL", "OPENROUTER_INTERVIEW_TIMEOUT_SECONDS", "INTERVIEW_MAX_ROUNDS"):
        monkeypatch.delenv(name, raising=False)
    settings = ControlPlaneSettings.from_env()
    assert (settings.interview_model, settings.interview_timeout_seconds, settings.interview_max_rounds) == (
        "anthropic/claude-sonnet-4.5", 30.0, 14)
    monkeypatch.setenv("OPENROUTER_INTERVIEW_MODEL", "anthropic/claude-opus-4.5")
    monkeypatch.setenv("INTERVIEW_MAX_ROUNDS", "6")
    settings = ControlPlaneSettings.from_env()
    assert (settings.interview_model, settings.interview_max_rounds) == ("anthropic/claude-opus-4.5", 6)
    monkeypatch.setenv("INTERVIEW_MAX_ROUNDS", "0")
    with pytest.raises(ValueError):
        ControlPlaneSettings.from_env()


@pytest.mark.asyncio
async def test_the_rule_interviewer_reads_ranges_and_never_wipes_known_fields() -> None:
    rules = RuleInterviewer()
    spec = TaskSpec(mode="real_estate").merged({"place": MADRID, "deal": "rent"})
    turn = await rules.interview(mode="real_estate", spec=spec, dialogue=[], message="от 500 до 900 евро", asking="budget.max")
    assert (turn.spec.budget.min, turn.spec.budget.max, turn.spec.budget.currency) == (500, 900, "EUR")
    assert turn.spec.deal == "rent" and turn.spec.place.name == "Madrid" and turn.asking == "property_type"
    turn = await rules.interview(mode="real_estate", spec=turn.spec, dialogue=[], message="студия", asking="rooms.min")
    assert turn.spec.rooms.min == 1 and turn.spec.budget.max == 900
    edited = await rules.interview(mode="real_estate", spec=turn.spec, dialogue=[], message="бюджет: до 1500", asking="budget.max",
                                   editing=True)
    assert edited.spec.budget.max == 1500 and edited.spec.budget.min == 500


@pytest.mark.asyncio
async def test_no_to_a_confirmation_question_reaches_the_ai_interviewer_and_is_not_a_skip() -> None:
    fake = FakeInterviewer(
        ask("Правильно ли я понял: Мадрид, аренда? Например: да, нет.", "deal",
            {"place": MADRID, "deal": "rent", "property_type": "apartment"}),
        ask("Тогда покупка? Например: покупка.", "deal", {}),
    )
    control, _ = await _mode(fake)
    await say(control, USER, "квартиру в Мадриде")
    await say(control, USER, "нет")
    assert [c["message"] for c in fake.calls] == ["квартиру в Мадриде", "нет"]
    assert fake.calls[1]["spec"].unspecified == [] and fake.calls[1]["asking"] == "deal"
    await say(control, USER, "-")
    assert fake.calls[2]["message"] == "-" and fake.calls[2]["spec"].unspecified == []
    # An explicit skip word still skips the asked field.
    await say(control, USER, "без разницы")
    assert fake.calls[3]["message"] == "Не важно" and fake.calls[3]["spec"].unspecified == ["deal"]


def test_merge_never_wipes_a_set_field_with_junk() -> None:
    base = TaskSpec(mode="real_estate").merged({"place": MADRID, "deal": "rent", "property_type": "apartment",
                                                "budget": {"max": 900, "currency": "EUR"}, "rooms": {"min": 2}})
    junk = base.merged({"deal": "whatever", "budget": {"max": "abc"}, "rooms": {"min": -3}, "property_type": "",
                        "place": {"name": None, "country": "zzz"}})
    assert (junk.deal, junk.property_type, junk.budget.max, junk.rooms.min) == ("rent", "apartment", 900, 2)
    assert junk.place.name == "Madrid"
    assert base.merged({"budget": {"max": -5}}).budget.max == 900
    assert base.merged({"budget": {"max": 700}}).budget.max == 700  # a real value still overrides


@pytest.mark.parametrize(("text", "number"), [
    ("1,200", 1200), ("1.200", 1200), ("1,200,000", 1_200_000), ("1.200.000", 1_200_000), ("1,5 млн", 1_500_000),
    ("200k", 200_000), ("200 тыс", 200_000), ("200 тыс.", 200_000), ("1.5", 1.5), ("1,5", 1.5), ("1 200", 1200),
    ("до 1200 €", 1200), ("1.5m", 1_500_000), ("2 комнаты", 2), ("1,234.5", 1234.5), ("abc", None)])
def test_number_parsing(text: str, number: float | None) -> None:
    assert parse_number(text) == number
    assert TaskSpec().merged({"budget": {"max": text}}).budget.max == number


def test_inverted_ranges_drop_or_swap() -> None:
    spec = TaskSpec().merged({"budget": {"min": 900, "max": 500}, "area_m2": {"min": 90, "max": 50}})
    assert (spec.budget.min, spec.budget.max) == (None, 500) and (spec.area_m2.min, spec.area_m2.max) == (None, 50)
    rooms = TaskSpec().merged({"rooms": {"min": 3, "max": 2}}).rooms
    assert isinstance(rooms, Rooms) and (rooms.min, rooms.max) == (2, 3)
    assert TaskSpec().merged({"budget": {"min": 100, "max": 500}}).budget.min == 100


def test_unspecified_and_asking_are_whitelisted_to_leaf_field_paths() -> None:
    spec = TaskSpec(mode="investors").merged({"unspecified": ["investor", "budget", "place", "bogus", "investor.who"]})
    assert spec.unspecified == ["investor.who"]
    assert "place" in spec.missing_hard() and "investor.ticket" in spec.missing_hard()
    spec = TaskSpec(mode="investors").merged({"unspecified": ["investor"]})
    assert spec.missing_hard() == ["place", "investor.who", "investor.ticket", "investor.user_role"]
    assert TaskSpec(unspecified=["rooms", "rooms.min"]).unspecified == ["rooms.min"]
    turn = parse_turn(json.dumps(ask("Какой бюджет?", "everything", {})), TaskSpec())
    assert turn.asking is None
    assert parse_turn(json.dumps(ask("Какой бюджет?", "budget.max", {})), TaskSpec()).asking == "budget.max"
    assert parse_turn(json.dumps(ask("Где искать?", "place", {})), TaskSpec()).asking == "place"


@pytest.mark.asyncio
async def test_a_corrupted_spec_row_loads_as_a_fresh_draft(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        draft = Draft.load(USER, 1, "real_estate", "ask", {"task": "x", "rounds": 3},
                           spec={"place": {"radius_km": "very far", "name": "Madrid"}, "budget": {"max": [1]}, "wishes": [{"weight": {}}], "must_have": 5, "deal": "rent", "rooms": 3, "sources": "x"})
    assert draft.step == "idle" and draft.spec is None and draft.task == "" and draft.mode == "real_estate"
    assert "draft_spec_corrupt" in caplog.text


# --- the three parts: hard fields, a task-specific round, the deviation question --------------------------------------


def test_the_prompt_describes_the_three_parts_and_the_examples_per_kind() -> None:
    from bot.control_plane.interviewer import PROMPT_VERSION

    assert PROMPT_VERSION == "interview-v2"
    for words in ("THREE PARTS", "TASK-SPECIFIC ROUND", "ONE DEVIATION QUESTION", "Если точных вариантов не будет, что допустимо?",
                  "buildable classification", "stage of the project", "equity or debt", "language of the listings",
                  "spec.deviations", "context.answers", "бюджет до +10 % (до 220 000 €)", "нет, только точные"):
        assert words in SYSTEM or words.replace("бюджет до +10 % (до 220 000 €)", "бюджет до +10 %") in SYSTEM, words


def test_deviations_and_context_are_parsed_into_the_spec_and_context_is_appended() -> None:
    base = TaskSpec(mode="real_estate").merged({"place": MADRID, "context": {"answers": [{"question": "Q1?", "answer": "A1"}]}})
    turn = parse_turn(json.dumps({
        "spec": {"deviations": {"budget_pct": "10 %", "area_pct": 15, "nearby_areas": ["Eixample", "Quatre Carreres"],
                                "other": ["без лифта ок до 2 этажа"], "asked": True},
                 "context": {"answers": [{"question": "Q2?", "answer": "A2"}]}},
        "question": None, "done": True}), base)
    dev = turn.spec.deviations
    assert (dev.budget_pct, dev.area_pct, dev.nearby_areas, dev.other, dev.asked) == (
        10, 15, ["Eixample", "Quatre Carreres"], ["без лифта ок до 2 этажа"], True)
    assert [(a.question, a.answer) for a in turn.spec.context.answers] == [("Q1?", "A1"), ("Q2?", "A2")]  # appended
    assert turn.spec.summary_ru().count("Допустимые отступления: бюджет ±10 %; площадь −15 %; соседние районы: Eixample") == 1
    # «Только точные» is asked-with-zeros; a later message never un-asks the question.
    strict = turn.spec.merged({"deviations": {"budget_pct": 0, "area_pct": 0, "asked": False}})
    assert strict.deviations.asked and strict.deviations.budget_pct == 0
    # Out-of-range numbers are dropped; the context is bounded (20 pairs, 400 characters each).
    junk = TaskSpec().merged({"deviations": {"budget_pct": 400, "rooms_delta": 99}})
    assert junk.deviations.budget_pct is None and junk.deviations.rooms_delta is None and not junk.deviations.asked
    many = TaskSpec().merged({"context": {"answers": [{"question": f"Q{n}", "answer": "я" * 900} for n in range(30)]}})
    assert len(many.context.answers) == 20 and len(many.context.answers[0].answer) == 400
    # The model's suggested answers: at most four.
    options = parse_turn(json.dumps({"spec": {}, "question": "Вопрос?", "done": False,
                                     "options": ["a1", "a2", "a3", "a4", "a5"]}), base)
    assert options.choices == ("a1", "a2", "a3", "a4")


@pytest.mark.asyncio
async def test_enough_skips_the_task_round_and_the_deviation_question() -> None:
    fake = FakeInterviewer(ask(FLOOR_Q, "context.answers", EVERYTHING))
    control, _ = await _mode(fake)
    first = await say(control, USER, "снять квартиру от двух комнат в Мадриде до 1200 евро")
    assert "task:enough" in callbacks(first)
    card = await press(control, USER, "task:enough")  # «Хватит, ищи»: the card at once, nothing more is asked
    assert card.text.startswith("Проверьте задачу:") and "Допустимые отступления" not in card.text
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_investors_get_investor_questions_and_their_answers_travel_with_the_spec() -> None:
    stage_q = "На какой стадии проект и какую сумму нужно привлечь? Например: идея, 300 тыс. €."
    split_q = "Доля или долг, и что получит инвестор? Например: 20 % компании, заём под 8 %."
    investors = {"place": MADRID, "investor": {"who": ["private", "fund"], "ticket": {"min": 100000, "max": 500000},
                                              "user_role": "raising", "asset_class": ["real_estate"]}}
    fake = FakeInterviewer(
        ask(stage_q, "context.answers", investors, "Ищете инвесторов в Мадриде"),
        ask(split_q, "context.answers", {}, "Идея, 300 тыс. €"),  # the model forgot the pair: the code keeps it verbatim
        ask_with("Если подходящих инвесторов не найдётся, что допустимо? Например: соседние страны, другой тикет.",
                 "deviations", {}, ["тикет вдвое меньше", "нет, только точные"]),
        finish({"deviations": {"other": ["тикет вдвое меньше"], "asked": True}}),
    )
    control, sink = await _mode(fake, "investors")
    first = await say(control, USER, "ищу инвесторов для апарт-отеля в Мадриде, 100-500 тыс")
    assert first.text.endswith(stage_q) and "Проверьте" not in first.text
    assert (await say(control, USER, "идея, нужно 300 тыс. €")).text.endswith(split_q)
    assert "Если подходящих инвесторов" in (await say(control, USER, "20 % компании")).text
    card = await press(control, USER, "task:opt:0")
    assert card.text.startswith("Проверьте задачу:") and "Допустимые отступления: тикет вдвое меньше" in card.text
    assert f"• {stage_q.split(' Например')[0]} — идея, нужно 300 тыс. €" in card.text
    await press(control, USER, "task:launch")
    spec = TaskSpec.model_validate(parse_campaign_spec(sink.envelopes[0].arguments))
    assert [a.answer for a in spec.context.answers] == ["идея, нужно 300 тыс. €", "20 % компании"]
    assert spec.context.answers[0].question == stage_q


@pytest.mark.asyncio
async def test_the_rules_fallback_asks_the_deviation_question_and_reads_the_buttons() -> None:
    control, _, _ = plane()
    await press(control, USER, "mode:real_estate")
    await say(control, USER, "купить квартиру в Валенсии до 200000 €, от 2 комнат")
    await press(control, USER, "task:skip")  # districts
    await press(control, USER, "task:skip")  # must-haves
    generic = await say(control, USER, "ммм")  # a generic question for a flat purchase, answered loosely
    generic = await press(control, USER, "task:skip") if "Этаж" in generic.text else generic
    assert "Если точных вариантов не найду" in generic.text and "бюджет +10 % (до 220 000 €)" in generic.text
    assert [b.text for b in generic.buttons[:3]] == ["±10 %", "±20 %", "Только точные"]
    card = await press(control, USER, "task:dev:10")
    assert "Допустимые отступления: бюджет ±10 %; площадь −10 %" in card.text
    # «Только точные» sets zeros: the card says so and nothing is approved.
    control, _, _ = plane()
    await press(control, USER, "mode:real_estate")
    await say(control, USER, "снять квартиру в Мадриде до 1200 € от 2 комнат")
    for _ in range(3):
        await press(control, USER, "task:skip")
    strict = await press(control, USER, "task:dev:0")
    assert "Допустимые отступления: нет, только точные" in strict.text
