"""The legacy one-shot task understanding client (no network), plus the «Остановить поиск» button.

``understanding.py`` is no longer used by the intake (the interviewer replaced it, see test_interviewer.py); its
client and normalisation stay tested until the module is removed. «Остановить поиск» under the launch reply
does what typing «стоп» does.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from bot.control_plane.intake import SEARCH_KEYBOARD, TASK_KEYBOARD
from bot.control_plane.service import NO_ACTIVE_SEARCH, SEARCH_STOPPED
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.understanding import (
    SCHEMA,
    SYSTEM,
    OpenRouterUnderstanding,
    UnderstandingError,
    parse_understanding,
)
from tests.test_user_intake import (
    OTHER_USER,
    OWNER,
    STRANGER,
    USER,
    enough,
    plane,
    press,
    say,
)
from tests.test_visibility_gate import assert_plain_russian, stoppable

UK_VOICE = ("шукаємо ділянку або участок від тисячі метрів з будинком або без в передмісті Мадрида "
            "5 хвилин до метро на машині для забудови купівля")


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
    control, _, _ = plane()
    await press(control, USER, "mode:real_estate")
    await say(control, USER, "купить участок в Мадриде")
    await enough(control, USER)
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

    control, sink, _ = plane()
    assert (await press(control, USER, "mode:real_estate")).keyboard == DRAFT_KEYBOARD
    await say(control, USER, "купить участок в Мадриде")
    summary = await enough(control, USER)
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
