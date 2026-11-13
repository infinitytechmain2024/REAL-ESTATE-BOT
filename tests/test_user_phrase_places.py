"""Q28 USER_PHRASE_PLACES: Latin place names survive in the deviation phrase users read, Latin prose never does."""

from __future__ import annotations

import json

import httpx
import pytest

from bot.agents.reviewer import OpenRouterReviewer, parse_review
from bot.campaign.relevance import (
    OpenRouterRelevanceJudge,
    ReviewerJudge,
    parse_relevance,
    user_phrase,
)
from bot.campaign.settings import CampaignRunnerSettings

N1 = "не в центре (Vista Alegre, Карабанчель)"  # the reviewer's phrase from run N1
PROSE = ("outside the center", "near the center", "a 5 minutos", "не в центре, near the center",
         "дальше от метро, a 5 minutos", "не в центре, Near The Center", "farther from the metro", "m² 2000",
         "не в центре (VISTA ALEGRE)", "дальше от de")
PLACES = ("ближе к Playa de San Juan", "район l'Eixample", "Calle de Alcalá, дальше от метро", "район Centro")


# Q28 review: the phrases the reviewer may write. Spanish real-estate words and prose drop the phrase; place names
# (articles «A»/«O», connector chains «de la», «i la», «·» and diacritics) pass with the flag.
REVIEW_PROSE = (
    "Close to metro, дальше", "дальше, Close to metro", "Sin ascensor, этаж 3", "этаж 3, Sin ascensor",
    "Precio negociable, дороже", "дороже, Precio negociable", "дороже: Negociable", "без лифта (Sin ascensor)",
    "Garaje нет", "нет: Garaje", "нет Terraza", "без Terraza и Garaje", "Exterior, дальше", "дальше от Metro",
    "Alquiler вместо продажи", "Obra Nueva, дороже", "Piso вместо дома", "Chalet вместо квартиры", "Near центр",
    "Very far от центра", "Without лифта", "Studio вместо квартиры", "Duplex вместо", "Ático вместо дома",
    "Bajo, первый этаж", "Planta Baja", "Planta Baja вместо", "дороже (IVA)", "дороже + IVA", "OK, дороже",
    "Parking нет", "Piscina нет", "этаж Bajo", "Reformar нужно", "дороже Total", "Precio Negociable дороже",
    "район Sin", "район Y", "дороже, Is Negotiable", "дороже, A 5 Minutos", "район de la", "район Sant i Карабанчель",
    "Amueblado, дороже", "без Balcón", "без Jardín", "Estudio вместо квартиры", "Trastero нет", "Venta вместо аренды",
    "Interior, темнее", "Wifi нет",
)
REVIEW_PLACES = (
    "Sant Martí, дальше", "район L'Hospitalet", "район Puerta del Sol", "район Ciudad Lineal", "район A Coruña",
    "район Sant Andreu de Palomar", "район Les Corts", "район Sants-Montjuïc", "район Hospitalet de Llobregat",
    "район El Puerto de Santa María", "район Vall d'Hebron", "район Sant Gervasi i la Bonanova", "Coruña, дальше",
    "район La Latina", "район Fuencarral-El Pardo", "дальше от Calle Mayor", "дальше от C/ Mayor",
    "Avda. de América рядом", "Paseo de la Castellana рядом", "район Lavapiés", "ул. Gran Vía", "район O Grove",
    "район Puerto de la Cruz", "район Playa de las Américas", "район A Estrada", "район Paral·lel", "Ávila, дальше",
)


def test_every_reviewer_phrase_is_judged_right() -> None:
    for phrase in REVIEW_PROSE:
        assert user_phrase(phrase, places=True) is None, phrase
    for phrase in REVIEW_PLACES:
        assert user_phrase(phrase, places=True) == phrase, phrase
    assert user_phrase("В районе Retiro", places=True) == "в районе Retiro"  # a Russian first word is lowercased
    for phrase in REVIEW_PROSE + REVIEW_PLACES:
        assert user_phrase(phrase) is None, phrase  # without the flag: as before


def review_answer(deviation: str) -> str:
    return json.dumps({"criteria": [{"name": "place", "verdict": "pass", "quote": None, "note": ""}],
                       "overall": "near", "deviation_ru": deviation, "confidence": 0.8})


def test_place_names_pass_with_the_flag() -> None:
    assert user_phrase(N1, places=True) == N1
    for phrase in PLACES:
        assert user_phrase(phrase, places=True) == phrase, phrase
    assert user_phrase("Vista Alegre вместо центра", places=True) == "Vista Alegre вместо центра"  # keeps its capital
    assert user_phrase("Не в центре (Vista Alegre).", places=True) == "не в центре (Vista Alegre)"


def test_latin_prose_is_still_dropped_with_the_flag() -> None:
    for phrase in PROSE:
        assert user_phrase(phrase, places=True) is None, phrase
    assert user_phrase("район " + "Vista Alegre " * 8, places=True) is None  # longer than 80 characters
    assert user_phrase("Vista Alegre", places=True) is None  # no Russian at all


def test_without_the_flag_any_latin_letter_drops_the_phrase() -> None:
    assert user_phrase(N1) is None
    for phrase in PROSE + PLACES:
        assert user_phrase(phrase) is None, phrase
    assert user_phrase("Дальше от метро.") == "дальше от метро"
    assert parse_relevance(json.dumps({"verdict": "near", "reason": "x", "deviation_ru": N1})).deviation is None
    assert parse_review(review_answer(N1), expected=["place"]).deviation_ru is None


def test_the_parsers_keep_the_place_phrase_with_the_flag() -> None:
    assert parse_relevance(json.dumps({"verdict": "near", "reason": "x", "deviation_ru": N1}),
                           places=True).deviation == N1
    assert parse_review(review_answer(N1), expected=["place"], places=True).deviation_ru == N1
    assert parse_review(review_answer("outside the center"), expected=["place"], places=True).deviation_ru is None


async def test_the_reviewer_and_the_legacy_judge_pass_the_flag_on() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = (review_answer(N1) if body["model"].startswith("anthropic")
                   else json.dumps({"verdict": "near", "reason": "x", "deviation_ru": N1}))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    task, finding = {"criteria": ["place"], "goal": "квартира в Мадриде"}, {"summary": "Piso en Vista Alegre"}
    for places, expected in ((True, N1), (False, None)):
        reviewer = OpenRouterReviewer(api_key="k", model="anthropic/x", client=client(), phrase_places=places)
        assert (await reviewer.review(task, finding)).deviation_ru == expected
        legacy = OpenRouterRelevanceJudge(api_key="k", model="openai/x", client=client(), phrase_places=places)
        assert (await legacy.judge(task, finding)).deviation == expected
        await reviewer.aclose()
        await legacy.aclose()


def test_the_setting_is_off_by_default_and_reaches_both_judges(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("USER_PHRASE_PLACES", "CAMPAIGN_JUDGE"):
        monkeypatch.delenv(name, raising=False)
    base = {"DATABASE_URL": "postgresql://x", "TELEGRAM_TOKEN": "t", "OPENROUTER_API_KEY": "k"}
    off = CampaignRunnerSettings(_env_file=None, **base)
    assert off.user_phrase_places is False
    judge = off.relevance_judge()
    assert isinstance(judge, ReviewerJudge) and judge.fallback.phrase_places is False
    on = CampaignRunnerSettings(_env_file=None, **base, USER_PHRASE_PLACES="true").relevance_judge()
    assert on.fallback.phrase_places is True and on.reviewer.phrase_places is True
