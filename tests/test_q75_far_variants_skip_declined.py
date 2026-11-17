"""Q75: a compromise the deviation question offered as a button and the person did not take is never offered again
among the farther variants (FAR_VARIANTS_SKIP_DECLINED, off by default). Sim b7-g5-s8: «от 1 спальни», buttons
«бюджет до +10 % (до 1650 €)», «студия вместо 1 спальни», «нет, только точные»; the answer «можно ±10 % по бюджету и
площади»; after «Показать более далёкие варианты?» — «Одобрить» three studios were sent. Mocks only, no network.
"""

from __future__ import annotations

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.spec import TaskSpec
from bot.campaign.tolerance import classify, far_skip_declined_enabled, request_for
from bot.control_plane.intake import TaskIntake
from bot.control_plane.rules import declined_deviations, deviation_kinds
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore
from bot.orchestra.parser import parse_campaign_spec
from tests.test_interviewer import FakeInterviewer, _mode, ask, ask_with, finish
from tests.test_near_match import CHAT, OWNER, USER, ButtonMessenger, _sink
from tests.test_user_intake import USER as INTAKE_USER
from tests.test_user_intake import press, say

DEVIATION_Q = ("Если точных вариантов не будет, что допустимо? Например: бюджет до +10 % (до 1650 €), студия вместо 1 "
               "спальни, или только точные.")
OPTIONS = ["бюджет до +10 % (до 1650 €)", "студия вместо 1 спальни", "нет, только точные"]
ANSWER = "можно ±10 % по бюджету и площади"
TERM_Q = "На какой срок нужна аренда? Например: на год, от 6 месяцев, долгосрочно."
BARCELONA = {"name": "Barcelona", "country": "ES", "level": "city",
             "names": {"ru": "Барселона", "es": "Barcelona", "uk": "Барселона", "ru_in": "Барселоне",
                       "uk_in": "Барселоні"}}
SPEC = {"place": BARCELONA, "deal": "rent", "property_type": "apartment",
        "budget": {"max": 1500, "currency": "EUR"}, "rooms": {"min": 1}}
GOAL = "аренда до 1500 € Тип: квартира. Задача: снять piso в Барселоне до 1500 в месяц"

FLAT = {"deal_type": "rent", "property_type": "apartment", "rooms": 2, "price_amount": 1250, "price_currency": "EUR",
        "location": "Gràcia, Barcelona", "area_m2": 53.0, "summary": "Piso de dos habitaciones en Gràcia.",
        "summary_ru": "Квартира с двумя спальнями в Грасии."}
STUDIO = {"deal_type": "rent", "property_type": "studio", "rooms": None, "price_amount": 1100, "price_currency": "EUR",
          "location": "Eixample, Barcelona", "area_m2": 30.0, "summary": "Estudio en el Eixample.",
          "summary_ru": "Студия в Эшампле."}


def g5(deviations: dict) -> dict:
    return {**SPEC, "deviations": {"asked": True, **deviations}}


def request(monkeypatch, flag: bool, deviations: dict, constraints: dict | None = None):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("STUDIO_ZERO_BEDROOMS", "true")
    monkeypatch.setenv("FAR_VARIANTS_SKIP_DECLINED", "true" if flag else "false")
    spec = TaskSpec.model_validate(g5(deviations)).model_dump(mode="json")
    return request_for({"deal": "rent", "max_price": 1500, "property_type": "apartment", "rooms": 1, **(constraints or {})},
                       location="Barcelona", vertical="real_estate", country="ES", text=GOAL,
                       deviations=spec["deviations"], spec=spec)


# --- what the answer did not take ----------------------------------------------------------------


def test_kinds_of_the_suggested_answers() -> None:
    assert [deviation_kinds(o) for o in OPTIONS] == [{"budget"}, {"studio"}, set()]
    assert deviation_kinds(ANSWER) == {"budget", "area"}
    assert deviation_kinds("соседние районы: Чамартин") == {"place"}
    assert deviation_kinds("1 спальня вместо 2") == {"rooms"}


def _spec(deviations: dict) -> TaskSpec:
    return TaskSpec.model_validate(g5(deviations))


def test_sim_g5_the_studio_was_offered_and_not_taken() -> None:
    assert declined_deviations(OPTIONS, ANSWER, _spec({"budget_pct": 10, "area_pct": 10})) == ["studio"]


def test_the_studio_pressed_is_taken() -> None:
    assert declined_deviations(OPTIONS, OPTIONS[1], _spec({"rooms_delta": 1})) == ["budget"]
    # the model kept it as free text: still taken
    assert declined_deviations(OPTIONS, OPTIONS[1], _spec({"other": ["студия вместо 1 спальни"]})) == ["budget"]


def test_only_exact_declines_every_offered_compromise() -> None:
    assert declined_deviations(OPTIONS, OPTIONS[2], _spec({"budget_pct": 0, "area_pct": 0})) == ["budget", "studio"]
    assert declined_deviations(OPTIONS, OPTIONS[2], _spec({})) == ["budget", "studio"]  # «только точные» itself


def test_nothing_is_read_from_an_answer_that_chose_nothing_or_from_unclear_buttons() -> None:
    assert declined_deviations(OPTIONS, "не знаю", _spec({})) == []
    assert declined_deviations(["бюджет +10 % или студия"], ANSWER, _spec({"budget_pct": 10})) == []


# --- the farther variants ---------------------------------------------------------------------------


def test_a_declined_studio_is_excluded_not_other(monkeypatch) -> None:
    on = request(monkeypatch, True, {"budget_pct": 10, "area_pct": 10, "declined": ["studio"]})
    assert far_skip_declined_enabled() and on.declined == ("studio",)
    assert (classify(STUDIO, on).bucket, classify(STUDIO, on).why) == ("excluded", "rooms")
    assert classify(FLAT, on).bucket == "exact"
    # one room may be the studio itself: unconfirmed (similar), not the declined difference
    assert classify({**STUDIO, "rooms": 1}, on).bucket == "similar"


def test_flag_off_the_studio_stays_other(monkeypatch) -> None:
    off = request(monkeypatch, False, {"budget_pct": 10, "area_pct": 10, "declined": ["studio"]})
    assert off.declined == ()
    assert (classify(STUDIO, off).bucket, classify(STUDIO, off).why) == ("other", "rooms")


def test_a_chosen_studio_is_sent_as_before(monkeypatch) -> None:
    chosen = request(monkeypatch, True, {"rooms_delta": 1, "declined": ["budget"]})
    match = classify(STUDIO, chosen)
    assert (match.bucket, match.why, match.note) == ("exact", "approved_deviation", "студия при запросе от 1 комн.")
    # approved after all (an edit): no longer declined
    assert request(monkeypatch, True, {"rooms_delta": 1, "declined": ["studio"]}).declined == ()


def test_other_declined_kinds(monkeypatch) -> None:
    budget = request(monkeypatch, True, {"budget_pct": 0, "declined": ["budget"]})
    assert classify({**FLAT, "price_amount": 2400}, budget).bucket == "excluded"
    assert classify({**FLAT, "price_amount": 1800}, budget).bucket == "similar"  # similar: unchanged
    assert classify({**FLAT, "price_amount": None}, budget).bucket == "other"  # unknown price: not over the budget
    place = request(monkeypatch, True, {"declined": ["place"]})
    madrid = {**FLAT, "location": "Chamberí, Madrid"}
    assert (classify(madrid, place).bucket, classify(madrid, place).why) == ("excluded", "location")
    rooms = request(monkeypatch, True, {"declined": ["rooms"]}, {"rooms": 3})
    assert classify(FLAT, rooms).bucket == "excluded"
    assert classify(FLAT, request(monkeypatch, False, {"declined": ["rooms"]}, {"rooms": 3})).bucket == "other"


def test_empty_declined_is_left_out_of_the_spec_json() -> None:
    assert "declined" not in TaskSpec().model_dump()["deviations"]
    assert "declined" not in TaskSpec().model_dump_json()
    spec = _spec({"declined": ["studio", "studio", "junk"]})
    assert spec.deviations.declined == ["studio"] and not spec.deviations.allows()
    # the model never writes it
    assert TaskSpec().merged({"deviations": {"declined": ["budget"], "budget_pct": 5}}).deviations.declined == []


async def _run(monkeypatch, flag: bool, deviations: dict) -> tuple[MemoryRunStore, ButtonMessenger]:
    monkeypatch.setenv("STUDIO_ZERO_BEDROOMS", "true")
    monkeypatch.setenv("FAR_VARIANTS_SKIP_DECLINED", "true" if flag else "false")
    spec = TaskSpec.model_validate({"mode": "real_estate", **g5(deviations)})
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={OWNER},
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=0))
    plan = plan_campaign(GOAL, vertical="real_estate", location="Barcelona", spec=spec)
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=GOAL, actor=f"telegram:{USER}",
                                 spec=spec.model_dump(mode="json"))
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    for fid, payload in (("flat", FLAT), ("studio", STUDIO)):
        store.add_finding(cid, fid, f"🏠 {fid}", payload=payload, original=f"post {fid}", vertical="real_estate")
    await runner.tick()
    await runner.tick()
    await campaigns.set_state(cid, "completed", "campaign:test")
    await runner.tick()
    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x", operator_user_ids=frozenset({OWNER}))
    control = ControlPlane(settings, MemoryControlPlaneStore(), None, _sink, offers=store.desk)
    for _, text, buttons in list(messenger.asks):
        if text == "Показать более далёкие варианты?":
            await control.handle_callback(USER, buttons[0][1], chat_id=CHAT)  # «Одобрить»
    await runner.tick()
    return store, messenger


async def test_sim_g5_the_declined_studio_is_never_offered_nor_sent(monkeypatch) -> None:
    store, messenger = await _run(monkeypatch, True, {"budget_pct": 10, "area_pct": 10, "declined": ["studio"]})
    assert store.buckets["studio"][0] == "excluded"
    assert [text for _, text, _ in messenger.asks] == []
    assert len(messenger.findings()) == 1 and "Грасии" in messenger.findings()[0]


async def test_flag_off_the_studio_is_offered_and_sent_after_approval(monkeypatch) -> None:
    store, messenger = await _run(monkeypatch, False, {"budget_pct": 10, "area_pct": 10, "declined": ["studio"]})
    assert store.buckets["studio"][0] == "other"
    assert [text for _, text, _ in messenger.asks] == ["Показать более далёкие варианты?"]
    assert len(messenger.findings()) == 2 and "Тип: студия" in messenger.findings()[1]


async def test_a_chosen_studio_is_sent_at_once(monkeypatch) -> None:
    store, messenger = await _run(monkeypatch, True, {"rooms_delta": 1, "declined": ["budget"]})
    assert store.buckets["studio"][0] == "exact"
    assert len(messenger.findings()) == 2 and messenger.asks == []


# --- the intake records it -------------------------------------------------------------------------


async def _interview(skip_declined: bool, answer: str, deviations: dict, *, press_option: int | None = None) -> TaskSpec:
    fake = FakeInterviewer(
        ask(TERM_Q, "context.answers", SPEC, "Квартира в аренду в Барселоне, до 1500 € в месяц, от 1 спальни"),
        ask_with(DEVIATION_Q, "deviations", {"context": {"answers": [{"question": TERM_Q, "answer": "на год"}]}},
                 OPTIONS, "Аренда на год"),
        finish({"deviations": {**deviations, "asked": True}}),
    )
    control, sink = await _mode(fake)
    intake: TaskIntake = control.intake
    intake.skip_declined = skip_declined
    await say(control, INTAKE_USER, "снять piso в Барселоне до 1500 в месяц от 1 спальни")
    await say(control, INTAKE_USER, "на год")
    card = await (press(control, INTAKE_USER, f"task:opt:{press_option}") if press_option is not None
                  else say(control, INTAKE_USER, answer))
    assert card.text.startswith("Проверьте задачу:")
    await press(control, INTAKE_USER, "task:launch")
    [envelope] = sink.envelopes
    return TaskSpec.model_validate(parse_campaign_spec(envelope.arguments))


@pytest.mark.asyncio
async def test_the_intake_records_the_studio_not_taken() -> None:
    spec = await _interview(True, ANSWER, {"budget_pct": 10, "area_pct": 10})
    assert spec.deviations.declined == ["studio"]
    assert spec.deviations.budget_pct == 10 and spec.deviations.area_pct == 10


@pytest.mark.asyncio
async def test_the_intake_with_the_studio_pressed() -> None:
    spec = await _interview(True, "", {"rooms_delta": 1}, press_option=1)
    assert spec.deviations.declined == ["budget"] and spec.deviations.rooms_delta == 1


@pytest.mark.asyncio
async def test_the_intake_flag_off_records_nothing() -> None:
    spec = await _interview(False, ANSWER, {"budget_pct": 10, "area_pct": 10})
    assert spec.deviations.declined == []
