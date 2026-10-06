"""The user dialogue in Russian: ask one missing thing at a time, confirm, launch; a voice task is shown back once."""

from __future__ import annotations

import re

import pytest

from bot.control_plane.intake import DRAFT_KEYBOARD, SEARCH_KEYBOARD, TASK_KEYBOARD
from bot.control_plane.models import IncomingMessage, Reply
from tests.test_user_intake import (
    OPERATOR,
    OWNER,
    USER,
    FakeTranscriber,
    _ids,
    callbacks,
    enough,
    plane,
    press,
    say,
)

SECRET = "фиолетовый жираф поёт"  # shown back once after a voice note, never after that
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
async def test_a_voice_task_is_shown_back_once_to_a_user_or_an_operator(who: int) -> None:
    control, sink, outbox = plane(FakeTranscriber(VOICE_TASK))
    await press(control, who, "mode:real_estate")
    reply = await control.handle_voice(voice_note(who), download)
    assert reply is not None and reply.text.startswith(f"Я услышал: «{VOICE_TASK}»\n\nПонял: Мадрид")
    assert reply.text.count(SECRET) == 1 and "Transcript" not in reply.text
    card = await enough(control, who)
    assert card.text.startswith("Проверьте задачу") and SECRET not in card.text
    assert card.keyboard == TASK_KEYBOARD and not card.buttons
    launched = await control.handle_callback(who, "task:launch", "Ann", "ann", chat_id=who)
    for text in [launched.text, *(r.text for _, r in outbox.sent)]:
        assert SECRET not in text and "Transcript" not in text and "Распознано" not in text, text
    assert len(sink.envelopes) == 1  # the transcript still drives the task internally
    assert SECRET in sink.envelopes[0].arguments


@pytest.mark.asyncio
async def test_each_voice_note_is_echoed_once_and_an_unusable_answer_is_not_understood() -> None:
    control, _, _ = plane(FakeTranscriber(f"{SECRET} квартиры"))
    await press(control, USER, "mode:real_estate")
    reply = await control.handle_voice(voice_note(USER), download)
    assert reply is not None and reply.text.startswith(f"Я услышал: «{SECRET} квартиры»") and reply.text.count(SECRET) == 1
    assert "В каком городе" in reply.text
    answer = await control.handle_voice(voice_note(USER), download)  # an unusable spoken answer
    assert answer is not None and answer.text.count(SECRET) == 1 and "Не понял город" in answer.text


@pytest.mark.asyncio
async def test_the_owner_still_sees_the_transcript() -> None:
    control, _, _ = plane(FakeTranscriber(VOICE_TASK))
    await press(control, OWNER, "mode:real_estate")
    reply = await control.handle_voice(voice_note(OWNER), download)
    assert reply is not None and reply.text.startswith("Transcript (ru") and SECRET in reply.text
    assert "Я услышал" not in reply.text and "Сколько комнат" in reply.text and reply.keyboard == DRAFT_KEYBOARD


@pytest.mark.asyncio
async def test_voice_problems_are_explained_in_russian_to_users() -> None:
    control, _, _ = plane(None)
    await press(control, USER, "mode:real_estate")
    reply = await control.handle_voice(voice_note(USER), download)
    assert reply is not None and reply.text == "Голосовые сообщения сейчас недоступны. Напишите задачу текстом."


@pytest.mark.asyncio
async def test_a_clear_request_asks_only_for_the_rooms() -> None:
    control, sink, _ = plane()
    await press(control, USER, "mode:real_estate")
    question = await say(control, USER, "снять квартиру в Мадриде до 1200 €")
    assert question.text.endswith("Сколько комнат нужно? Например: студия, 2, от 2 до 3.")
    reply = await enough(control, USER)
    assert reply.text.splitlines()[:6] == [
        "Проверьте задачу:", "Режим: 🏡 Участки и объекты", "Город: Мадрид", "Сделка: аренда", "Тип: квартира",
        "Бюджет: до 1200 €",
    ]
    assert reply.keyboard == TASK_KEYBOARD and not reply.buttons
    assert "?" not in reply.text.replace("Всё верно?", "")
    assert sink.envelopes == []


@pytest.mark.asyncio
async def test_only_the_missing_budget_is_asked() -> None:
    control, _, _ = plane()
    await press(control, USER, "mode:real_estate")
    question = await say(control, USER, "снять квартиру в Мадриде")
    assert question.text == "Понял: Мадрид, аренда, квартира\n\nКакой бюджет? Например, до 1200 € или «от 500 до 900 €»."
    assert callbacks(question) == ["task:skip", "task:enough", "task:cancel"]
    assert "Сколько комнат" in (await say(control, USER, "до 900 евро")).text
    summary = await enough(control, USER)
    assert "Город: Мадрид" in summary.text and "Сделка: аренда" in summary.text and "Бюджет: до 900 €" in summary.text


@pytest.mark.asyncio
async def test_only_the_missing_city_is_asked() -> None:
    control, _, _ = plane()
    await press(control, USER, "mode:real_estate")
    question = await say(control, USER, "купить квартиру до 300 000 €")
    assert question.text.endswith("В каком городе искать? Напишите город, район или регион в любой стране.")
    assert "Аренда" not in question.text and "Какой бюджет" not in question.text


@pytest.mark.asyncio
async def test_several_missing_fields_are_asked_one_by_one_and_one_reply_may_answer_many() -> None:
    control, _, _ = plane()
    await press(control, USER, "mode:real_estate")
    question = await say(control, USER, "ищу квартиру")
    assert question.text == "Понял: квартира\n\nВ каком городе искать? Напишите город, район или регион в любой стране."
    assert_clean(question)
    rooms = await say(control, USER, "Валенсия, аренда, до 1000 €")  # city, deal and budget in one reply
    assert rooms.text.startswith("Понял: Валенсия, аренда, до 1000 €") and "Сколько комнат" in rooms.text
    summary = await enough(control, USER)
    assert summary.text.startswith("Проверьте задачу")
    assert "Город: Валенсия" in summary.text and "Сделка: аренда" in summary.text and "Бюджет: до 1000 €" in summary.text


@pytest.mark.asyncio
async def test_investors_ask_for_the_city_who_the_ticket_and_the_role() -> None:
    control, _, _ = plane()
    await press(control, USER, "mode:investors")
    question = await say(control, USER, "нужны контакты")
    assert question.text.startswith("В каком городе искать?")
    only_who = await say(control, USER, "Барселона")
    assert only_who.text.endswith("Кого ищете? Например: инвесторы в недвижимость, стартапы, бизнес-ангелы.")
    ticket = await say(control, USER, "бизнес-ангелы и фонды")
    assert "размер вложения" in ticket.text
    role = await say(control, USER, "до 500 тыс €")
    assert "Вы ищете деньги для своего проекта" in role.text
    summary = await press(control, USER, "task:role:deploying")
    assert "Город: Барселона" in summary.text and "Кого ищем: частные инвесторы, фонды" in summary.text
    assert "Тикет: до 500000 €" in summary.text and "Ваша роль: вкладываю деньги" in summary.text
    # Without a ticket the ticket question is asked, even when everything else is said.
    other, _, _ = plane()
    await press(other, USER, "mode:investors")
    assert "размер вложения" in (await say(other, USER, "инвесторы в недвижимость в Малаге")).text


@pytest.mark.asyncio
async def test_launch_queues_the_campaign_and_the_user_reply_is_plain_russian() -> None:
    control, sink, _ = plane()
    await press(control, USER, "mode:real_estate")
    await say(control, USER, "снять квартиру в Мадриде до 1200 €")
    summary = await enough(control, USER)
    assert_clean(summary)
    launched = await press(control, USER, "task:launch")
    assert launched.text == ("Принято. Начинаю поиск. Найденные варианты пришлю сюда.\n"
                             "Чтобы остановить поиск, нажмите «Остановить поиск» внизу или напишите «стоп».")
    assert launched.keyboard == SEARCH_KEYBOARD and not launched.buttons
    assert_clean(launched)
    [envelope] = sink.envelopes
    assert envelope.command == "campaign" and envelope.arguments.startswith("mode=real_estate city=Madrid spec=")
    # The owner keeps the technical trace.
    await press(control, OWNER, "mode:real_estate")
    await say(control, OWNER, "снять квартиру в Мадриде до 1200 €")
    await enough(control, OWNER)
    owner = await press(control, OWNER, "task:launch")
    assert owner.text.startswith("Принято. Начинаю поиск.") and "Queue id: cmd-2" in owner.text


@pytest.mark.asyncio
async def test_edit_changes_one_field_and_cancel_still_works() -> None:
    control, sink, _ = plane()
    await press(control, USER, "mode:real_estate")
    await say(control, USER, "снять квартиру в Мадриде до 1200 €")
    await enough(control, USER)
    edit = await press(control, USER, "task:edit")
    assert edit.text == "Что изменить?"
    assert [b.text for b in edit.buttons] == ["Место", "Сделка", "Тип", "Бюджет", "Комнаты", "Площадь", "Районы", "Обязательно",
                                              "Пожелания", "Исключить", "Источники", "Назад"]
    prompt = await press(control, USER, "task:field:budget")
    assert prompt.text.startswith("Бюджет: напишите новое значение.") and callbacks(prompt) == ["task:back"]
    card = await say(control, USER, "до 800 евро")  # one field changes, the rest of the task stays
    assert card.text.startswith("Проверьте задачу") and card.keyboard == TASK_KEYBOARD
    for line in ("Город: Мадрид", "Сделка: аренда", "Тип: квартира", "Бюджет: до 800 €"):
        assert line in card.text, line
    # A typed correction under the card is a correction, not a new task.
    assert "Город: Севилья" in (await say(control, USER, "место: Севилья")).text
    assert "Бюджет: до 800 €" in (await press(control, USER, "task:back")).text
    cancel = await press(control, USER, "task:cancel")
    assert "Черновик удалён" in cancel.text
    assert "устарела" in (await press(control, USER, "task:launch")).text
    await say(control, USER, "ищу квартиру")  # cancel in the middle of the questions
    assert "Черновик удалён" in (await say(control, USER, "Отмена")).text
    for reply in (edit, cancel):
        assert_clean(reply)
    assert sink.envelopes == []