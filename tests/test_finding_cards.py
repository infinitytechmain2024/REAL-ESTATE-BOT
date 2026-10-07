"""Finding cards: Russian labels and text, fields chosen by the task, always the original post."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from bot.analysis_pipeline.cards import MAX_CARD_CHARS, CardTask, confidence_words, render_card
from bot.analysis_pipeline.formatters import finding_payload, real_estate
from bot.analysis_pipeline.models import Evidence
from bot.analysis_pipeline.openrouter import PROMPT_VERSION, RESULT_SCHEMA, parse_result
from bot.analysis_pipeline.pipeline import AnalysisPipeline
from bot.campaign.architect import plan_campaign
from bot.campaign.models import Campaign
from bot.campaign.runner import finding_card
from bot.campaign.runs import StreamFinding

ES_POST = (
    "Alquilo piso de 2 habitaciones en Lavapiés, Madrid. 1.200 € al mes, gastos aparte. "
    "Disponible desde octubre, sin mascotas. Contacto por privado."
)
RU_POST = "Сдаю комнату в районе Usera, Мадрид, 400 евро в месяц, всё включено, для одной девушки."
LINK = "https://www.facebook.com/groups/pisosmadrid/posts/123/"
ENGLISH_LABELS = re.compile(r"\b(location|price|confidence|summary|signal|links?|original post|not stated)\b", re.I)


def es_payload(**extra) -> dict:
    return {
        "schema_version": "analysis-v3",
        "summary": "Piso de 2 habitaciones en Lavapiés por 1.200 € al mes.",
        "summary_ru": "Сдаётся квартира с двумя спальнями в Лавапьес, Мадрид, за 1200 € в месяц, коммунальные отдельно. "
                      "Свободна с октября, без животных.",
        "source_language": "es",
        "location": "Madrid, Lavapiés",
        "price_signals": ["1.200 € al mes"],
        "price_amount": 1200,
        "price_currency": "EUR",
        "deal_type": "rent",
        "property_type": "apartment",
        "rooms": 2,
        "who": None,
        "original_post_link": LINK,
        "related_links": [],
        **extra,
    }


def test_spanish_source_gives_russian_card_and_the_spanish_original() -> None:
    card = render_card(es_payload(), original=ES_POST, confidence=0.91)
    assert card.startswith("🏠 Недвижимость")
    assert "Кратко: Сдаётся квартира с двумя спальнями" in card
    assert "Цена: 1 200 € в месяц" in card and "Локация: Madrid, Lavapiés" in card
    assert "Сделка: аренда" in card and "Тип: квартира" in card and "Уверенность: высокая" in card
    assert f"Ссылка: {LINK}" in card and "Источник: Facebook" in card
    assert card.endswith("Язык оригинала: испанский")
    # The post itself (with Facebook's own buttons) is never quoted.
    assert ES_POST not in card and "Оригинал (" not in card


def test_russian_source_gives_russian_body_and_russian_original() -> None:
    payload = {
        "schema_version": "analysis-v3", "summary": "Комната в Усере за 400 евро.",
        "summary_ru": "Сдаётся комната в районе Усера за 400 € в месяц, всё включено.", "source_language": "ru",
        "location": "Мадрид, Усера", "price_amount": 400, "price_currency": "EUR", "deal_type": "rent",
        "property_type": "room", "original_post_link": LINK,
    }
    card = render_card(payload, original=RU_POST, confidence=0.6)
    assert "Кратко: Сдаётся комната в районе Усера" in card and "Уверенность: средняя" in card
    assert card.endswith("Язык оригинала: русский") and RU_POST not in card


def test_no_english_labels_or_raw_numbers_anywhere() -> None:
    cards = [
        render_card(es_payload(), original=ES_POST, confidence=0.91),
        render_card(es_payload(who="Fondo Norte"), original=ES_POST, vertical="investors", confidence=0.3),
        render_card({"summary": "x", "price_signals": ["450 EUR/month"], "original_post_link": LINK}, original=ES_POST),
    ]
    for card in cards:
        head = card
        assert not ENGLISH_LABELS.search(head), head
        assert "0.91" not in head and "91%" not in head and "analysis" not in head and "None" not in head
    assert "Уверенность: низкая" in cards[1] and "Кто: Fondo Norte" in cards[1] and cards[1].startswith("📈 Инвестиции")


def test_the_task_decides_the_fields_and_unknown_ones_are_left_out() -> None:
    sparse = {"summary_ru": "Сдаётся квартира в Мадриде.", "source_language": "es", "price_amount": 950,
              "price_currency": "EUR", "location": "Madrid", "original_post_link": LINK}
    budget = render_card(sparse, original=ES_POST, task=CardTask(max_price=1000))
    lines = budget.splitlines()
    assert lines[1] == "Цена: 950 €"  # a budget task shows the price first
    for missing in ("Сделка:", "Тип:", "Комнаты:", "Кто:", "Уверенность:", "не указано", "not stated"):
        assert missing not in budget
    rent = render_card(es_payload(), original=ES_POST, task=CardTask(deal="rent"))
    assert rent.splitlines()[1:3] == ["Сделка: аренда", "Тип: квартира"]
    both = render_card(es_payload(), original=ES_POST, task=CardTask(deal="rent", max_price=1300))
    assert both.splitlines()[1:4] == ["Цена: 1 200 € в месяц", "Сделка: аренда", "Тип: квартира"]
    investors = render_card(es_payload(who="Ana García, Norte Capital"), original=ES_POST, vertical="investors")
    assert investors.splitlines()[1:3] == ["Кто: Ana García, Norte Capital", "Локация: Madrid, Lavapiés"]
    assert "Тип:" not in investors and "Сумма: 1 200 €" in investors


def test_an_old_payload_without_the_new_fields_still_renders() -> None:
    old = {
        "schema_version": "analysis-v1", "summary": "Piso en Madrid por 900 euros al mes.", "location": "Madrid",
        "price_signals": ["900 EUR/month"], "original_post_link": LINK, "related_links": [],
        "formatted": "🏠 Real Estate proposition\nLocation: Madrid",
    }
    card = render_card(old, original="Alquilo piso en Madrid, 900 euros al mes.", language="es", confidence=0.85)
    assert "Цена: 900 EUR/month" in card and "Локация: Madrid" in card and "Кратко: Piso en Madrid" in card
    assert card.endswith("Язык оригинала: испанский") and "Alquilo" not in card
    assert "Real Estate" not in card and "Location" not in card
    # No original text stored: the summary stands in; the language is guessed from it.
    assert render_card({"summary": "Alquilo habitación en Madrid"}, language="unknown").endswith(
        "Язык оригинала: испанский")
    # A Ukrainian post the keyword filter called "ru".
    assert "Язык оригинала: украинский" in render_card({"summary": "x"}, original="Здаю кімнату в Мадриді", language="ru")


def test_a_long_post_is_trimmed_to_one_telegram_message() -> None:
    long_post = "Alquilo piso en Madrid. " * 600
    card = render_card(es_payload(summary_ru="Очень длинный текст. " * 200), original=long_post)
    assert len(card) <= MAX_CARD_CHARS and "Alquilo piso" not in card
    assert len(f"{card}\n\n🔎 Найдено: 100 · ищу дальше") <= 4096


def test_confidence_words() -> None:
    assert [confidence_words(x) for x in (0.95, 0.8, 0.5, 0.2, None, "x")] == [
        "высокая", "высокая", "средняя", "низкая", None, None]


def test_parse_result_accepts_the_new_schema_and_drifted_variants() -> None:
    assert PROMPT_VERSION == "analysis-v6"
    assert set(RESULT_SCHEMA["required"]) == set(RESULT_SCHEMA["properties"])
    assert {"summary_ru", "source_language", "price_amount", "price_currency"} <= set(RESULT_SCHEMA["required"])
    assert {"listing_kind", "country", "area_m2"} <= set(RESULT_SCHEMA["required"])
    assert RESULT_SCHEMA["properties"]["listing_kind"]["enum"] == ["offer", "catalog", "wanted", "other"]
    exact = {
        "relevant": True, "confidence": 0.9, "summary": "Piso en Lavapiés", "location": "Madrid", "price_signals": ["1.200 €"],
        "related_links": [], "category": "real_estate", "reason": "offer", "summary_ru": "Квартира в Лавапьес",
        "source_language": "es", "price_amount": 1200, "price_currency": "EUR", "deal_type": "rent",
        "property_type": "apartment", "rooms": 2, "who": None,
    }
    result = parse_result(json.dumps(exact, ensure_ascii=False))
    assert (result.summary_ru, result.source_language, result.price_amount, result.price_currency) == (
        "Квартира в Лавапьес", "es", 1200.0, "EUR")
    assert (result.deal_type, result.property_type, result.rooms, result.who) == ("rent", "apartment", 2, None)

    drifted = {**exact, "source_language": "Spanish", "price_amount": "1.200 €", "price_currency": "euros",
               "deal_type": "Alquiler", "property_type": "Piso", "rooms": "2", "who": ""}
    result = parse_result(json.dumps(drifted, ensure_ascii=False))
    assert (result.source_language, result.price_amount, result.price_currency) == ("es", 1200.0, "EUR")
    assert (result.deal_type, result.property_type, result.rooms, result.who) == ("rent", "apartment", 2, None)
    odd = parse_result(json.dumps({**exact, "source_language": "ES-es", "price_amount": "a consultar",
                                   "price_currency": "chf", "deal_type": "swap", "property_type": "boat", "rooms": -1}))
    assert (odd.source_language, odd.price_amount, odd.price_currency) == ("es", None, "CHF")
    assert (odd.deal_type, odd.property_type, odd.rooms) == (None, None, None)
    # An analysis-v2 answer (json_object fallback without the new keys) is still accepted.
    legacy = {k: exact[k] for k in ("relevant", "confidence", "summary", "location", "price_signals", "related_links",
                                    "category", "reason")}
    result = parse_result(json.dumps(legacy))
    assert result.summary_ru is None and result.price_amount is None and result.source_language is None
    assert (result.listing_kind, result.country, result.area_m2) == (None, None, None)


def test_analysis_v4_listing_kind_country_and_area_with_drift() -> None:
    base = {
        "relevant": True, "confidence": 0.9, "summary": "Parcela", "location": "Boadilla del Monte", "price_signals": [],
        "related_links": [], "category": "real_estate", "reason": "offer",
    }
    exact = parse_result(json.dumps({**base, "listing_kind": "catalog", "country": "ES", "area_m2": 2500}))
    assert (exact.listing_kind, exact.country, exact.area_m2) == ("catalog", "ES", 2500.0)
    drifted = parse_result(json.dumps({**base, "listing_kind": "Search results page", "country": "Spain",
                                       "area_m2": "2.500 m²"}))
    assert (drifted.listing_kind, drifted.country, drifted.area_m2) == ("catalog", "ES", 2500.0)
    wanted = parse_result(json.dumps({**base, "listing_kind": "WANTED", "country": "uk", "area_m2": -3}))
    assert (wanted.listing_kind, wanted.country, wanted.area_m2) == ("wanted", "GB", None)
    odd = parse_result(json.dumps({**base, "listing_kind": "banana", "country": "Atlantis", "area_m2": "grande"}))
    assert (odd.listing_kind, odd.country, odd.area_m2) == ("other", None, None)
    single = parse_result(json.dumps({**base, "listing_kind": "a single listing", "country": None, "area_m2": None}))
    assert single.listing_kind == "offer"


def test_an_old_payload_without_the_v4_fields_still_renders_and_is_an_offer() -> None:
    from bot.campaign.tolerance import Request, classify

    old = es_payload()
    assert "listing_kind" not in old
    assert render_card(old, original=ES_POST).startswith("🏠 Недвижимость")
    assert classify(old, Request(amount=1300, deal="rent", location="Madrid")).bucket == "exact"


class SpanishAnalyzer:
    async def analyze(self, evidence, vertical, task_hint=None):
        return parse_result(json.dumps({
            "relevant": True, "confidence": 0.88, "summary": "Piso en Lavapiés", "location": "Madrid, Lavapiés",
            "price_signals": ["1.200 € al mes"], "related_links": [], "category": vertical, "reason": "offer",
            "summary_ru": "Сдаётся квартира с двумя спальнями в Лавапьес за 1200 € в месяц.", "source_language": "es",
            "price_amount": 1200, "price_currency": "EUR", "deal_type": "rent", "property_type": "apartment",
            "rooms": 2, "who": None, "listing_kind": "offer", "country": "es", "area_m2": "85 m²",
        }, ensure_ascii=False))


@pytest.mark.asyncio
async def test_pipeline_digest_and_campaign_stream_use_the_same_card() -> None:
    evidence = Evidence(post_id="p1", source_id="s1", canonical_url=LINK, text=ES_POST,
                        published_at=datetime.now(UTC))
    outcome = await AnalysisPipeline(SpanishAnalyzer()).process(evidence, "real_estate")
    assert outcome.accepted and outcome.formatted == real_estate(outcome.result, evidence, "es")
    assert outcome.formatted.endswith("Язык оригинала: испанский")
    payload = {**finding_payload(outcome.result, evidence), "formatted": outcome.formatted}
    assert payload["schema_version"] == "analysis-v6" and payload["price_amount"] == 1200.0
    assert (payload["listing_kind"], payload["country"], payload["area_m2"]) == ("offer", "ES", 85.0)

    plan = plan_campaign("Найди квартиры в аренду в Мадриде до 1300 евро")
    campaign = Campaign("c1", plan, "running", 1, 1, "goal", None, None, datetime.now(UTC))
    stored = json.loads(json.dumps(payload))  # as read back from jsonb
    card = finding_card(campaign, StreamFinding("f1", outcome.formatted, stored, ES_POST, "es", 0.88, "real_estate"))
    assert plan.constraints["max_price"] == 1300 and plan.constraints["deal"] == "rent"
    assert card.splitlines()[1:4] == ["Цена: 1 200 € в месяц", "Сделка: аренда", "Тип: квартира"]
    assert "Кратко: Сдаётся квартира" in card and card.endswith("Язык оригинала: испанский")
    # A finding without a payload (legacy memory store) is sent as its text.
    assert finding_card(campaign, StreamFinding("f2", "🏠 text")) == "🏠 text"


def test_card_shows_v6_fields_and_old_payloads_still_render() -> None:
    from bot.analysis_pipeline.cards import render_card

    card = render_card({"schema_version": "analysis-v6", "summary_ru": "Квартира", "district": "Лавапьес", "floor": 3,
                        "features": ["terraza", "ascensor"], "deal_type": "rent"}, vertical="real_estate")
    assert "Район: Лавапьес" in card and "Этаж: 3" in card and "Особенности: terraza, ascensor" in card
    assert "Район" not in render_card({"schema_version": "analysis-v4", "summary_ru": "Квартира"}, vertical="real_estate")
