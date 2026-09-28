"""AI task understanding in Telegram intake, with a fake model (no network).

The AI picks the main points and the extra wishes, asks only for what is
missing and writes «Проверьте задачу»; the code still insists on a gazetteer
city and falls back to the deterministic rules whenever the AI is off, fails
or answers nonsense. «Остановить поиск» under the launch reply does what
typing «стоп» does.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from bot.campaign import plan_campaign
from bot.campaign.architect import MAX_TEXT_CHARS
from bot.control_plane.intake import SEARCH_KEYBOARD, TASK_KEYBOARD, Draft, quotes
from bot.control_plane.models import IncomingMessage, Reply
from bot.control_plane.service import NO_ACTIVE_SEARCH, SEARCH_STOPPED, ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.understanding import (
    SCHEMA,
    SYSTEM,
    OpenRouterUnderstanding,
    TaskUnderstanding,
    UnderstandingError,
    parse_understanding,
)
from bot.orchestra.parser import parse_campaign_goal
from tests.test_user_intake import (
    OTHER_USER,
    OWNER,
    STRANGER,
    USER,
    FakeTranscriber,
    callbacks,
    plane,
    press,
    say,
)
from tests.test_visibility_gate import assert_plain_russian, stoppable

UK_VOICE = ("шукаємо ділянку або участок від тисячі метрів з будинком або без в передмісті Мадрида "
            "5 хвилин до метро на машині для забудови купівля")
LAND = TaskUnderstanding(
    city="Madrid", deal="sale", property_type="land",
    primary=["земельный участок", "покупка", "пригород Мадрида", "площадь от 1000 м²", "под застройку"],
    secondary=["с домом или без", "до метро 5 минут на машине"],
    summary_ru=("Ищем земельный участок под застройку в пригороде Мадрида, покупка.\n"
                "Площадь от 1000 м².\nДополнительно: с домом или без, до метро 5 минут на машине."),
)


class FakeAI:
    """Hands out scripted understandings (or raises them) and records every call."""

    model = "fake/intake"

    def __init__(self, *results: TaskUnderstanding | BaseException) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def understand(self, *, mode: str, task: str, answers: list[str], known: dict[str, Any]) -> TaskUnderstanding:
        self.calls.append({"mode": mode, "task": task, "answers": list(answers), "known": dict(known)})
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, BaseException):
            raise result
        return TaskUnderstanding.load(result.payload())


def with_ai(ai: FakeAI, transcriber: FakeTranscriber | None = None):  # type: ignore[no-untyped-def]
    control, sink, outbox = plane(transcriber)
    control.intake.understander = ai
    return control, sink, outbox


def assert_summary_buttons(reply: Reply) -> None:
    assert reply.keyboard == TASK_KEYBOARD, reply.text


# --- the land-plot case --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_ukrainian_voice_land_task_is_a_plot_not_a_room() -> None:
    ai = FakeAI(LAND)
    control, sink, _ = with_ai(ai, FakeTranscriber(UK_VOICE))
    await press(control, USER, "mode:real_estate")
    reply = await control.handle_voice(IncomingMessage(USER, USER, 777, voice_file_id="v", voice_size=10,
                                                       voice_duration_seconds=5), _audio)
    assert reply is not None
    assert reply.text == (
        "Проверьте задачу:\n"
        "Ищем земельный участок под застройку в пригороде Мадрида, покупка.\n"
        "Площадь от 1000 м².\n"
        "Дополнительно: с домом или без, до метро 5 минут на машине.\n\n"
        "Всё верно? Нажмите «Запустить» внизу, чтобы начать поиск."
    )
    assert "комнат" not in reply.text
    for word in ("шукаємо", "ділянку", "передмісті", "хвилин", "будинком"):
        assert word not in reply.text.casefold()
    assert_summary_buttons(reply)
    assert_plain_russian(reply, "summary")
    [call] = ai.calls
    assert call["mode"] == "real_estate" and call["task"] == UK_VOICE and call["answers"] == []
    # Launch: the goal carries the AI's requirements and the original task; the plan reads "покупка".
    launched = await press(control, USER, "task:launch")
    assert launched.text.startswith("Принято. Начинаю поиск.")
    [envelope] = sink.envelopes
    goal, vertical, city, _place = parse_campaign_goal(envelope.arguments)
    assert (vertical, city) == ("real_estate", "Madrid")
    assert goal.startswith("покупка Тип: земельный участок. Задача: шукаємо")
    assert "Главное: земельный участок; покупка; пригород Мадрида; площадь от 1000 м²; под застройку." in goal
    assert "Дополнительно: с домом или без; до метро 5 минут на машине." in goal
    plan = plan_campaign(goal, vertical="real_estate", location="Madrid")
    assert plan.constraints["deal"] == "sale" and plan.constraints["max_price"] is None


async def _audio() -> bytes:
    return b"OggS"


@pytest.mark.asyncio
async def test_the_owner_also_sees_the_technical_lines() -> None:
    control, _, _ = with_ai(FakeAI(LAND))
    await press(control, OWNER, "mode:real_estate")
    reply = await say(control, OWNER, UK_VOICE)
    assert "Разбор задачи: ИИ (fake/intake)" in reply.text and "Цель: real_estate · Madrid · sale" in reply.text
    assert "Языки поиска:" in reply.text


# --- questions -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_city_is_asked_by_the_ai_and_the_answer_goes_back_to_it() -> None:
    asking = TaskUnderstanding(deal="sale", property_type="land", primary=["земельный участок", "покупка"],
                               questions=["В каком городе или рядом с каким городом искать участок?"],
                               summary_ru="Ищем земельный участок, покупка.")
    ai = FakeAI(asking, LAND)
    control, _, _ = with_ai(ai)
    await press(control, USER, "mode:real_estate")
    question = await say(control, USER, "ищу участок под застройку, покупка")
    assert question.text == ("В каком городе или рядом с каким городом искать участок? "
                             "Напишите город, район или регион в любой стране.")
    assert callbacks(question) == [], "no list of cities to pick from"
    summary = await say(control, USER, "под Мадридом")
    assert summary.text.startswith("Проверьте задачу:\nИщем земельный участок")
    assert_summary_buttons(summary)
    assert ai.calls[1]["answers"] == ["под Мадридом"] and ai.calls[1]["task"] == "ищу участок под застройку, покупка"
    assert ai.calls[1]["known"]["city"] == "Madrid"  # a plain city name is read before the call


@pytest.mark.asyncio
async def test_the_city_question_has_no_list_of_cities_and_an_old_city_button_is_stale() -> None:
    asking = TaskUnderstanding(deal="rent", property_type="apartment", questions=["В каком городе искать?"],
                               summary_ru="Снять квартиру.")
    ai = FakeAI(asking)
    control, _, _ = with_ai(ai)
    await press(control, USER, "mode:real_estate")
    question = await say(control, USER, "хочу снять квартиру")
    assert question.text.startswith("В каком городе искать?") and question.buttons == ()
    assert (await press(control, USER, "task:city:2")).text == "Эта кнопка устарела."


@pytest.mark.asyncio
async def test_any_place_in_the_world_is_taken_with_its_names_and_country() -> None:
    parsed = parse_understanding(json.dumps({
        "city": "Ubud, Bali", "summary_ru": "Контакты компаний по управлению виллами в Убуде.",
        "place": {"es": "Ubud, Bali", "ru": "Убуд, Бали", "uk": "Убуд, Балі", "ru_in": "Убуде", "uk_in": "Убуді",
                  "country": "id"}}))
    assert parsed.city == "Ubud, Bali" and parsed.place["country"] == "ID" and parsed.place["ru"] == "Убуд, Бали"
    assert parse_understanding(json.dumps({"city": "Toledo", "summary_ru": "Квартира в Толедо."})).city == "Toledo"
    assert parse_understanding(json.dumps({"city": "мадрид", "summary_ru": "Квартира."})).city == "Madrid"
    bali = TaskUnderstanding(city="Ubud, Bali", place=parsed.place, target="русскоязычные агенты по управлению виллами",
                             primary=["управление виллами"], summary_ru="Ищем русскоязычные компании и агентов по "
                                                                        "управлению виллами.")
    control, sink, _ = with_ai(FakeAI(bali))
    await press(control, USER, "mode:investors")
    summary = await say(control, USER, "надай контакти російськомовних компаній та агентів з управління віллами, Убуд")
    assert "Город: Убуд, Бали" in summary.text and "Киев" not in summary.text
    await press(control, USER, "task:launch")
    goal, vertical, city, place = parse_campaign_goal(sink.envelopes[-1].arguments)
    assert (vertical, city, place["en"], place["country"]) == ("investors", None, "Ubud, Bali", "ID")
    plan = plan_campaign(goal, vertical=vertical, place=place)
    assert (plan.location, plan.country, plan.location_aliases["ru"]) == ("Ubud, Bali", "ID", "Убуд, Бали")


@pytest.mark.asyncio
async def test_without_a_place_the_code_asks_even_if_the_ai_does_not() -> None:
    forgot = TaskUnderstanding(deal="rent", primary=["квартира"], summary_ru="Снять квартиру.")
    control, _, _ = with_ai(FakeAI(forgot))
    await press(control, USER, "mode:real_estate")
    question = await say(control, USER, "снять квартиру")
    assert question.text == "В каком городе искать? Напишите город, район или регион в любой стране."
    # The same answer again: still no known city, asked again with a hint.
    again = await say(control, USER, "Толедо")
    assert again.text.startswith("Не понял город. В каком городе искать?")


@pytest.mark.asyncio
async def test_the_deal_is_asked_once_and_optional_questions_can_be_skipped() -> None:
    first = TaskUnderstanding(city="Madrid", property_type="apartment", questions=["Какой бюджет?"],
                              summary_ru="Квартира в Мадриде.")
    ai = FakeAI(first)
    control, _, _ = with_ai(ai)
    await press(control, USER, "mode:real_estate")
    question = await say(control, USER, "квартира в Мадриде")
    assert question.text.splitlines() == ["Уточните, пожалуйста:", "1. Аренда или покупка?", "2. Какой бюджет?",
                                          "Можно ответить одним сообщением."]
    assert callbacks(question)[:3] == ["task:deal:rent", "task:deal:sale", "task:deal:any"]
    ai.results = [TaskUnderstanding(city="Madrid", deal="rent", property_type="apartment",
                                    questions=["Какой бюджет?"], summary_ru="Снять квартиру в Мадриде.")]
    only_budget = await press(control, USER, "task:deal:rent")
    assert only_budget.text == "Какой бюджет?" and callbacks(only_budget) == ["task:ask:skip"]
    calls = len(ai.calls)
    summary = await press(control, USER, "task:ask:skip")
    assert summary.text.startswith("Проверьте задачу:\nСнять квартиру в Мадриде.") and len(ai.calls) == calls


@pytest.mark.asyncio
async def test_investors_need_who_to_look_for() -> None:
    vague = TaskUnderstanding(city="Barcelona", summary_ru="Контакты в Барселоне.")
    ai = FakeAI(vague, TaskUnderstanding(city="Barcelona", target="бизнес-ангелы", primary=["бизнес-ангелы"],
                                         summary_ru="Ищем бизнес-ангелов в Барселоне."))
    control, sink, _ = with_ai(ai)
    await press(control, USER, "mode:investors")
    question = await say(control, USER, "нужны контакты в Барселоне")
    assert question.text.startswith("Кого ищете?")
    summary = await say(control, USER, "бизнес-ангелы")
    assert "бизнес-ангелов" in summary.text
    await press(control, USER, "task:launch")
    assert "Кого ищем: бизнес-ангелы." in sink.envelopes[0].arguments


def test_a_summary_that_echoes_the_transcript_is_rebuilt_from_the_lists() -> None:
    echo = TaskUnderstanding(city="Madrid", deal="sale", primary=["земельный участок"], secondary=["рядом с метро"],
                             summary_ru="Вы сказали: " + UK_VOICE)
    draft = Draft(USER, USER, "real_estate", task=UK_VOICE, city="Madrid", deal="sale", ai=echo.payload())
    from bot.control_plane.intake import ai_summary

    reply = ai_summary(draft, echo, draft.plan())
    assert "шукаємо" not in reply.text
    assert "Главное: земельный участок\nДополнительно: рядом с метро" in reply.text
    assert reply.text.startswith("Проверьте задачу:\nГород: Мадрид\n")  # the summary lost the city: it is added
    assert quotes("один два три четыре пять шесть семь", "ноль один два три четыре пять шесть семь восемь")
    assert not quotes("снять квартиру в Мадриде", "снять квартиру в Мадриде до 1200")


def test_a_long_task_keeps_the_goal_within_the_planner_limit() -> None:
    ai = TaskUnderstanding(city="Madrid", deal="sale", budget_max=300000, currency="EUR", property_type="land",
                           primary=["земельный участок"] * 1, secondary=["x" * 100], summary_ru="Участок.")
    draft = Draft(USER, USER, "real_estate", task="участок " * 230, city="Madrid", deal="sale", budget=300000,
                  ai=ai.payload())
    goal = draft.goal_text()
    assert len(goal) <= MAX_TEXT_CHARS and goal.startswith("покупка до 300000 € Тип: земельный участок.")
    assert goal.endswith("Дополнительно: " + "x" * 100 + ".")
    assert draft.plan().constraints["max_price"] == 300000
    # The draft survives the store round trip.
    assert Draft.load(USER, USER, "real_estate", "summary", json.loads(json.dumps(draft.payload()))).goal_text() == goal


# --- fallback ------------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [UnderstandingError("timeout"), UnderstandingError("invalid_response", status=200),
                                     TimeoutError(), ValueError("bad json")])
async def test_any_ai_failure_falls_back_to_the_rules(failure: BaseException, caplog: pytest.LogCaptureFixture) -> None:
    control, sink, _ = with_ai(FakeAI(failure))
    await press(control, USER, "mode:real_estate")
    with caplog.at_level(logging.WARNING):
        reply = await say(control, USER, "Ищем участок от 1000 м² с домом или без в пригороде Мадрида, покупка, до 300 000 €")
    assert reply.text.startswith("Проверьте задачу:\nРежим: 🏡 Участки и объекты\nГород: Мадрид\nСделка: покупка\nТип: участок")
    assert "Пожелания: площадь от 1 000 м², с домом или без, пригород" in reply.text
    assert "telegram.intake.understanding_failed" in caplog.text
    await press(control, USER, "task:launch")
    assert "Главное" not in sink.envelopes[0].arguments


@pytest.mark.asyncio
async def test_without_an_ai_client_nothing_changes() -> None:
    control, _, _ = plane()
    assert control.intake.understander is None
    await press(control, USER, "mode:real_estate")
    assert (await say(control, USER, "снять квартиру в Мадриде до 1200 €")).text.startswith("Проверьте задачу:\nРежим:")


@pytest.mark.asyncio
async def test_a_failure_mid_dialogue_hands_the_answer_to_the_rules() -> None:
    asking = TaskUnderstanding(deal="rent", questions=["В каком городе искать?"], summary_ru="Снять квартиру.")
    ai = FakeAI(asking, UnderstandingError("timeout"))
    control, _, _ = with_ai(ai)
    await press(control, USER, "mode:real_estate")
    await say(control, USER, "снять квартиру до 1000 €")
    summary = await say(control, USER, "Малага")
    assert summary.text.startswith("Проверьте задачу:\nРежим:") and "Город: Малага" in summary.text
    assert "Сделка: аренда" in summary.text


# --- the stop button -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_stop_button_stops_the_users_search_like_typing_stop() -> None:
    control, campaigns, orchestra, campaign_id = await stoppable()
    reply = await press(control, USER, "search:stop")
    assert reply.text == SEARCH_STOPPED
    assert campaigns.campaigns[campaign_id].state == "cancelled"
    [envelope] = orchestra.envelopes
    assert envelope.arguments == f"cancel {campaign_id}" and envelope.message_id < 0
    assert (await press(control, USER, "search:stop")).text == NO_ACTIVE_SEARCH
    assert len(orchestra.envelopes) == 1


@pytest.mark.asyncio
async def test_only_the_requester_or_an_owner_can_press_stop() -> None:
    control, campaigns, orchestra, campaign_id = await stoppable(chat=-500)
    for someone in (OTHER_USER, STRANGER):
        reply = await control.handle_callback(someone, "search:stop", chat_id=-500)
        assert reply.text != SEARCH_STOPPED
        assert_plain_russian(reply, str(someone))
    assert orchestra.envelopes == [] and campaigns.campaigns[campaign_id].state == "discovering"
    assert (await control.handle_callback(OWNER, "search:stop", chat_id=-500)).text == SEARCH_STOPPED
    assert campaigns.campaigns[campaign_id].state == "cancelled"


@pytest.mark.asyncio
async def test_the_launch_reply_carries_the_stop_button() -> None:
    control, _, _ = with_ai(FakeAI(LAND))
    await press(control, USER, "mode:real_estate")
    await say(control, USER, UK_VOICE)
    launched = await press(control, USER, "task:launch")
    assert launched.text == ("Принято. Начинаю поиск. Найденные варианты пришлю сюда.\n"
                             "Чтобы остановить поиск, нажмите «Остановить поиск» внизу или напишите «стоп».")
    assert launched.keyboard == SEARCH_KEYBOARD and not launched.buttons


@pytest.mark.asyncio
async def test_bottom_keyboard_taps_launch_stop_cancel_and_start_a_new_search() -> None:
    from bot.control_plane.intake import (
        DRAFT_KEYBOARD,
        IDLE_KEYBOARD,
        LAUNCH_KEY,
        NEW_SEARCH_KEY,
        STOP_KEY,
    )

    control, sink, _ = with_ai(FakeAI(LAND))
    assert (await press(control, USER, "mode:real_estate")).keyboard == DRAFT_KEYBOARD
    summary = await say(control, USER, UK_VOICE)
    assert summary.keyboard == TASK_KEYBOARD
    launched = await say(control, USER, LAUNCH_KEY)  # the tap sends its label as a message
    assert launched.text.startswith("Принято. Начинаю поиск.") and launched.keyboard == SEARCH_KEYBOARD
    assert [e.command for e in sink.envelopes] == ["campaign"]
    stopped = await say(control, USER, STOP_KEY)
    assert stopped.keyboard == IDLE_KEYBOARD
    fresh = await say(control, USER, NEW_SEARCH_KEY)
    assert [b.callback_data for b in fresh.buttons][:1] == ["mode:real_estate"]


# --- the OpenRouter client -----------------------------------------------------------------


def _completion(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def _client(*responses: httpx.Response | Exception) -> tuple[OpenRouterUnderstanding, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"body": json.loads(request.content), "auth": request.headers.get("authorization")})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenRouterUnderstanding(api_key="sk-test-secret", model="openai/gpt-4o-mini", timeout_seconds=5,
                                   client=client), sent


@pytest.mark.asyncio
async def test_the_client_sends_a_strict_schema_and_falls_back_to_json_mode_on_400(caplog: pytest.LogCaptureFixture) -> None:
    drifty = "```json\n" + json.dumps({
        "city": "Мадрид", "deal": "покупка", "budget_max": "300 000 €", "currency": "€", "property_type": "участок",
        "target": None, "primary": "земельный участок; покупка", "secondary": None,
        "questions": ["Один?", "Два?", "Три?", "Четыре?", "English?"], "summary_ru": "  Ищем участок.  ", "extra": 1,
    }, ensure_ascii=False) + "\n```"
    client, sent = await _client(httpx.Response(400, json={"error": "no structured outputs"}), _completion(drifty))
    with caplog.at_level(logging.DEBUG):
        result = await client.understand(mode="real_estate", task=UK_VOICE, answers=["a"] * 7, known={"city": None})
    assert sent[0]["body"]["response_format"]["type"] == "json_schema"
    assert sent[0]["body"]["response_format"]["json_schema"]["strict"] is True
    assert sent[1]["body"]["response_format"] == {"type": "json_object"}
    data = json.loads(sent[0]["body"]["messages"][1]["content"].split("\n", 1)[1])
    assert data == {"mode": "real_estate", "task": UK_VOICE, "answers": ["a"] * 5, "known": {}}
    assert sent[0]["auth"] == "Bearer sk-test-secret" and "sk-test-secret" not in caplog.text
    assert (result.city, result.deal, result.budget_max, result.currency, result.property_type) == (
        "Madrid", "sale", 300000, "EUR", "land")
    assert result.primary == ["земельный участок", "покупка"] and result.secondary == []
    assert result.questions == ["Один?", "Два?", "Три?"] and result.summary_ru == "Ищем участок."
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(("response", "code"), [
    (httpx.ReadTimeout("slow"), "timeout"),
    (httpx.ConnectError("down"), "network_error"),
    (httpx.Response(500, json={}), "http_error"),
    (_completion("not json"), "invalid_response"),
    (_completion(json.dumps({"city": "Madrid", "summary_ru": ""})), "invalid_response"),
    (_completion(json.dumps({"city": "Madrid", "summary_ru": "Plot in Madrid"})), "invalid_response"),
    (_completion("[1, 2]"), "invalid_response"),
])
async def test_client_failures_raise_a_safe_error(response: httpx.Response | Exception, code: str) -> None:
    client, _ = await _client(response)
    with pytest.raises(UnderstandingError) as caught:
        await client.understand(mode="real_estate", task="x", answers=[], known={})
    assert caught.value.code == code and "sk-test" not in str(caught.value)


def test_schema_and_prompt() -> None:
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"]) and SCHEMA["additionalProperties"] is False
    assert "enum" not in SCHEMA["properties"]["city"], "any place in the world, not a fixed list"
    assert SCHEMA["properties"]["place"]["required"] == ["es", "ru", "uk", "ru_in", "uk_in", "country"]
    assert "land" in SCHEMA["properties"]["property_type"]["enum"]
    for words in ("ділянка", "terreno", "anywhere in the world", "Ukrainian words do not mean Kyiv", "voice transcript",
                  "never quote", "do\nNOT guess", "«вілл» — это виллы?", "translated, not transliterated"):
        assert words in SYSTEM
    with pytest.raises(ValueError):
        OpenRouterUnderstanding(api_key="", model="m", timeout_seconds=1)


@pytest.mark.parametrize(("raw", "expected"), [
    ({"deal": "не важно"}, ("any", None, None)),
    ({"deal": "rent", "budget_max": 1200.4, "property_type": "habitación"}, ("rent", 1200, "room")),
    ({"budget_max": "1,5k", "property_type": "plot of land"}, (None, 1500, "land")),
    ({"budget_max": -5, "property_type": "null"}, (None, None, None)),
    ({"budget_max": True, "property_type": "villa"}, (None, None, "house")),
])
def test_drift_normalisation(raw: dict[str, Any], expected: tuple[Any, ...]) -> None:
    result = parse_understanding(json.dumps({**raw, "summary_ru": "Задача."}))
    assert (result.deal, result.budget_max, result.property_type) == expected


def test_settings_defaults_and_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_TOKEN", "t")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("OPENROUTER_INTAKE_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_INTAKE_TIMEOUT_SECONDS", raising=False)
    settings = ControlPlaneSettings.from_env()
    assert (settings.intake_model, settings.intake_timeout_seconds) == ("openai/gpt-4o-mini", 20.0)
    monkeypatch.setenv("OPENROUTER_INTAKE_MODEL", "anthropic/claude-3.5-haiku")
    monkeypatch.setenv("OPENROUTER_INTAKE_TIMEOUT_SECONDS", "15")
    settings = ControlPlaneSettings.from_env()
    assert (settings.intake_model, settings.intake_timeout_seconds) == ("anthropic/claude-3.5-haiku", 15.0)
    monkeypatch.setenv("OPENROUTER_INTAKE_TIMEOUT_SECONDS", "999")
    with pytest.raises(ValueError):
        ControlPlaneSettings.from_env()


def test_control_plane_accepts_an_understander() -> None:
    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x")
    from bot.control_plane.store import MemoryControlPlaneStore

    async def sink(_envelope: object) -> None:
        return None

    ai = FakeAI(LAND)
    control = ControlPlane(settings, MemoryControlPlaneStore(), None, sink, understander=ai)
    assert control.intake.understander is ai


def test_the_bottom_keyboard_is_rendered_set_or_removed() -> None:
    from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove

    from bot.control_plane.main import _markup
    from bot.control_plane.models import Button, Reply

    markup = _markup(Reply("x", keyboard=TASK_KEYBOARD))
    assert isinstance(markup, ReplyKeyboardMarkup) and markup.resize_keyboard and markup.is_persistent
    assert [[b.text for b in row] for row in markup.keyboard] == [list(row) for row in TASK_KEYBOARD]
    assert isinstance(_markup(Reply("x", keyboard=())), ReplyKeyboardRemove)
    assert _markup(Reply("x")) is None
    assert isinstance(_markup(Reply("x", (Button("Одобрить", callback_data="near:yes"),))), InlineKeyboardMarkup)


@pytest.mark.asyncio
async def test_an_unclear_word_is_asked_about_before_the_summary() -> None:
    unsure = TaskUnderstanding(city="Ubud, Bali", place={"en": "Ubud, Bali", "ru": "Убуд, Бали", "country": "ID"},
                               target="русскоязычные компании и агенты",
                               questions=["Уточните: «вілл» — это виллы?"], summary_ru="Ищем русскоязычные компании.")
    sure = TaskUnderstanding(city="Ubud, Bali", place={"en": "Ubud, Bali", "ru": "Убуд, Бали", "country": "ID"},
                             target="русскоязычные компании и агенты по управлению виллами",
                             summary_ru="Ищем русскоязычные компании и агентов по управлению виллами в Убуде.")
    ai = FakeAI(unsure, sure)
    control, _, _ = with_ai(ai)
    await press(control, USER, "mode:investors")
    question = await say(control, USER, "надай контакти російськомовних компаній та агентів з управління вілл, Убуд")
    assert question.text.startswith("Уточните: «вілл» — это виллы?") and "Проверьте задачу" not in question.text
    summary = await say(control, USER, "да, виллы")
    assert summary.text.startswith("Проверьте задачу:") and "управлению виллами" in summary.text
    assert "Убуде" in summary.text and "Киев" not in summary.text and ai.calls[1]["answers"] == ["да, виллы"]
