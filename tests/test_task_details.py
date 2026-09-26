"""«Проверьте задачу» keeps the wishes of the task, as normalised Russian phrases."""

from __future__ import annotations

import pytest

from bot.campaign.architect import find_places
from bot.control_plane.details import details, property_type
from bot.control_plane.intake import Draft, summary

UK = ("Шукаємо ділянку від тисячі метрів квадратних з будинком або без, в передмісті Мадрида, "
      "близько до метро 5 хвилин на машині, ділянка для забудови. Покупка.")
RU = "Ищем участок от 1000 м² с домом или без в пригороде Мадрида, до метро 5 минут на машине, под застройку, покупка"


@pytest.mark.parametrize("text", [UK, RU])
def test_land_task_wishes(text: str) -> None:
    assert property_type(text) == "участок"
    assert details(text) == ["площадь от 1 000 м²", "с домом или без", "до метро 5 мин на машине", "под застройку", "пригород"]


def test_units_and_plain_cases() -> None:
    assert details("участок 15 соток") == ["площадь 1 500 м²"]
    assert details("квартира рядом с метро") == ["рядом с метро"]
    assert details("снять квартиру в Мадриде до 1200 €") == []
    assert property_type("снять квартиру") == "квартира"


def test_ukrainian_city_spellings() -> None:
    for text in ("у Мадріді", "під Мадридом", "в передмісті Мадрида", "Madryd"):
        assert find_places(text) == ["Madrid"], text


def test_summary_shows_type_and_wishes_but_not_the_words() -> None:
    draft = Draft(1, 1, "real_estate", task=UK)
    reply = summary(draft, draft.plan())
    assert "Город: Мадрид" in reply.text and "Сделка: покупка" in reply.text and "Тип: участок" in reply.text
    assert "Пожелания: площадь от 1 000 м², с домом или без, до метро 5 мин на машине, под застройку, пригород" in reply.text
    assert "Шукаємо" not in reply.text and "передмісті" not in reply.text
