"""The ``user`` role: approval, mode choice, task intake with clarifying questions, and launch."""

from __future__ import annotations

import pytest

from bot.campaign import InvalidGoal, MemoryCampaignStore, plan_campaign
from bot.control_plane.access import AccessDesk, MemoryAccessStore
from bot.control_plane.intake import (
    TASK_KEYBOARD,
    MemoryIntakeStore,
    TaskIntake,
    parse_budget,
    parse_deal,
)
from bot.control_plane.models import CommandEnvelope, IncomingMessage, Reply, TranscriptResult
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore
from bot.operators import OperatorSet
from bot.orchestra.dispatcher import OrchestraDispatcher
from bot.orchestra.models import ClaimedCommand, CommandReceipt, CommandState
from bot.orchestra.parser import CommandValidationError, parse_campaign_goal
from tests.test_operator_access import Outbox, request_id
from tests.test_orchestra_dispatcher import FakeStore

OWNER, OPERATOR, HELPER, USER, OTHER_USER, STRANGER = 11, 22, 21, 31, 32, 99
_ids = iter(range(1, 100_000))


class Sink:
    """The Orchestra inbox: idempotent on (chat, message, command) like the real one."""

    def __init__(self) -> None:
        self.envelopes: list[CommandEnvelope] = []
        self.keys: set[tuple[int, int, str]] = set()

    async def __call__(self, envelope: CommandEnvelope) -> CommandReceipt:
        key = (envelope.chat_id, envelope.message_id, envelope.command)
        duplicate = key in self.keys
        if not duplicate:
            self.keys.add(key)
            self.envelopes.append(envelope)
        return CommandReceipt(f"cmd-{len(self.envelopes)}", CommandState.QUEUED, duplicate)


class FakeTranscriber:
    model, provider = "whisper", "openrouter"

    def __init__(self, text: str) -> None:
        self.text = text

    async def transcribe(self, audio: bytes, *, filename: str) -> TranscriptResult:
        return TranscriptResult(self.text, "ru", 0.9, self.model)


def plane(transcriber: FakeTranscriber | None = None) -> tuple[ControlPlane, Sink, Outbox]:
    outbox, sink = Outbox(), Sink()
    operators = OperatorSet({OWNER}, {OPERATOR: "operator", HELPER: "helper", USER: "user", OTHER_USER: "user"})
    store = MemoryAccessStore()
    for uid, role in operators.approved.items():
        store.approved[uid] = (None, None, role)
    desk = AccessDesk(store, operators, notify=outbox)
    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x", operator_user_ids=frozenset({OWNER}))
    return ControlPlane(settings, MemoryControlPlaneStore(), transcriber, sink, access=desk), sink, outbox


def text(user: int, body: str) -> IncomingMessage:
    return IncomingMessage(chat_id=user, user_id=user, message_id=next(_ids), text=body)


async def say(control: ControlPlane, user: int, body: str) -> Reply:
    reply = await control.handle_text(text(user, body))
    assert reply is not None
    return reply


def callbacks(reply: Reply) -> list[str]:
    return [b.callback_data or "" for b in reply.buttons]


async def press(control: ControlPlane, user: int, data: str) -> Reply:
    return await control.handle_callback(user, data, chat_id=user)


async def enough(control: ControlPlane, user: int) -> Reply:
    """«Хватит, ищи»: stop the interview and get the card."""
    return await press(control, user, "task:enough")


# --- the role ----------------------------------------------------------------------------


def test_a_user_has_access_but_neither_verifies_nor_controls() -> None:
    operators = OperatorSet({OWNER}, {HELPER: "helper", USER: "user", OPERATOR: "operator"})
    assert operators.has_access(USER) and operators.role(USER) == "user" and not operators.has_access(STRANGER)
    assert USER not in operators and USER not in operators.controllers and not operators.can_control(USER)
    assert list(operators) == [OWNER, HELPER, OPERATOR] and len(operators) == 3
    assert operators.approved[USER] == "user"


@pytest.mark.asyncio
async def test_an_owner_approves_a_request_as_user_and_can_change_it_with_role() -> None:
    outbox = Outbox()
    operators = OperatorSet({OWNER})
    desk = AccessDesk(MemoryAccessStore(), operators, notify=outbox)
    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x", operator_user_ids=frozenset({OWNER}))
    control = ControlPlane(settings, MemoryControlPlaneStore(), None, Sink(), access=desk)

    await control.handle_callback(STRANGER, "access:request", "Ann", "ann")
    notice = outbox.to(OWNER)[-1]
    assert f"access:user:{request_id(notice)}" in callbacks(notice)
    assert "Пользователь" in " ".join(b.text for b in notice.buttons)
    reply = await control.handle_callback(OWNER, f"access:user:{request_id(notice)}")
    assert reply.text.startswith("Approved as user")
    assert operators.role(STRANGER) == "user" and STRANGER not in operators
    assert "/start" in outbox.to(STRANGER)[-1].text
    assert "уже есть доступ" in (await control.handle_callback(STRANGER, "access:request")).text

    assert (await say(control, OWNER, f"/role {STRANGER} operator")).text == f"{STRANGER} is now a operator."
    assert operators.can_control(STRANGER)
    assert (await say(control, OWNER, f"/role {STRANGER} user")).text == f"{STRANGER} is now a user."
    assert operators.role(STRANGER) == "user" and not operators.can_control(STRANGER)
    assert "helper|user|operator" in (await say(control, OWNER, "/role oops")).text
    assert "helper|user|operator" in (await say(control, OWNER, "/operators")).text


@pytest.mark.asyncio
async def test_a_user_cannot_use_operator_commands() -> None:
    control, sink, _ = plane()
    for body in ("/run website https://example.org", "/pause all", "/resume all", "/cancel all",
                 "/login", "/operators", "/auto on", "/role 5 operator", "/revoke 5"):
        reply = await say(control, USER, body)
        assert not reply.text.startswith(("Confirmation required", "Approved", "Auto mode is on")), body
        assert "access:request" not in callbacks(reply), body
    for body in ("/run website https://example.org", "/status", "/login"):
        assert (await say(control, USER, body)).text == "Эта команда недоступна. Опишите, что ищете, — я начну поиск."
    assert not await control.auto.applies_to(USER)
    assert sink.envelopes == []


@pytest.mark.asyncio
async def test_a_user_gets_no_verification_links() -> None:
    from bot.verification.service import AccessDenied, FlowConfig, VerificationService
    from bot.verification.store import MemoryVerificationStore
    from tests.test_live_view import TOKEN, init_data
    from tests.test_verification_flow import FakeLive, FakeNotifier, FakeWatchdog, new_job, token_of

    store, notifier = MemoryVerificationStore(), FakeNotifier()
    store.roles = {HELPER: "helper", USER: "user"}
    service = VerificationService(store, FakeLive(), FakeWatchdog(), notifier,
                                  FlowConfig(public_url="https://x.sslip.io", operator_ids=OperatorSet({OWNER}), owner_id=OWNER, bot_token=TOKEN))
    store.add_job(new_job())
    await service.tick()
    [link] = notifier.links(HELPER)
    assert notifier.links(USER) == []
    with pytest.raises(AccessDenied):
        await service.open(token_of(link), init_data(USER))


# --- mode ----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_shows_the_two_modes_and_the_choice_is_remembered() -> None:
    control, _, _ = plane()
    start = await say(control, USER, "/start")
    assert callbacks(start) == ["mode:real_estate", "mode:investors"]
    assert [b.text for b in start.buttons] == ["🏡 Участки и объекты", "💼 Инвесторы и компании"]
    assert "Запустить" in start.text and "Привет" in start.text and "/campaign" not in start.text
    assert callbacks(await say(control, USER, "/mode")) == ["mode:real_estate", "mode:investors"]
    chosen = await press(control, USER, "mode:investors")
    assert "Инвесторы" in chosen.text
    assert await control.intake.mode(USER) == "investors"
    assert (await say(control, USER, "/help")).buttons == ()  # mode already chosen
    # Operators and owners may use it too; helpers and strangers may not.
    assert "mode:real_estate" in callbacks(await say(control, OPERATOR, "/start"))
    assert "Режим доступен" in (await say(control, HELPER, "/mode")).text
    assert "только одобренные" in (await press(control, STRANGER, "mode:investors")).text
    assert "Эта кнопка устарела" in (await press(control, USER, "mode:nonsense")).text


@pytest.mark.asyncio
async def test_a_task_written_before_choosing_a_mode_is_kept() -> None:
    control, _, _ = plane()
    reply = await say(control, USER, "инвесторы для стартапа в Барселоне")
    assert "Сначала выберите режим" in reply.text and "mode:investors" in callbacks(reply)
    asked = await press(control, USER, "mode:investors")
    assert asked.text.startswith("Понял: Барселона") and "размер вложения" in asked.text
    summary = await enough(control, USER)
    assert "Проверьте задачу" in summary.text and "Город: Барселона" in summary.text


# --- intake ------------------------------------------------------------------------------


async def with_mode(mode: str = "real_estate") -> tuple[ControlPlane, Sink]:
    control, sink, _ = plane()
    await press(control, USER, f"mode:{mode}")
    return control, sink


@pytest.mark.asyncio
async def test_what_the_task_states_is_not_asked_again_and_the_card_is_structured() -> None:
    control, sink = await with_mode()
    reply = await say(control, USER, "квартиры в аренду в Мадриде до 1200 €")
    # City, deal, type and budget are known: only the rooms are asked, one question.
    assert reply.text.startswith("Понял: Мадрид, аренда, квартира, до 1200 €\n\nСколько комнат нужно?")
    assert callbacks(reply) == ["task:skip", "task:enough", "task:cancel"]
    assert [b.text for b in reply.buttons] == ["Не важно", "Хватит, ищи", "Отмена"]
    card = await enough(control, USER)
    assert card.text.startswith("Проверьте задачу")
    for line in ("Город: Мадрид", "Сделка: аренда", "Тип: квартира", "Бюджет: до 1200 €", "Комнаты: не указаны"):
        assert line in card.text, line
    # A user sees no planner internals; the owner does.
    for noise in ("Языки поиска", "окнами", "Цель:", "real_estate", "Madrid", "/"):
        assert noise not in card.text, noise
    assert card.keyboard == TASK_KEYBOARD
    assert sink.envelopes == []  # nothing runs without "Запустить"

    await press(control, OWNER, "mode:real_estate")
    await say(control, OWNER, "квартиры в аренду в Мадриде до 1200 €")
    owner = await enough(control, OWNER)
    for line in ("Город: Мадрид", "Цель: real_estate · Madrid", "Языки поиска: ES, EN, RU, UK", "Группы: до 40, окнами по 20"):
        assert line in owner.text, line


@pytest.mark.asyncio
async def test_a_missing_city_is_asked_and_a_typed_answer_is_accepted() -> None:
    control, _ = await with_mode()
    question = await say(control, USER, "квартиры в аренду до 1000 евро")
    assert question.text.startswith("Понял: аренда, квартира, до 1000 €\n\nВ каком городе искать?")
    assert "task:skip" not in callbacks(question), "the place cannot be skipped"
    assert "Не понял город" in (await say(control, USER, "где-нибудь у моря")).text
    assert "Сколько комнат" in (await say(control, USER, "Валенсия")).text
    assert "Город: Валенсия" in (await enough(control, USER)).text

    control, sink = await with_mode()
    await say(control, USER, "квартиры в аренду до 1000 евро")
    await say(control, USER, "в Лиссабон")
    summary = await enough(control, USER)
    assert "Город: Лиссабон" in summary.text  # without the AI the name is kept as typed
    await press(control, USER, "task:launch")
    assert sink.envelopes[-1].arguments.startswith("mode=real_estate place=")

    # «Хватит, ищи» never skips the place.
    control, _ = await with_mode()
    await say(control, USER, "квартиры в аренду до 1000 евро")
    assert (await enough(control, USER)).text.startswith("Место нужно в любом случае. В каком городе")


@pytest.mark.asyncio
async def test_several_cities_ask_to_choose_one() -> None:
    control, sink = await with_mode()
    question = await say(control, USER, "снять квартиру в Мадриде или Барселоне до 900 €")
    assert "несколько городов (Мадрид, Барселона)" in question.text and "task:skip" not in callbacks(question)
    assert "Сколько комнат" in (await say(control, USER, "Барселона")).text
    summary = await enough(control, USER)
    assert "Город: Барселона" in summary.text
    await press(control, USER, "task:launch")
    arguments = sink.envelopes[0].arguments
    assert arguments.startswith("mode=real_estate city=Barcelona spec=")
    assert "Мадриде или Барселоне" in arguments  # the task itself is kept as written
    _, campaigns, notices = await dispatch(claimed("campaign", arguments))
    [campaign] = campaigns.campaigns.values()
    assert campaign.plan.location == "Barcelona"
    assert campaign.spec is not None and campaign.spec["place"]["name"] == "Barcelona"  # the spec is stored
    assert (campaign.plan.constraints["deal"], campaign.plan.constraints["max_price"]) == ("rent", 900)
    assert notices == []  # a user gets no campaign id; the runner's status message follows


@pytest.mark.asyncio
async def test_the_deal_is_asked_for_real_estate_only_and_every_field_can_be_skipped() -> None:
    control, _ = await with_mode()
    deal = await say(control, USER, "квартиры в Малаге")
    assert deal.text.startswith("Понял: Малага, квартира\n\nАренда или покупка?")
    assert callbacks(deal) == ["task:deal:rent", "task:deal:sale", "task:skip", "task:enough", "task:cancel"]
    budget = await press(control, USER, "task:deal:sale")
    assert budget.text.startswith("Понял: покупка\n\nКакой бюджет?")
    assert (await press(control, USER, "task:skip")).text.startswith("Сколько комнат")  # «Не важно» for the budget
    assert "Район или вся Малага?" in (await press(control, USER, "task:skip")).text
    assert (await press(control, USER, "task:skip")).text.startswith("Что обязательно должно быть?")
    card = await press(control, USER, "task:skip")
    assert "Сделка: покупка" in card.text and "Бюджет: не важно" in card.text and "Комнаты: не важно" in card.text
    assert "Эта кнопка устарела" in (await press(control, USER, "task:deal:rent")).text

    control, _ = await with_mode()
    await say(control, USER, "квартиры в Малаге")
    assert "Какой бюджет?" in (await say(control, USER, "не важно")).text  # typed «не важно» = the button
    assert "Сколько комнат" in (await say(control, USER, "1 500")).text
    assert "Бюджет: до 1500 €" in (await enough(control, USER)).text

    control, _ = await with_mode("investors")
    ticket = await say(control, USER, "стартапы в Киеве")  # investors: who is known, the ticket is next
    assert ticket.text.startswith("Понял: Киев") and "размер вложения" in ticket.text
    role = await say(control, USER, "от 100 тыс до 1 млн €")
    assert "ищете деньги" in role.text and callbacks(role)[:2] == ["task:role:raising", "task:role:deploying"]
    card = await press(control, USER, "task:role:raising")
    assert "Тикет: от 100000 до 1000000 €" in card.text and "Ваша роль: привлекаю деньги" in card.text


@pytest.mark.asyncio
async def test_one_question_at_a_time_in_a_fixed_order() -> None:
    control, _ = await with_mode()
    first = await say(control, USER, "жильё")
    assert first.text == "В каком городе искать? Напишите город, район или регион в любой стране."
    expected = ["Аренда или покупка?", "Что ищете:", "Какой бюджет?"]
    for answer, question in zip(["Севилья", "аренда", "квартира"], expected, strict=True):
        assert question in (await say(control, USER, answer)).text
    assert "Сколько комнат" in (await say(control, USER, "до 700")).text
    assert "Район или вся Севилья?" in (await say(control, USER, "2")).text
    assert "Что обязательно" in (await say(control, USER, "центр")).text
    card = await say(control, USER, "лифт")
    assert card.text.startswith("Проверьте задачу")
    for line in ("Город: Севилья", "Районы: центр", "Сделка: аренда", "Тип: квартира", "Бюджет: до 700 €", "Комнаты: от 2",
                 "Обязательно: лифт"):
        assert line in card.text, line


@pytest.mark.asyncio
async def test_cancel_clears_the_draft_at_any_point_and_edit_keeps_the_task() -> None:
    control, sink = await with_mode()
    await say(control, USER, "квартиры")
    assert "Черновик удалён" in (await say(control, USER, "Отмена")).text
    assert "Эта задача уже запущена или устарела" in (await press(control, USER, "task:launch")).text
    await say(control, USER, "квартиры в аренду в Мадриде до 1200 €")
    assert "Черновик удалён" in (await press(control, USER, "task:cancel")).text
    await say(control, USER, "квартиры в аренду в Мадриде до 1200 €")
    await enough(control, USER)
    menu = await press(control, USER, "task:edit")  # «Изменить» lists the fields; the task stays
    assert menu.text == "Что изменить?" and callbacks(menu)[:2] == ["task:field:place", "task:field:deal"]
    assert callbacks(menu)[-1] == "task:back"
    assert "Проверьте задачу" in (await press(control, USER, "task:back")).text
    assert "Принято" in (await press(control, USER, "task:launch")).text
    assert await control.intake.mode(USER) == "real_estate"  # the mode survives
    assert len(sink.envelopes) == 1


@pytest.mark.asyncio
async def test_launch_queues_exactly_one_campaign_for_the_user_and_their_chat() -> None:
    control, sink = await with_mode()
    task = text(USER, "квартиры в аренду в Мадриде до 1200 €")
    await control.handle_text(task)
    await enough(control, USER)
    launched = await press(control, USER, "task:launch")
    assert launched.text.startswith("Принято. Начинаю поиск.")
    assert "устарела" in (await press(control, USER, "task:launch")).text  # double tap
    [envelope] = sink.envelopes
    assert (envelope.command, envelope.chat_id, envelope.user_id, envelope.message_id, envelope.auto) == ("campaign", USER, USER, task.message_id, False)
    goal, vertical, city, _place = parse_campaign_goal(envelope.arguments)
    plan = plan_campaign(goal, vertical=vertical, location=city)  # type: ignore[arg-type]
    assert (plan.location, plan.vertical, plan.constraints["deal"], plan.constraints["max_price"]) == ("Madrid", "real_estate", "rent", 1200)
    # The confirmed requirements travel with the command and are stored on the campaign.
    _, campaigns, _ = await dispatch(claimed("campaign", envelope.arguments))
    [campaign] = campaigns.campaigns.values()
    assert campaign.spec is not None and campaign.spec["budget"]["max"] == 1200 and campaign.spec["deal"] == "rent"
    assert campaign.plan.constraints["property_type"] == "apartment"


@pytest.mark.asyncio
async def test_a_failed_enqueue_keeps_the_summary_for_another_try() -> None:
    calls: list[CommandEnvelope] = []

    async def flaky(envelope: CommandEnvelope) -> CommandReceipt:
        calls.append(envelope)
        if len(calls) == 1:
            raise ConnectionError("db down")
        return CommandReceipt("cmd-1", CommandState.QUEUED)

    intake = TaskIntake(MemoryIntakeStore(), flaky)
    await intake.choose_mode(USER, USER, "real_estate")
    await intake.on_text(text(USER, "квартиры в аренду в Мадриде до 1200 €"), "квартиры в аренду в Мадриде до 1200 €")
    await intake.on_button(USER, USER, "enough", "")
    assert "Не удалось" in (await intake.on_button(USER, USER, "launch", "")).text
    assert "Принято" in (await intake.on_button(USER, USER, "launch", "")).text
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_campaign_goal_from_a_user_goes_through_intake_and_operators_keep_confirm() -> None:
    control, sink, _ = plane()
    await press(control, USER, "mode:real_estate")
    assert "Сколько комнат" in (await say(control, USER, "/campaign квартиры в аренду в Мадриде до 1200 €")).text
    assert sink.envelopes == []
    assert (await say(control, USER, "/campaign status")).text == "Проверяю, как идёт поиск."
    assert sink.envelopes[-1].arguments == "status"
    # Users never see campaign ids: «/campaign cancel» stops their running search (none here), no token.
    assert (await say(control, USER, "/campaign cancel 123")).text == "Сейчас нет активного поиска."
    assert sink.envelopes[-1].arguments == "status"
    assert "Confirmation required for /campaign" in (await say(control, OPERATOR, "/campaign квартиры в Мадриде")).text


@pytest.mark.asyncio
async def test_a_voice_task_from_a_user_goes_through_intake() -> None:
    control, sink, _ = plane(FakeTranscriber("квартиры в аренду в Мадриде до 1200 евро"))
    await press(control, USER, "mode:real_estate")

    async def download() -> bytes:
        return b"OggS"

    reply = await control.handle_voice(IncomingMessage(USER, USER, next(_ids), voice_file_id="v", voice_size=100, voice_duration_seconds=3), download)
    # The transcript is shown back once, then the first question.
    assert reply and reply.text.startswith("Я услышал: «квартиры в аренду в Мадриде до 1200 евро»\n\nПонял: Мадрид")
    assert "Сколько комнат" in reply.text and reply.text.count("Я услышал") == 1
    assert "Город: Мадрид" in (await enough(control, USER)).text
    assert sink.envelopes == []
    # Size checks still apply before anything is downloaded; strangers stay refused.
    too_long = IncomingMessage(USER, USER, next(_ids), voice_file_id="v", voice_size=100, voice_duration_seconds=10_000)
    assert "слишком длинное" in (await control.handle_voice(too_long, download)).text  # type: ignore[union-attr]
    stranger = IncomingMessage(STRANGER, STRANGER, next(_ids), voice_file_id="v", voice_size=100, voice_duration_seconds=3)
    assert "после одобрения доступа" in (await control.handle_voice(stranger, download)).text  # type: ignore[union-attr]


def test_answer_parsers() -> None:
    assert parse_deal("Аренда") == "rent" and parse_deal("хочу купить") == "sale" and parse_deal("не важно") == "any"
    assert parse_deal("что-то") is None
    assert parse_budget("до 1200 €") == 1200 and parse_budget("1.5к") == 1500 and parse_budget("250 000") == 250_000
    assert parse_budget("много") is None


def test_the_interviewer_is_pluggable_and_defaults_to_the_rules() -> None:
    intake = TaskIntake(MemoryIntakeStore(), Sink())
    assert intake.interviewer is None and intake.rules.model == "rules"


# --- the Orchestra -------------------------------------------------------------------------


def claimed(command: str, arguments: str, user: int = USER) -> ClaimedCommand:
    return ClaimedCommand(f"cmd-{next(_ids)}", command, arguments, user, user)


async def dispatch(*items: ClaimedCommand, campaigns: MemoryCampaignStore | None = None) -> tuple[FakeStore, MemoryCampaignStore, list[str]]:
    store, notices = FakeStore(list(items)), []
    campaigns = campaigns or MemoryCampaignStore()
    roles = OperatorSet({OWNER}, {OPERATOR: "operator", USER: "user", OTHER_USER: "user", HELPER: "helper"})

    async def notify(_chat: int, message: str) -> None:
        notices.append(message)

    dispatcher = OrchestraDispatcher(store, operator_ids=roles.controllers, notifier=notify, campaigns=campaigns, roles=roles)  # type: ignore[arg-type]
    while await dispatcher.process_once():
        pass
    return store, campaigns, notices


@pytest.mark.asyncio
async def test_the_dispatcher_accepts_a_users_campaign_and_nothing_else() -> None:
    store, campaigns, notices = await dispatch(
        claimed("campaign", "недвижимость квартиры в аренду в Мадриде до 1200 €"),
        claimed("campaign", "status"),
        claimed("run", "website https://example.org"),
        claimed("pause", "all"),
        claimed("campaign", "инвесторы в Мадриде", user=HELPER),
        claimed("campaign", "инвесторы в Мадриде", user=STRANGER),
    )
    [campaign] = campaigns.campaigns.values()
    assert (campaign.requested_by, campaign.chat_id, campaign.plan.vertical, campaign.plan.location) == (USER, USER, "real_estate", "Madrid")
    states = [(state, code) for _, state, _, code in store.completed]
    assert states == [(CommandState.FINISHED, None), (CommandState.FINISHED, None)] + [(CommandState.FAILED, "not_operator")] * 4
    assert not any(campaign.id in n or "Кампания" in n for n in notices)
    assert "Ищу…" in notices  # /campaign status, user-safe


@pytest.mark.asyncio
async def test_a_user_cancels_and_sees_only_their_own_campaigns() -> None:
    campaigns = MemoryCampaignStore()
    plan = plan_campaign("квартиры в Мадриде")
    mine = await campaigns.create(plan, chat_id=USER, requested_by=USER, source_text="x", actor="t")
    theirs = await campaigns.create(plan, chat_id=OTHER_USER, requested_by=OTHER_USER, source_text="x", actor="t")
    shared = await campaigns.create(plan, chat_id=-500, requested_by=OPERATOR, source_text="x", actor="t")
    group_status = ClaimedCommand("cmd-g", "campaign", "status", -500, USER)
    _, _, notices = await dispatch(claimed("campaign", f"cancel {theirs}"), claimed("campaign", f"cancel {mine}"), group_status,
                                   campaigns=campaigns)
    assert campaigns.campaigns[theirs].state == "planned" and campaigns.campaigns[mine].state == "cancelled"
    assert notices == ["Этот поиск уже завершён или недоступен.", "Пока ничего подходящего не нашёл."]  # no ids, no other people's campaigns
    assert shared not in notices[-1]
    # An operator still cancels any campaign.
    _, _, _ = await dispatch(claimed("campaign", f"cancel {theirs}", user=OPERATOR), campaigns=campaigns)
    assert campaigns.campaigns[theirs].state == "cancelled"


# --- review follow-ups: authoritative mode and city, expiry, owner notice --------------------


def test_explicit_vertical_and_location_override_detection() -> None:
    assert plan_campaign("квартиры и инвесторы в Мадриде", vertical="investors").vertical == "investors"
    assert plan_campaign("инвесторы в Мадриде", vertical="real_estate").vertical == "real_estate"
    assert plan_campaign("что-нибудь в Мадриде", vertical="investors").vertical == "investors"  # no "no vertical" error
    assert plan_campaign("квартиры в Мадриде или Барселоне", location="Barcelona").location == "Barcelona"
    assert plan_campaign("квартиры", location="Lisbon").location == "Lisbon"  # any place in the world
    with pytest.raises(InvalidGoal):
        plan_campaign("квартиры в Мадриде", vertical="both")  # type: ignore[arg-type]


def test_the_campaign_parser_strips_mode_and_city_tokens() -> None:
    assert parse_campaign_goal("mode=investors city=Málaga квартиры") == ("квартиры", "investors", "Málaga", None)
    assert parse_campaign_goal("city=Ubud,_Bali виллы") == ("виллы", None, "Ubud, Bali", None)
    assert parse_campaign_goal("квартиры в Мадриде") == ("квартиры в Мадриде", None, None, None)
    from bot.control_plane.intake import encode_place

    token = encode_place({"en": "Ubud, Bali", "ru": "Убуд, Бали", "country": "ID"})
    assert " " not in token
    assert parse_campaign_goal(f"place={token} виллы")[3] == {"en": "Ubud, Bali", "ru": "Убуд, Бали", "country": "ID"}
    for bad in ("mode=both квартиры", "mode=investors mode=investors x", "city= x", "mode=investors",
                "city=Madrid city=Kyiv квартиры", "place=%%% x", f"place={token} place={token} x",
                "city=" + "a" * 81 + " x", "place=" + encode_place({"ru": "без en"}) + " x"):
        with pytest.raises(CommandValidationError):
            parse_campaign_goal(bad)


@pytest.mark.asyncio
async def test_bad_mode_or_city_tokens_are_rejected_without_a_campaign() -> None:
    for arguments in ("mode=both квартиры в Мадриде", "place=%%% квартиры"):
        _store, campaigns, notices = await dispatch(claimed("campaign", arguments))
        assert campaigns.campaigns == {}
        assert "failed validation" in notices[-1]


@pytest.mark.asyncio
async def test_the_chosen_mode_wins_from_intake_through_the_queue_to_the_stored_plan() -> None:
    for mode, task, other in (("investors", "квартиры и инвесторы в Мадриде", "real_estate"),
                              ("real_estate", "инвестиции в квартиры в Мадриде до 900 €", "investors")):
        control, sink = await with_mode(mode)
        await say(control, USER, task)
        await enough(control, USER)
        await press(control, USER, "task:launch")
        [envelope] = sink.envelopes
        assert envelope.arguments.startswith(f"mode={mode} ")
        _, campaigns, _ = await dispatch(claimed("campaign", envelope.arguments))
        [campaign] = campaigns.campaigns.values()
        assert campaign.plan.vertical == mode != other
    # Operators' plain /campaign goal keeps today's detection.
    _, campaigns, _ = await dispatch(claimed("campaign", "квартиры и инвесторы в Мадриде", user=OPERATOR))
    assert next(iter(campaigns.campaigns.values())).plan.vertical == "both"
    assert next(iter(campaigns.campaigns.values())).spec is None


@pytest.mark.asyncio
async def test_a_draft_untouched_for_a_day_is_gone() -> None:
    from datetime import timedelta

    control, sink = await with_mode()
    await say(control, USER, "квартиры в аренду в Мадриде до 1200 €")
    await enough(control, USER)
    store = control.intake.store
    store.drafts[USER].updated_at -= timedelta(hours=25)  # type: ignore[attr-defined]
    assert (await press(control, USER, "task:launch")).text == "Черновик устарел, опишите задачу заново."
    assert sink.envelopes == [] and await control.intake.mode(USER) == "real_estate"

    await say(control, USER, "квартиры в аренду до 1200 €")  # asks for the city
    store.drafts[USER].updated_at -= timedelta(hours=25)  # type: ignore[attr-defined]
    assert "устарела" in (await press(control, USER, "task:skip")).text
    await say(control, USER, "квартиры в аренду до 1200 €")  # asks for the city again
    store.drafts[USER].updated_at -= timedelta(hours=25)  # type: ignore[attr-defined]
    assert (await press(control, USER, "task:launch")).text == "Черновик устарел, опишите задачу заново."
    # A stale question step: the next text is a new task, not an answer.
    assert "Понял: Севилья" in (await say(control, USER, "квартиры в аренду в Севилье до 800 €")).text


@pytest.mark.asyncio
async def test_owners_hear_when_a_user_launches() -> None:
    control, sink, outbox = plane()
    await press(control, USER, "mode:real_estate")
    await say(control, USER, "квартиры в аренду в Мадриде до 1200 €")
    await enough(control, USER)
    await control.handle_callback(USER, "task:launch", "Ann", "ann", chat_id=USER)
    [notice] = outbox.to(OWNER)
    assert notice.text.startswith("Пользователь Ann (@ann), ID 31 запустил кампанию: real_estate · Madrid")
    # An operator launching the same way is not announced.
    await press(control, OPERATOR, "mode:real_estate")
    await say(control, OPERATOR, "квартиры в аренду в Мадриде до 1200 €")
    await enough(control, OPERATOR)
    await control.handle_callback(OPERATOR, "task:launch", "Op", "op", chat_id=OPERATOR)
    assert len(outbox.to(OWNER)) == 1 and len(sink.envelopes) == 2


@pytest.mark.asyncio
async def test_only_the_owner_sees_commands_on_start() -> None:
    control, _, _ = plane()
    owner = await say(control, OWNER, "/start")
    assert "/operators" in owner.text and "/run" in owner.text
    for who in (USER, OPERATOR):
        start = await say(control, who, "/start")
        assert start.text.startswith("Привет") and "/" not in start.text, who
        assert callbacks(start) == ["mode:real_estate", "mode:investors"]
    helper = await say(control, HELPER, "/start")
    assert helper.text.startswith("Привет") and "/" not in helper.text and not helper.buttons
    stranger = await say(control, STRANGER, "/start")
    assert stranger.text.startswith("Привет") and "/" not in stranger.text
    assert callbacks(stranger) == ["access:request"]


@pytest.mark.asyncio
async def test_owner_settings_switch_roles_with_buttons() -> None:
    control, _, outbox = plane()
    assert {"set:list", "login:list"} <= set(callbacks(await say(control, OWNER, "/start")))
    assert not {"set:list", "login:list"} & set(callbacks(await say(control, OPERATOR, "/start")))
    listing = await press(control, OWNER, "set:list")
    assert f"set:user:{HELPER}" in callbacks(listing)
    card = await press(control, OWNER, f"set:user:{HELPER}")
    assert "Сейчас: Помощник" in card.text
    assert [b.text for b in card.buttons][:3] == ["Пользователь", "✓ Помощник", "Оператор"]
    # A helper becomes a normal user: the role changes at once and they are told in Russian.
    card = await press(control, OWNER, f"set:role:{HELPER}:user")
    assert "Сейчас: Пользователь" in card.text and control.operators.role(HELPER) == "user"
    assert "теперь вы пользователь" in outbox.to(HELPER)[-1].text
    assert (await say(control, HELPER, "/start")).text.startswith("Привет! 👋 Я помогу")
    # Removing access asks first.
    ask = await press(control, OWNER, f"set:del:{HELPER}")
    assert callbacks(ask) == [f"set:delok:{HELPER}", f"set:user:{HELPER}"]
    done = await press(control, OWNER, f"set:delok:{HELPER}")
    assert done.text.startswith("Доступ удалён") and control.operators.role(HELPER) is None
    # Only an owner, never on an owner, never with a forged role.
    assert "только владельцу" in (await press(control, OPERATOR, "set:list")).text
    assert "только владельцу" in (await press(control, USER, f"set:role:{USER}:operator")).text
    assert control.operators.role(USER) == "user"
    assert "устарела" in (await press(control, OWNER, f"set:role:{OWNER}:helper")).text
    await press(control, OWNER, f"set:role:{USER}:admin")
    assert control.operators.role(USER) == "user"
    assert (await say(control, OWNER, "/settings")).text.startswith("⚙️ Настройки")
