"""C2: the four task types from the first message (typed or a voice transcript) to the queued Orchestra command.

Memory only: ``MemoryIntakeStore``, a fake command sink, the rule interviewer (no network, no AI).
"""

from __future__ import annotations

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.spec import MODE_TITLES, TaskSpec
from bot.control_plane.anthropic_interviewer import system_prompt, task_prompt
from bot.control_plane.intake import STALE, MemoryIntakeStore, TaskIntake, mode_menu
from bot.control_plane.interviewer import SYSTEM
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.orchestra.parser import parse_campaign_goal, parse_campaign_spec
from tests.test_intake_dialogue import download, voice_note
from tests.test_user_intake import (
    POST_LINK,
    USER,
    FakeTranscriber,
    Sink,
    callbacks,
    claimed,
    dispatch,
    plane,
    press,
    say,
    text,
)

ANSWERS = {"place": "Валенсия", "rooms.min": "2", "investor.who": "фонды", "investor.ticket": "от 500 тыс. до 2 млн €",
           "investor.user_role": "хочу вкладывать", "deviations": "Только точные"}
FIRST = {
    "real_estate": "купить квартиру в Валенсии до 300000 евро",
    "investors": "инвесторы для стартапа в Валенсии",
    "both": "купить квартиру в Валенсии до 300000 евро и найти инвесторов в недвижимость",
}


async def to_card(control: ControlPlane, first: str, voice: bool = False):
    reply = await control.handle_voice(voice_note(USER), download) if voice else await say(control, USER, first)
    assert reply is not None
    for _ in range(20):
        draft = await control.intake.store.get(USER)
        assert draft is not None
        if draft.step == "summary":
            return reply
        reply = await say(control, USER, ANSWERS.get(draft.asking or "", "не важно"))
    raise AssertionError("the interview did not end")


async def launch(control: ControlPlane, sink: Sink, mode: str):
    assert (await press(control, USER, "task:launch")).text.startswith("Принято.")
    [envelope] = sink.envelopes
    assert envelope.command == "campaign" and envelope.arguments.startswith(f"mode={mode} ")
    goal, vertical, city, place = parse_campaign_goal(envelope.arguments)
    assert vertical == mode
    assert plan_campaign(goal, vertical=vertical, location=city, place=place).vertical == mode  # type: ignore[arg-type]
    spec = parse_campaign_spec(envelope.arguments)
    assert spec is not None and spec["mode"] == mode
    _, campaigns, _ = await dispatch(claimed("campaign", envelope.arguments))
    [campaign] = campaigns.campaigns.values()
    assert campaign.plan.vertical == mode and campaign.spec is not None and campaign.spec["mode"] == mode
    return campaign


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [False, True])
@pytest.mark.parametrize("mode", ["real_estate", "investors", "both"])
async def test_a_task_goes_from_the_first_message_to_a_queued_campaign(mode: str, voice: bool) -> None:
    control, sink, _ = plane(FakeTranscriber(FIRST[mode]) if voice else None)
    control.intake.both_mode = True
    await press(control, USER, f"mode:{mode}")
    await to_card(control, FIRST[mode], voice)
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None and draft.spec.mode == mode
    summary = draft.spec.summary_ru()
    assert MODE_TITLES[mode] in summary
    assert ("Сделка:" in summary) == (mode != "investors") and ("Тикет:" in summary) == (mode != "real_estate")
    assert draft.spec.missing_hard(mode) == []
    campaign = await launch(control, sink, mode)
    assert campaign.spec is not None and campaign.spec["place"]["name"] == "Valencia"


@pytest.mark.asyncio
async def test_a_voice_transcript_is_shown_back_once_as_heard() -> None:
    control, _, _ = plane(FakeTranscriber(FIRST["both"]))
    control.intake.both_mode = True
    await press(control, USER, "mode:both")
    reply = await control.handle_voice(voice_note(USER), download)
    assert reply is not None and reply.text.startswith("Я услышал: «купить квартиру")


@pytest.mark.asyncio
async def test_a_post_link_goes_to_an_investors_campaign_with_the_post_as_the_source() -> None:
    control, sink, _ = plane()
    control.intake.post_links = True
    card = await say(control, USER, POST_LINK)
    assert card.text.startswith("Проверьте задачу:") and POST_LINK in card.text
    campaign = await launch(control, sink, "investors")
    assert campaign.spec is not None and campaign.spec["post_link"] is True
    assert campaign.spec["sources"]["required"] == [POST_LINK]


@pytest.mark.asyncio
async def test_both_works_through_the_intake_class_alone() -> None:
    sink = Sink()
    intake = TaskIntake(MemoryIntakeStore(), sink, both_mode=True)
    assert "mode:both" in callbacks(intake.menu())
    reply = await intake.choose_mode(USER, USER, "both")
    assert reply.text.startswith("Режим: " + MODE_TITLES["both"])
    reply = await intake.on_text(text(USER, FIRST["both"]), FIRST["both"])
    assert "Сколько комнат" in reply.text


@pytest.mark.asyncio
async def test_without_the_flag_there_is_no_both_button_and_the_choice_is_refused() -> None:
    control, _, _ = plane()
    assert control.intake.both_mode is False
    for menu in (await say(control, USER, "/mode"), mode_menu(), control.intake.menu()):
        assert callbacks(menu) == ["mode:real_estate", "mode:investors"]
    assert (await press(control, USER, "mode:both")).text == STALE
    assert await control.intake.mode(USER) is None
    reply = await say(control, USER, FIRST["both"])  # no mode is chosen: the menu, still without both
    assert "mode:both" not in callbacks(reply) and callbacks(reply) == ["mode:real_estate", "mode:investors"]


@pytest.mark.asyncio
async def test_with_the_flag_the_menu_has_the_third_button() -> None:
    control, _, _ = plane()
    control.intake.both_mode = True
    assert callbacks(await say(control, USER, "/mode")) == ["mode:real_estate", "mode:investors", "mode:both"]
    assert callbacks(mode_menu(both=True))[-1] == "mode:both"


def test_the_flag_is_read_from_the_environment_and_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("INTAKE_BOTH_MODE", raising=False)
    assert ControlPlaneSettings.from_env().intake_both_mode is False
    monkeypatch.setenv("INTAKE_BOTH_MODE", "true")
    assert ControlPlaneSettings.from_env().intake_both_mode is True


def test_both_needs_the_requirements_of_both_modes_and_the_card_shows_both_parts() -> None:
    spec = TaskSpec(mode="both")
    assert spec.missing_hard("both") == ["place", "deal", "property_type", "budget.max", "investor.who",
                                         "investor.ticket", "investor.user_role"]
    assert spec.missing_hard("real_estate") == ["place", "deal", "property_type", "budget.max"]
    assert spec.missing_hard("investors") == ["place", "investor.who", "investor.ticket", "investor.user_role"]
    card = spec.summary_ru()
    assert "Недвижимость + инвесторы" in card and "Сделка:" in card and "Кого ищем:" in card
    assert TaskSpec.model_validate({"mode": "both"}).mode == "both"


def test_the_prompts_cover_both_and_the_post_link_skill_is_attached_by_the_spec() -> None:
    both = system_prompt("both")
    assert task_prompt("real_estate") in both and task_prompt("investors") in both and "both" in SYSTEM
    assert "post link" not in system_prompt("investors").lower()
    assert task_prompt("post_link") in system_prompt("investors", post_link=True)
    assert "first question" not in task_prompt("post_link")


@pytest.mark.asyncio
async def test_the_orchestra_takes_mode_both_whatever_the_intake_flag_says() -> None:
    _, campaigns, notices = await dispatch(claimed("campaign", "mode=both city=Valencia квартиры и инвесторы"))
    [campaign] = campaigns.campaigns.values()
    assert campaign.plan.vertical == "both" and not any("failed" in n for n in notices)
    assert isinstance(campaigns, MemoryCampaignStore)


@pytest.mark.asyncio
async def test_a_both_draft_is_dropped_when_the_flag_is_turned_off() -> None:
    control, _, _ = plane()
    control.intake.both_mode = True
    await press(control, USER, "mode:both")
    await say(control, USER, FIRST["both"])
    control.intake.both_mode = False
    assert await control.intake.mode(USER) is None and not await control.intake.drafting(USER)
    reply = await say(control, USER, "ещё одна задача в Мадриде")
    assert callbacks(reply) == ["mode:real_estate", "mode:investors"]


@pytest.mark.asyncio
async def test_the_ticket_answer_keeps_the_property_budget_in_both() -> None:
    control, _, _ = plane()
    control.intake.both_mode = True
    await press(control, USER, "mode:both")
    await say(control, USER, FIRST["both"])
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None and draft.spec.budget.max == 300000
    for answer in ("2", "фонды"):
        await say(control, USER, answer)
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.asking == "investor.ticket"
    await say(control, USER, "до 2 000 000 евро")
    draft = await control.intake.store.get(USER)
    assert draft is not None and draft.spec is not None
    assert draft.spec.budget.max == 300000 and draft.spec.investor.ticket.max == 2_000_000


def test_the_generic_question_of_both_has_an_investor_part() -> None:
    from bot.control_plane.rules import generic_question

    assert "про инвесторов" in generic_question(TaskSpec(mode="both")) and "про инвесторов" not in generic_question(TaskSpec(mode="real_estate"))
