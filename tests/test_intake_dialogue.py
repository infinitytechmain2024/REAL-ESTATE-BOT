"""The user dialogue in Russian: ask only what is missing, confirm, launch; the transcript stays internal."""

from __future__ import annotations

import re

import pytest

from bot.control_plane.models import IncomingMessage, Reply
from tests.test_user_intake import (
    OPERATOR,
    OWNER,
    USER,
    FakeTranscriber,
    _ids,
    callbacks,
    plane,
    press,
    say,
)

SECRET = "фиолетовый жираф поёт"  # a phrase no reply may ever repeat
VOICE_TASK = f"{SECRET}, снять квартиру в Мадриде до 1200 евро"
TECH_NOISE = re.compile(r"cmd-|Queue|queue|/campaign|cancel <id>|окнами|Языки поиска|Цель:|[A-Za-z]{3,}")


def voice_note(user: int) -> IncomingMessage:
    return IncomingMessage(user, user, next(_ids), voice_file_id="v", voice_size=100, voice_duration_seconds=3)


async def download() -> bytes:
    return b"OggS"


def assert_clean(reply: Reply) -> None:
    """Plain Russian for a user: no ids, commands or English (the mode button emoji titles are Russian)."""
    assert not TECH_NOISE.search(reply.text), reply.text


@pytest.mark.asyncio
@pytest.mark.parametrize("who", [USER, OPERATOR])
async def test_a_voice_transcript_is_never_shown_to_a_user_or_an_operator(who: int) -> None:
    control, sink, outbox = plane(FakeTranscriber(VOICE_TASK))
    await press(control, who, "mode:real_estate")
    reply = await control.handle_voice(voice_note(who), download)
    assert reply is not None and reply.text.startswith("Проверьте задачу")
    assert callbacks(reply) == ["task:launch", "task:edit", "task:cancel"]  # voice keeps the buttons
    launched = await control.handle_callback(who, "task:launch", "Ann", "ann", chat_id=who)
    everything = [reply.text, launched.text, *(r.text for _, r in outbox.sent)]
    for text in everything:
        assert SECRET not in text and "Transcript" not in text and "Распознано" not in text, text
    assert len(sink.envelopes) == 1  # the transcript still drives the task internally
    assert SECRET in sink.envelopes[0].arguments


@pytest.mark.asyncio
async def test_a_voice_question_for_a_user_does_not_echo_the_transcript() -> None:
    control, _, _ = plane(FakeTranscriber(f"{SECRET} квартиры"))
    await press(control, USER, "mode:real_estate")
    reply = await control.handle_voice(voice_note(USER), download)
    assert reply is not None and reply.text.startswith("Уточните") and SECRET not in reply.text
    answer = await control.handle_voice(voice_note(USER), download)  # an unusable spoken answer
    assert answer is not None and SECRET not in answer.text and answer.text.startswith("Не понял")


@pytest.mark.asyncio
async def test_the_owner_still_sees_the_transcript() -> None:
    control, _, _ = plane(FakeTranscriber(VOICE_TASK))
    await press(control, OWNER, "mode:real_estate")
    reply = await control.handle_voice(voice_note(OWNER), download)
    assert reply is not None and reply.text.startswith("Transcript (ru") and SECRET in reply.text
    assert "Проверьте задачу" in reply.text and "task:launch" in callbacks(reply)


@pytest.mark.asyncio
async def test_voice_problems_are_explained_in_russian_to_users() -> None:
    control, _, _ = plane(None)
    await press(control, USER, "mode:real_estate")
    reply = await control.handle_voice(voice_note(USER), download)
    assert reply is not None and reply.text == "Голосовые сообщения сейчас недоступны. Напишите задачу текстом."


@pytest.mark.asyncio
async def test_a_clear_request_skips_every_question() -> None:
    control, sink, _ = plane()
    await press(control, USER, "mode:real_estate")
    reply = await say(control, USER, "снять квартиру в Мадриде до 1200 €")
    assert reply.text.splitlines()[:6] == [
        "Проверьте задачу:", "Режим: 🏡 Участки и объекты", "Город: Мадрид", "Сделка: аренда", "Тип: квартира",
        "Бюджет: до 1200 €",
    ]
    assert [b.text for b in reply.buttons] == ["Запустить", "Изменить", "Отмена"]
    assert "?" not in reply.text.replace("Всё верно?", "")
    assert sink.envelopes == []


@pytest.mark.asyncio
async def test_only_the_missing_budget_is_asked() -> None:
    control, _, _ = plane()
    await press(control, USER, "mode:real_estate")
    question = await say(control, USER, "снять квартиру в Мадриде")
    assert question.text == "Какой бюджет? Например, до 1200 €. Или нажмите «Пропустить»."
    assert callbacks(question) == ["task:budget:skip", "task:cancel"]
    summary = await say(control, USER, "до 900 евро")
    assert "Город: Мадрид" in summary.text and "Сделка: аренда" in summary.text and "Бюджет: до 900 €" in summary.text


@pytest.mark.asyncio
async def test_only_the_missing_city_is_asked() -> None:
    control, _, _ = plane()
    await press(control, USER, "mode:real_estate")
    question = await say(control, USER, "купить квартиру до 300 000 €")
    assert question.text == "В каком городе искать? Выберите или напишите."
    assert "Бюджет" not in question.text and "Аренда" not in question.text


@pytest.mark.asyncio
async def test_several_missing_fields_are_asked_in_one_message_and_answered_in_one_reply() -> None:
    control, _, _ = plane()
    await press(control, USER, "mode:real_estate")
    question = await say(control, USER, "ищу квартиру")
    assert question.text.splitlines()[:4] == [
        "Уточните, пожалуйста:", "1. В каком городе искать?", "2. Аренда или покупка?", "3. Какой бюджет? Например, до 1200 €.",
    ]
    assert_clean(question)
    summary = await say(control, USER, "Валенсия, аренда, до 1000 €")
    assert summary.text.startswith("Проверьте задачу")
    assert "Город: Валенсия" in summary.text and "Сделка: аренда" in summary.text and "Бюджет: до 1000 €" in summary.text


@pytest.mark.asyncio
async def test_investors_ask_for_the_city_and_who_to_look_for() -> None:
    control, _, _ = plane()
    await press(control, USER, "mode:investors")
    question = await say(control, USER, "нужны контакты")
    assert question.text.splitlines()[1:3] == ["1. В каком городе искать?", "2. Кого ищете? Например: инвесторы в недвижимость, стартапы, бизнес-ангелы."]
    only_who = await say(control, USER, "Барселона")
    assert only_who.text.startswith("Кого ищете?") and "городе" not in only_who.text
    summary = await say(control, USER, "бизнес-ангелы и фонды")
    assert "Город: Барселона" in summary.text and "Кого ищем: бизнес-ангелы, фонды" in summary.text
    # A complete investors task asks nothing.
    assert (await say(control, USER, "инвесторы в недвижимость в Малаге")).text.startswith("Проверьте задачу")


@pytest.mark.asyncio
async def test_launch_queues_the_campaign_and_the_user_reply_is_plain_russian() -> None:
    control, sink, _ = plane()
    await press(control, USER, "mode:real_estate")
    summary = await say(control, USER, "снять квартиру в Мадриде до 1200 €")
    assert_clean(summary)
    launched = await press(control, USER, "task:launch")
    assert launched.text == ("Принято. Начинаю поиск. Найденные варианты пришлю сюда.\n"
                             "Чтобы остановить поиск, нажмите кнопку ниже или напишите «стоп».")
    assert [(b.text, b.callback_data) for b in launched.buttons] == [("Остановить поиск", "search:stop")]
    assert_clean(launched)
    [envelope] = sink.envelopes
    assert envelope.command == "campaign" and envelope.arguments.startswith("mode=real_estate city=Madrid ")
    # The owner keeps the technical trace.
    await press(control, OWNER, "mode:real_estate")
    await say(control, OWNER, "снять квартиру в Мадриде до 1200 €")
    owner = await press(control, OWNER, "task:launch")
    assert owner.text.startswith("Принято. Начинаю поиск.") and "Queue id: cmd-2" in owner.text


@pytest.mark.asyncio
async def test_edit_and_cancel_still_work() -> None:
    control, sink, _ = plane()
    await press(control, USER, "mode:real_estate")
    await say(control, USER, "снять квартиру в Мадриде до 1200 €")
    edit = await press(control, USER, "task:edit")
    assert "Опишите задачу заново" in edit.text
    assert "Город: Севилья" in (await say(control, USER, "снять квартиру в Севилье до 800 €")).text
    cancel = await press(control, USER, "task:cancel")
    assert "Черновик удалён" in cancel.text
    assert "устарела" in (await press(control, USER, "task:launch")).text
    await say(control, USER, "ищу квартиру")  # cancel in the middle of the questions
    assert "Черновик удалён" in (await say(control, USER, "Отмена")).text
    for reply in (edit, cancel):
        assert_clean(reply)
    assert sink.envelopes == []
