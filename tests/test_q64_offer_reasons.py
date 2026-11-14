"""Q64 (``OFFER_REASONS_FROM_REVIEW``): the «Показать?» question names the AI reviewer's differences of the held similar
listings, the same ones grouped and ranked by count; the reviewer's phrase may keep Spanish land-status words (sim
b6-g1-s7: «не подтверждено: площадь» named for 3 of 37, the 34 reviewer phrases «земля rústico, не urbano» dropped for
their Latin letters). Synthetic plots and phrases; mocks only."""

from __future__ import annotations

import json

import httpx
import pytest

from bot.agents.reviewer import OpenRouterReviewer, parse_review
from bot.campaign import offers
from bot.campaign.offers import MAX_REASONS as MAX
from bot.campaign.offers import Deviation
from bot.campaign.relevance import (
    OpenRouterRelevanceJudge,
    Relevance,
    ReviewerJudge,
    parse_relevance,
    user_phrase,
)
from bot.campaign.runner import RunnerConfig
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_campaign_relevance import FakeJudge, add, plot, setup

RUSTICO = "земля rústico, не urbano"
LAND = (RUSTICO, "сельхозучасток (rústica), а не solar urbano", "статус urbano не указан",
        "площадь не указана, земля no urbanizable", "земля urbanizable, не urbano")
PROSE = ("near the center", "земля rústico, near the road", "a 5 minutos", "без лифта (Sin ascensor)", "m² 2000")


def review_answer(deviation: str) -> str:
    return json.dumps({"criteria": [{"name": "place", "verdict": "pass", "quote": None, "note": ""}],
                       "overall": "near", "deviation_ru": deviation, "confidence": 0.8})


def test_land_terms_pass_only_with_the_flag() -> None:
    for phrase in LAND:
        assert user_phrase(phrase) is None and user_phrase(phrase, places=True) is None
        assert user_phrase(phrase, land_terms=True) == phrase, phrase
        assert user_phrase(phrase, places=True, land_terms=True) == phrase, phrase
    for phrase in PROSE:
        assert user_phrase(phrase, land_terms=True) is None, phrase
    # with the place names flag a place may stand next to a land word
    assert user_phrase("земля rústico (Pedrezuela)", land_terms=True) is None
    assert user_phrase("земля rústico (Pedrezuela)", places=True, land_terms=True) == "земля rústico (Pedrezuela)"
    assert user_phrase("промышленный участок, статус земли не указан") == "промышленный участок, статус земли не указан"


def test_parsers_keep_the_land_phrase_with_the_flag() -> None:
    assert parse_review(review_answer(RUSTICO), expected=["place"], places=True).deviation_ru is None
    assert parse_review(review_answer(RUSTICO), expected=["place"], land_terms=True).deviation_ru == RUSTICO
    legacy = json.dumps({"verdict": "near", "reason": "x", "deviation_ru": RUSTICO})
    assert parse_relevance(legacy).deviation is None
    assert parse_relevance(legacy, land_terms=True).deviation == RUSTICO


async def test_the_reviewer_and_the_legacy_judge_pass_the_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = (review_answer(RUSTICO) if body["model"].startswith("anthropic")
                   else json.dumps({"verdict": "near", "reason": "x", "deviation_ru": RUSTICO}))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    def client() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    task, finding = {"criteria": ["place"], "goal": "участок в Мадриде"}, {"summary": "Finca rústica"}
    for flag, expected in ((True, RUSTICO), (False, None)):
        reviewer = OpenRouterReviewer(api_key="k", model="anthropic/x", client=client(), phrase_land_terms=flag)
        assert (await reviewer.review(task, finding)).deviation_ru == expected
        legacy = OpenRouterRelevanceJudge(api_key="k", model="openai/x", client=client(), phrase_land_terms=flag)
        assert (await legacy.judge(task, finding)).deviation == expected
        await reviewer.aclose()
        await legacy.aclose()


def test_counted_reasons_group_and_rank_by_count() -> None:
    deviations = [
        Deviation("area_unknown", phrase="площадь не указана"),
        Deviation("other", phrase="статус земли не указан"),
        Deviation("other", phrase=RUSTICO),
        Deviation("other", phrase=RUSTICO),
        Deviation("area_unknown", phrase="площадь не указана"),
        Deviation("other", phrase=" " + RUSTICO + " "),
        Deviation("other", phrase="статус земли не указан"),
        Deviation("area_unknown", phrase="площадь не указана"),
        Deviation("price", "~60 000 €", "~50 000 €", value=60_000),
        Deviation("price", "~75 000 €", "~50 000 €", value=75_000),
        Deviation("unverified", unknown=("место",)),
        Deviation("deal_kind", phrase="аукцион"),
        Deviation("other"),
    ]
    assert offers.counted_reasons(deviations) == [
        "не подтверждено: площадь — 3", f"{RUSTICO} — 3", "статус земли не указан — 2", "дороже (до ~75 000 €) — 2",
        "не подтверждено: место — 1", "другой вид сделки: аукцион — 1", "другие отличия — 1"]
    assert offers.counted_reasons([]) == []


async def _question(**config) -> str:
    phrases = (RUSTICO, "статус земли не указан", RUSTICO, RUSTICO, "статус земли не указан")
    judge = FakeJudge(*(Relevance("near", "Отличие.", phrase) for phrase in phrases))
    _, store, messenger, runner, _cid = await setup(judge, **config)
    for i in range(len(phrases)):
        add(store, _cid, f"p{i}", plot(2500))
    await runner.tick()
    assert all(store.buckets[f"p{i}"][0] == "similar" for i in range(len(phrases)))
    (text,) = [text for _, text, _ in messenger.asks]
    return text


async def test_the_question_counts_the_reviewer_differences_only_with_the_flag() -> None:
    lead = "По вашим критериям пока ничего не нашёл, но есть участки"
    assert await _question() == f"{lead} {RUSTICO}. Показать?"
    assert await _question(offer_all_reasons=True) == (
        f"{lead} с отличиями — {RUSTICO}; статус земли не указан. Показать?")
    expected = f"{lead} с отличиями — {RUSTICO} — 3; статус земли не указан — 2. Показать?"
    assert await _question(offer_reasons_from_review=True) == expected
    assert await _question(offer_reasons_from_review=True, offer_all_reasons=True) == expected


async def test_a_failure_while_counting_keeps_the_old_question() -> None:
    judge = FakeJudge(Relevance("near", "Отличие.", RUSTICO), Relevance("near", "Отличие.", "статус не указан"))
    _, store, messenger, runner, cid = await setup(judge, offer_reasons_from_review=True)
    add(store, cid, "a", plot(2500))
    add(store, cid, "b", plot(2500))

    async def broken(*_args, **_kw):
        raise RuntimeError("store down")

    runner._counted_reasons = broken  # type: ignore[method-assign]
    await runner.tick()
    assert [t for _, t, _ in messenger.asks] == [
        f"По вашим критериям пока ничего не нашёл, но есть участки {RUSTICO}. Показать?"]
    assert await store.offer_state(cid, "similar") == "asked"


def test_the_setting_is_off_by_default_and_reaches_the_runner_and_both_judges(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OFFER_REASONS_FROM_REVIEW", "CAMPAIGN_JUDGE"):
        monkeypatch.delenv(name, raising=False)
    base = {"DATABASE_URL": "postgresql://x", "TELEGRAM_TOKEN": "t", "OPENROUTER_API_KEY": "k"}
    assert RunnerConfig().offer_reasons_from_review is False
    off = CampaignRunnerSettings(_env_file=None, **base)
    assert off.offer_reasons_from_review is False and off.runner_config().offer_reasons_from_review is False
    judge = off.relevance_judge()
    assert isinstance(judge, ReviewerJudge)
    assert judge.reviewer.phrase_land_terms is False and judge.fallback.phrase_land_terms is False
    on = CampaignRunnerSettings(_env_file=None, **base, OFFER_REASONS_FROM_REVIEW="true")
    assert on.runner_config().offer_reasons_from_review is True
    judge = on.relevance_judge()
    assert judge.reviewer.phrase_land_terms is True and judge.fallback.phrase_land_terms is True


def test_no_and_solar_pass_only_with_another_land_word() -> None:
    for phrase in ("земля no urbanizable", "suelo no urbanizable, дальше от трассы", "земля urbano, no rústico",
                   "сельхозучасток, а не solar urbano", "участок solar, suelo urbano"):
        assert user_phrase(phrase, land_terms=True) == phrase, phrase
    for phrase in ("no, участок занят", "участок занят, no", "участок no занят urbano", "это solar", "solar, no",
                   "solar занят"):
        assert user_phrase(phrase, land_terms=True) is None, phrase


def test_only_unnamed_differences_keep_the_old_question() -> None:
    assert offers.counted_reasons([Deviation("other"), Deviation("other", phrase=" ")]) == []
    assert offers.counted_reasons([Deviation("other"), Deviation("other", phrase=RUSTICO)]) == [
        f"{RUSTICO} — 1", "другие отличия — 1"]


async def test_the_runner_keeps_the_old_question_when_no_difference_is_named() -> None:
    judge = FakeJudge(Relevance("near", "Отличие.", None), Relevance("near", "Отличие.", None))
    _, store, messenger, runner, cid = await setup(judge, offer_reasons_from_review=True)
    _, _, old_messenger, old_runner, old_cid = await setup(
        FakeJudge(Relevance("near", "Отличие.", None), Relevance("near", "Отличие.", None)))
    for fid in ("a", "b"):
        add(store, cid, fid, plot(2500))
    await runner.tick()
    old_store = old_runner.store
    for fid in ("a", "b"):
        add(old_store, old_cid, fid, plot(2500))
    await old_runner.tick()
    assert [t for _, t, _ in messenger.asks] == [t for _, t, _ in old_messenger.asks] and len(messenger.asks) == 1
    assert "другие отличия" not in messenger.asks[0][1]


def test_the_rest_is_named_as_kinds_of_differences_only_when_counted() -> None:
    many = [f"отличие {i} — 1" for i in range(MAX + 1)]
    assert offers.reasons_question(many, exact_found=True).endswith(" и ещё 1. Показать?")  # Q25 as before
    assert offers.reasons_question(many, exact_found=True, counted=True).endswith(
        " и ещё 1 другое отличие. Показать?")
    for n, tail in ((2, "других отличия"), (4, "других отличия"), (5, "других отличий"), (11, "других отличий"),
                    (12, "других отличий"), (21, "другое отличие"), (22, "других отличия")):
        reasons = [f"отличие {i} — 1" for i in range(MAX + n)]
        assert offers.reasons_question(reasons, exact_found=False, counted=True).endswith(
            f" и ещё {n} {tail}. Показать?"), n


async def test_the_runner_question_names_the_rest_as_kinds() -> None:
    phrases = tuple(f"отличие номер {i}" for i in range(MAX + 2))
    judge = FakeJudge(*(Relevance("near", "Отличие.", p) for p in phrases))
    _, store, messenger, runner, cid = await setup(judge, offer_reasons_from_review=True)
    for i in range(len(phrases)):
        add(store, cid, f"p{i}", plot(2500))
    await runner.tick()
    (text,) = [t for _, t, _ in messenger.asks]
    assert text.endswith(" и ещё 2 других отличия. Показать?")
