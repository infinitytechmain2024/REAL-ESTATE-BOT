"""The final report for the person who asked (PLAN 4.2): counts by reason, the ranked top, unreadable sites,
recommendations, sent once; and migration 038 on PostgreSQL."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import httpx
import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.final_report import (
    SYSTEM,
    FinalReporter,
    OpenRouterRecommender,
    Tally,
    card_score,
    clean_recommendations,
    facts_for_model,
    fallback_recommendations,
    parse_recommendations,
    rank_cards,
    real_estate_sources,
    report_text,
    similar_note,
    tally,
)
from bot.campaign.relevance import Relevance, ReviewerJudge
from bot.campaign.runner import CampaignRunner, RunnerConfig, campaign_request
from bot.campaign.runs import MemoryRunStore, OutcomeCount, SourceCount
from bot.web_search.models import SiteReport
from tests.test_campaign_relevance import _seed, needs_db, plot
from tests.test_campaign_runner import SUMMARY, Clock, FakeMessenger
from tests.test_reviewer import ScriptedReviewer

GOAL = "Купить квартиру в Валенсии до 200000 € от 2 комнат"
OWNER, USER, CHAT = 1, 2, -100
REPORT = "📋 Отчёт по поиску"


def flat(price: int, rooms: int | None, area: int | None, where: str, n: int, **extra) -> dict:
    return {"schema_version": "analysis-v4", "summary_ru": f"Квартира {n}", "location": f"{where}, Valencia",
            "country": "ES", "price_amount": price, "price_currency": "EUR", "deal_type": "sale",
            "property_type": "apartment", "rooms": rooms, "area_m2": area, "listing_kind": "offer",
            "original_post_link": f"https://www.idealista.com/inmueble/{n}/", **extra}


class FakeWeb:
    def __init__(self, reports: list[SiteReport]) -> None:
        self.reports = reports

    async def web_status(self, campaign_id: str):
        return None

    async def site_report(self, campaign_id: str) -> list[SiteReport]:
        return self.reports


class Advice:
    """The final model: records the facts it was given and answers with fixed recommendations."""

    def __init__(self, *tips: str, error: Exception | None = None) -> None:
        self.tips, self.error, self.facts = list(tips), error, []

    async def recommend(self, facts: dict) -> list[str]:
        self.facts.append(facts)
        if self.error:
            raise self.error
        return self.tips


SITES = [SiteReport("idealista.com", queries=3, results=25, links=14, from_search=6, refused=8),
         SiteReport("fotocasa.es", queries=2, results=12, links=9, read=7),
         SiteReport("habitaclia.com", queries=1, results=4, links=4, refused=4)]


async def build(advice=None, *, owner: bool = False, reports=SITES, judge=None, vertical: str = "real_estate"):
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = FakeMessenger()
    clock = Clock()
    reporter = FinalReporter(advice)
    runner = CampaignRunner(campaigns, store, messenger, None, now=clock, owner_ids={OWNER} if owner else set(),
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=0),
                            web=FakeWeb(reports) if reports is not None else None, relevance=judge,
                            final_report=reporter)
    cid = await campaigns.create(plan_campaign(GOAL, vertical=vertical, location="Valencia"), chat_id=CHAT,
                                 requested_by=OWNER if owner else USER, source_text=GOAL, actor="test")
    await campaigns.set_state(cid, "running", "test")
    store.add_groups(cid, 3)
    return campaigns, store, messenger, clock, runner, reporter, cid


def seed(store: MemoryRunStore, cid: str) -> None:
    rows = {
        # sent (exact): the budget is 200 000 €, from 2 rooms
        "f1": flat(195_000, 3, 85, "Ruzafa", 1), "f2": flat(150_000, 2, 70, "Benimaclet", 2),
        "f3": flat(199_000, 2, 60, "Campanar", 3), "f4": flat(200_000, 2, None, "El Cabanyal", 4),
        # the same flat as f1 on another site: one card only
        "dup": flat(196_000, 3, 85, "Barrio de Ruzafa", 5, original_post_link="https://www.fotocasa.es/es/5/"),
        # held: a little over the budget (similar), far over it (other)
        "over": flat(230_000, 3, 90, "Patraix", 6), "far": flat(400_000, 3, 120, "Eixample", 7),
        # excluded: a rental, another country, a catalog page
        "rent": flat(900, 2, 60, "Jesus", 8, deal_type="rent"), "pt": flat(150_000, 2, 60, "Lisboa", 9, country="PT"),
        "cat": flat(150_000, 2, 60, "Valencia", 10, listing_kind="catalog"),
    }
    for fid, payload in rows.items():
        store.add_finding(cid, fid, f"🏠 {fid}", payload=payload, original=f"post {fid}", vertical="real_estate",
                          url=payload["original_post_link"])


async def finish(campaigns, store, runner, clock, cid, state: str = "completed") -> None:
    await runner._stream(await campaigns.get(cid))
    await campaigns.set_state(cid, state, "test")
    clock.at = (await campaigns.get(cid)).finished_at + timedelta(minutes=1)


def reports_of(messenger: FakeMessenger) -> list[str]:
    return [t for _, _, t in messenger.sent if t.startswith(REPORT)]


# --- the numbers -------------------------------------------------------------------------------------------------------------


def test_outcome_rows_become_a_tally() -> None:
    counts = tally([OutcomeCount("sent", "exact", None, 4), OutcomeCount("sent", "similar", "budget", 1),
                    OutcomeCount("sending", "exact", None, 1), OutcomeCount("held", "similar", "budget", 2),
                    OutcomeCount("held", "similar", "unverified", 3), OutcomeCount("held", "other", "unverified", 1),
                    OutcomeCount("held", "excluded", "place", 2), OutcomeCount("held", "excluded", None, 1),
                    OutcomeCount("duplicate", "exact", None, 2)])
    assert (counts.sent_exact, counts.sent_approved, counts.held_similar, counts.held_other) == (5, 1, 5, 1)
    assert (counts.held_unverified, counts.duplicates, counts.excluded) == (4, 2, {"place": 2, "ai": 1})
    assert (counts.sent, counts.held, counts.rejected, counts.total) == (6, 6, 3, 17)


def test_a_report_warns_when_the_ai_check_failed_for_most_findings(caplog: pytest.LogCaptureFixture) -> None:
    few = tally([OutcomeCount("sent", "exact", None, 1), OutcomeCount("held", "similar", "ai_failed", 9)])
    assert few.unverified_majority is True  # 9 of 10 never judged
    with caplog.at_level("WARNING"):
        text = report_text("цель", few, [], [], [], [])
    assert "⚠️ ИИ-проверка не сработала для большинства находок (9 из 10)" in text
    assert "ИИ-проверка не сработала: 9" in text
    assert "campaign.unverified_majority" in caplog.text
    exactly = tally([OutcomeCount("sent", "exact", None, 1), OutcomeCount("held", "similar", "cost_cap", 9),
                     OutcomeCount("held", "excluded", "place", 50)])  # the rejected do not dilute the share
    assert exactly.unverified_majority and exactly.ai_failed == 9
    ok = tally([OutcomeCount("sent", "exact", None, 2), OutcomeCount("held", "similar", "ai_failed", 4),
                OutcomeCount("held", "similar", "budget", 4)])
    assert not ok.unverified_majority and "большинства находок" not in report_text("цель", ok, [], [], [], [])


def test_listings_without_an_area_are_not_blamed_on_the_ai_key() -> None:
    """The Madrid land run: most held findings had no area (a ≥2000 m² task), which said «проверьте ключ ИИ»."""
    run = tally([OutcomeCount("held", "similar", "area_unknown", 25), OutcomeCount("held", "similar", "unverified", 6),
                 OutcomeCount("held", "similar", None, 6)])
    assert run.held_unverified == 31 and not run.unverified_majority
    text = report_text("участок · Мадрид · покупка · от 2000 м²", run, [], [], [], [])
    assert "ключ ИИ" not in text and "большинства находок" not in text
    assert "площадь не указана: 25" in text and "не удалось подтвердить: 6" in text


def test_the_report_shows_the_money_the_skips_and_the_model_errors() -> None:
    from bot.campaign.final_report import cost_lines
    from bot.utils.costs import CostSummary

    spent = CostSummary({"llm": 0.9, "scrape": 0.15, "search": 0.0}, {},
                        {"llm:http_401": 3, "llm:model_request_refused_400": 1, "api:apify_http_429": 2},
                        {"llm:prefilter_deal": 14, "llm:prefilter_area": 40})
    lines = cost_lines(spent, budget=1.0)
    assert lines[0] == "💶 Расход: $1.05 из $1.00 (ИИ $0.90 · Scrape API $0.15)"
    assert lines[1].startswith("⛔ Бюджет прогона исчерпан")
    assert lines[2] == "Отсеяно до ИИ (без вызовов ИИ): 54 — участок меньше нужного 40, другой тип сделки 14"
    assert lines[3] == "⚠️ Ошибки ИИ: http_401 ×3, model_request_refused_400 ×1"
    assert lines[4] == "⚠️ Ошибки API порталов: apify_http_429 ×2"
    assert cost_lines(None) == [] and cost_lines(CostSummary(), 0) == ["💶 Расход: $0.00"]
    text = report_text("цель", tally([OutcomeCount("sent", "exact", None, 1)]), [], [], [], [], costs=lines)
    assert "💶 Расход: $1.05 из $1.00" in text and "⚠️ Ошибки ИИ: http_401 ×3" in text
    assert "⚠️ Ошибки API порталов: apify_http_429 ×2" in text
    separate = cost_lines(CostSummary(by_stage={"api": 0.20, "scrape": 0.10}))
    assert separate == ["💶 Расход: $0.30 (API порталов $0.20 · Scrape API $0.10)"]
    estimated = cost_lines(CostSummary(by_stage={"scrape": 0.05}, estimates={"scrape": 0.05}))
    assert estimated == ["💶 Расход: $0.05 (Scrape API $0.05)",
                         "Из них оценка без подтверждения провайдера: $0.05"]
    edge = tally([OutcomeCount("sent", "exact", None, 1), OutcomeCount("held", "similar", "unverified", 4)])
    assert not edge.unverified_majority, "exactly 80 % is not more than 80 %"


def test_the_model_gets_the_specs_tolerance_when_there_is_one() -> None:
    assert facts_for_model({}, Tally(), [], [])["tolerance_pct"] == 10
    assert facts_for_model({}, Tally(), [], [], 5)["tolerance_pct"] == 5


# --- the ranking ---------------------------------------------------------------------------------------------------------------


async def test_the_best_cards_come_first_by_budget_rooms_area_and_evidence() -> None:
    campaigns, store, _, clock, runner, _, cid = await build()
    seed(store, cid)
    await finish(campaigns, store, runner, clock, cid)
    request = campaign_request(await campaigns.get(cid))
    sent = await store.recent_sent_findings(cid)
    assert {s.finding.id for s in sent} == {"f1", "f2", "f3", "f4"}
    ranked = rank_cards(sent, request)
    assert [r.card.finding.id for r in ranked] == ["f3", "f1", "f2", "f4"]  # nearest the budget with the asked rooms first
    assert all(0 <= r.score <= 1 for r in ranked) and ranked[0].score > ranked[-1].score
    assert len(rank_cards(sent, request, limit=2)) == 2

    # the pieces of the score
    assert card_score(flat(200_000, 2, 60, "x", 1), request) > card_score(flat(150_000, 2, 60, "x", 1), request)
    assert card_score(flat(220_000, 2, 60, "x", 1), request) < card_score(flat(150_000, 2, 60, "x", 1), request)
    assert card_score(flat(190_000, 2, 60, "x", 1), request) > card_score(flat(190_000, None, 60, "x", 1), request)
    assert card_score(flat(190_000, 2, 60, "x", 1), request) > card_score({"price_amount": 190_000}, request)
    assert card_score(None, request) < 0.5


# --- the report ---------------------------------------------------------------------------------------------------------------------


async def test_the_user_gets_one_report_with_counts_reasons_top_sites_and_advice() -> None:
    advice = Advice("Поднимите бюджет на 10 %: 2 варианта отклонены по цене.",
                    "Idealista не читается: 8 отказов, включите чтение через API.")
    campaigns, store, messenger, clock, runner, _, cid = await build(advice)
    seed(store, cid)
    await finish(campaigns, store, runner, clock, cid)
    await runner.step(cid)
    await runner.step(cid)  # the next tick sends nothing more
    [text] = reports_of(messenger)
    assert not text.startswith(SUMMARY) and "🔎" not in text and len(text) < 4000
    assert messenger.summaries() == []  # a normal user gets no technical summary
    lines = text.splitlines()
    assert lines[:2] == [REPORT, "🎯 Валенсия · покупка · до 200 000 € · от 2 комн."]
    assert "Отправлено вам: 4" in lines and "Похожие, не показаны: 2 (чуть не подошли)" in lines
    assert "Отклонено: 3" in lines and "Повторы одного объекта на разных сайтах: 1" in lines
    assert "• другой тип сделки (аренда вместо покупки или наоборот) — 1" in lines
    assert "• не тот город или район — 1" in lines
    assert "• не объявление (каталог, статистика, поиск жилья) — 1" in lines
    # the ten best sent cards, ranked, each with its link
    best = lines[lines.index("Лучшие варианты (4):") + 1:lines.index("Не удалось прочитать:")]
    numbered = [line for line in best if line[:2] in ("1.", "2.", "3.", "4.")]
    places = ("Campanar", "Ruzafa", "Benimaclet", "El Cabanyal")
    assert [next(w for w in places if w in n) for n in numbered] == list(places)
    assert numbered[0].startswith("1. 199 000 € · 2 комн. · 60 м² · Campanar, Valencia · карточка №")
    assert "   https://www.idealista.com/inmueble/3/" in lines
    # the funnel of each site and the sites that could not be read, with why
    assert any(line.startswith("• Idealista — 14 ссылок в поиске") for line in lines)
    assert any(line.startswith("• Fotocasa — 9 ссылок в поиске · прочитано 7") for line in lines)
    broken = lines[lines.index("Не удалось прочитать:") + 1:lines.index("По источникам:")]
    assert any(line.startswith("• Habitaclia — сайт не пускает ботов") and "ни одна страница не открылась" in line
               for line in broken)
    assert any(line.startswith("• Idealista — сайт не пускает ботов") and "6 объявлений взято из описания" in line
               for line in broken)
    assert not any("Fotocasa" in line for line in broken)  # a readable site is not listed there
    # advice from the model, numbered, after everything else
    assert lines[-3:] == ["Что можно сделать:", "1. Поднимите бюджет на 10 %: 2 варианта отклонены по цене.",
                          "2. Idealista не читается: 8 отказов, включите чтение через API."]
    # the model saw aggregated numbers only: no listing text, no links, no ids
    [facts] = advice.facts
    assert facts["counts"]["sent_exact"] == 4 and facts["rejected_by_reason"] == {"deal": 1, "place": 1, "kind": 1}
    assert sorted(facts["unreadable_sites"]) == ["habitaclia.com", "idealista.com"]
    assert facts["task"] == {"place": "Valencia", "deal": "sale", "budget": 200000, "budget_is_maximum": True,
                             "currency": "EUR", "rooms_min": 2}
    blob = json.dumps(facts, ensure_ascii=False)
    assert "http" not in blob and "Квартира" not in blob and "Campanar" not in blob and "post " not in blob
    assert messenger.statuses()[-1] != text and messenger.sent[-1][2] == messenger.statuses()[-1]  # status stays last


async def test_the_report_is_sent_once_across_restarts_and_retried_when_telegram_fails() -> None:
    campaigns, store, messenger, clock, runner, reporter, cid = await build(Advice("Поднимите бюджет на 10 %: 2 варианта.", "Добавьте район: 3 варианта."))
    seed(store, cid)
    await finish(campaigns, store, runner, clock, cid)
    messenger.fail = 1  # Telegram is down for the first send: the claim is given back
    await runner.step(cid)
    assert reports_of(messenger) == [] and cid not in store.final_reports
    await runner.step(cid)
    assert len(reports_of(messenger)) == 1 and cid in store.final_reports
    # a restart: a new runner over the same stores (the claim lives in the database) sends nothing
    again = CampaignRunner(campaigns, store, messenger, None, now=clock, final_report=reporter,
                           config=RunnerConfig(relevance_fail_closed=False))
    await again.step(cid)
    await again.step(cid)
    assert len(reports_of(messenger)) == 1


async def test_owners_keep_their_summary_and_also_get_the_report() -> None:
    campaigns, store, messenger, clock, runner, _, cid = await build(Advice("Поднимите бюджет на 10 %: 2 варианта.", "Добавьте район: 3 варианта."), owner=True)
    seed(store, cid)
    await finish(campaigns, store, runner, clock, cid)
    await runner.step(cid)
    assert len(messenger.summaries()) == 1 and len(reports_of(messenger)) == 1
    texts = [t for _, _, t in messenger.sent]
    assert texts.index(messenger.summaries()[0]) < texts.index(reports_of(messenger)[0])
    assert messenger.summaries()[0].startswith(SUMMARY)


async def test_no_report_without_a_reporter_for_old_investor_failed_or_empty_cancelled_campaigns() -> None:
    campaigns, store, messenger, clock, runner, _, cid = await build(Advice("Поднимите бюджет на 10 %: 2 варианта.", "Добавьте район: 3 варианта."))
    runner.final_report = None
    seed(store, cid)
    await finish(campaigns, store, runner, clock, cid)
    await runner.step(cid)
    assert reports_of(messenger) == [] and not store.final_reports

    campaigns, store, messenger, clock, runner, _, cid = await build(Advice("Поднимите бюджет на 10 %: 2 варианта.", "Добавьте район: 3 варианта."))
    seed(store, cid)
    await finish(campaigns, store, runner, clock, cid)
    clock.at += timedelta(hours=3)  # ended before the report existed
    await runner.step(cid)
    assert reports_of(messenger) == []

    campaigns, store, messenger, clock, runner, _, cid = await build(Advice("Поднимите бюджет на 10 %: 2 варианта.", "Добавьте район: 3 варианта."))
    await campaigns.set_state(cid, "failed", "test")
    clock.at = (await campaigns.get(cid)).finished_at
    await runner.step(cid)
    assert reports_of(messenger) == []

    campaigns, store, messenger, clock, runner, _, cid = await build(Advice("Поднимите бюджет на 10 %: 2 варианта.", "Добавьте район: 3 варианта."), vertical="investors")
    await finish(campaigns, store, runner, clock, cid)
    await runner.step(cid)
    assert reports_of(messenger) == []

    campaigns, store, messenger, clock, runner, _, cid = await build(Advice("Поднимите бюджет на 10 %: 2 варианта.", "Добавьте район: 3 варианта."))
    await finish(campaigns, store, runner, clock, cid, state="cancelled")  # cancelled at once: nothing to report
    await runner.step(cid)
    assert reports_of(messenger) == []


async def test_without_a_model_or_when_it_fails_rule_based_advice_is_used() -> None:
    for advice in (None, Advice(error=httpx.ConnectError("down")), Advice(), Advice(error=ValueError("bad json"))):
        campaigns, store, messenger, clock, runner, _, cid = await build(advice)
        seed(store, cid)
        await finish(campaigns, store, runner, clock, cid)
        await runner.step(cid)
        [text] = reports_of(messenger)
        tail = text[text.index("Что можно сделать:"):]
        assert "Habitaclia не читается (отказов: 4): сайт не пускает ботов" in tail and "Idealista не читается" in tail

    # the rules by themselves
    facts = {"counts": {"sent_exact": 2}, "rejected_by_reason": {"budget": 5, "place": 4}, "unreadable_sites": []}
    tips = fallback_recommendations(facts)
    assert "поднимите бюджет на 10 %" in tips[0] and "соседние районы" in tips[1]
    empty = fallback_recommendations({"counts": {}, "rejected_by_reason": {}, "unreadable_sites": []})
    assert empty[0].startswith("Точных вариантов: 0 — ослабьте самое жёсткое условие")


async def test_an_empty_search_still_reports_what_could_not_be_read() -> None:
    campaigns, store, messenger, clock, runner, _, cid = await build(None, reports=SITES[:1])
    await finish(campaigns, store, runner, clock, cid)
    await runner.step(cid)
    [text] = reports_of(messenger)
    assert "Подходящих объявлений не нашлось." in text and "Не удалось прочитать:" in text
    assert "Лучшие варианты" not in text and "Почему отклонено" not in text


def test_the_report_always_fits_one_telegram_message() -> None:
    from bot.campaign.runs import SentFinding, StreamFinding

    cards = [SentFinding(StreamFinding(f"f{n}", "t", flat(190_000 + n, 2, 70, "Ruzafa" * 5, n,
                                                          original_post_link="https://www.idealista.com/" + "x" * 150)),
                         n, n) for n in range(1, 11)]
    request = campaign_request_of(GOAL)
    sources = [SourceCount("website", f"site{n}.es", 1, 3, 1, 1, 0) for n in range(12)]
    reports = [SiteReport(f"site{n}.es", links=5, read=3) for n in range(12)] + [
        SiteReport(f"closed{n}.es", links=4, refused=4) for n in range(8)]
    from bot.campaign.final_report import unreadable_sites
    from bot.campaign.summary import site_lines

    funnel, _ = site_lines(sources, reports, ())
    text = report_text("Цель " * 30, Tally(10, 0, 3, 2, 1, 4, {"place": 5, "budget": 3}), rank_cards(cards, request),
                       funnel, unreadable_sites(sources, reports, ()), ["Совет " * 40] * 4)
    assert len(text) <= 3900 and text.startswith(REPORT) and "Что можно сделать:" in text


def campaign_request_of(goal: str):
    from bot.campaign.tolerance import request_for

    plan = plan_campaign(goal, vertical="real_estate", location="Valencia")
    return request_for(plan.constraints, location=plan.location, vertical=plan.vertical, text=goal, country=plan.country)


# --- the final model over a fake HTTP -----------------------------------------------------------------------------------------------


async def test_the_recommender_asks_the_final_model_for_two_to_four_russian_tips() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        tips = ["Raise the budget", "Поднимите бюджет на 10 %.", "Поднимите бюджет на 10 %.", "Добавьте район Патрайкс.",
                "Включите API для Idealista.", "Пятый совет.", "Шестой совет."]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"recommendations": tips})}}]})

    recommender = OpenRouterRecommender(api_key="k", model="anthropic/claude-opus-4.5",
                                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    tips = await recommender.recommend({"counts": {"sent_exact": 1}})
    assert tips == ["Поднимите бюджет на 10 %.", "Добавьте район Патрайкс.", "Включите API для Idealista.",
                    "Пятый совет."]  # English dropped, duplicates dropped, at most four
    assert seen[0]["model"] == "anthropic/claude-opus-4.5" and seen[0]["response_format"]["json_schema"]["strict"]
    assert "only the given numbers" in seen[0]["messages"][0]["content"]
    assert parse_recommendations('```json\n{"recommendations": [{"text": "Совет."}]}\n```') == ["Совет."]
    with pytest.raises(ValueError):
        parse_recommendations('{"x": 1}')
    with pytest.raises(ValueError):
        OpenRouterRecommender(api_key="")


# --- PostgreSQL (migration 038) ---------------------------------------------------------------------------------------------------------


@pytest.fixture
async def pool():
    import asyncpg

    from tests.test_campaign_relevance import MIGRATIONS, URL

    conn = await asyncpg.connect(URL)
    await conn.execute("drop schema public cascade; create schema public;")
    for path in MIGRATIONS:
        await conn.execute(path.read_text(encoding="utf-8"))
    await conn.close()
    pool = await asyncpg.create_pool(URL, min_size=1, max_size=4)
    try:
        yield pool
    finally:
        await pool.close()


@needs_db
async def test_postgres_stores_the_matrix_the_reason_and_the_one_time_report_claim(pool) -> None:
    from bot.campaign.runs import PostgresRunStore
    from bot.campaign.store import PostgresCampaignStore
    from bot.orchestra.store import SafetyLimits

    columns = {(r["table_name"], r["column_name"]): r["data_type"] for r in await pool.fetch(
        """select table_name, column_name, data_type from information_schema.columns
            where (table_name, column_name) in (('campaign_finding_relevance', 'review'), ('campaign_findings', 'why'),
                                                 ('campaign_runs', 'final_report_sent_at'))""")}
    assert columns == {("campaign_finding_relevance", "review"): "jsonb", ("campaign_findings", "why"): "text",
                       ("campaign_runs", "final_report_sent_at"): "timestamp with time zone"}

    campaigns = PostgresCampaignStore(pool)
    store = PostgresRunStore(pool, SafetyLimits())
    messenger = FakeMessenger()
    reviewer = ScriptedReviewer({"PASS": ("pass", "match"), "FAIL": ("fail", "reject"), "UNKNOWN": ("unknown", "near")})
    advice = Advice("Расширьте район поиска: 1 вариант вне района.", "Поднимите бюджет на 10 %: 2 варианта.")
    runner = CampaignRunner(campaigns, store, messenger, None, relevance=ReviewerJudge(reviewer),
                            final_report=FinalReporter(advice), config=RunnerConfig(relevance_fail_closed=False))
    text = "Купить участок от 2000 м² в Мадриде"
    cid = await campaigns.create(plan_campaign(text, vertical="real_estate", location="Madrid"), chat_id=CHAT,
                                 requested_by=USER, source_text=text, actor="telegram:2")
    await campaigns.set_state(cid, "running", "campaign:test")
    ids = await _seed(pool, cid, {name: {**plot(2500), "summary_ru": f"Участок {word}"}
                                  for name, word in (("ok", "PASS"), ("bad", "FAIL"), ("maybe", "UNKNOWN"))})
    await runner.step(cid)
    rows = {r["finding_id"]: (r["bucket"], r["state"], r["why"], r["hold_reason"]) for r in await pool.fetch(
        "select finding_id::text, bucket, state, why, hold_reason from campaign_findings")}
    assert rows == {ids["ok"]: ("exact", "sent", None, None), ids["bad"]: ("excluded", "held", "place", None),
                    ids["maybe"]: ("similar", "held", "unverified", "Не подтверждено: место")}
    stored = await store.relevance(cid, ids["bad"])
    assert stored.verdict == "reject" and stored.review["criteria"][0]["verdict"] == "fail"
    assert stored.review["criteria"][0]["quote"] == "Boadilla" and stored.review["overall"] == "reject"
    assert (await store.relevance(cid, ids["ok"])).review["overall"] == "match"
    await store.save_relevance(cid, ids["ok"], Relevance("reject", "x", None, "m", {"overall": "reject"}))  # once
    assert (await store.relevance(cid, ids["ok"])).review["overall"] == "match"

    assert sorted((o.state, o.bucket, o.why, o.count) for o in await store.outcome_counts(cid)) == [
        ("held", "excluded", "place", 1), ("held", "similar", "unverified", 1), ("sent", "exact", None, 1)]
    # the search had nothing left to read, so that one step also completed it and sent the report
    [report] = reports_of(messenger)
    assert "Отправлено вам: 1" in report and "Отклонено: 1" in report and "Расширьте район поиска: 1 вариант вне района." in report
    assert "Похожие, не показаны: 1" in report and "не удалось подтвердить: 1" in report
    assert await pool.fetchval("select final_report_sent_at is not null from campaign_runs where campaign_id = $1::uuid",
                               cid)
    assert not await store.claim_final_report(cid)
    await runner.step(cid)
    assert len(reports_of(messenger)) == 1  # sent once
    await store.release_final_report(cid)
    assert await pool.fetchval("select final_report_sent_at from campaign_runs where campaign_id = $1::uuid", cid) is None
    assert await store.claim_final_report(cid) and not await store.claim_final_report(cid)
    assert await store.relevance_calls(cid) == 3


# --- no advice to change the deal, the kind of property or the city; real-estate sources only; the «similar» hint ------------------


def test_recommendations_never_ask_to_change_the_deal_type_property_type_or_city() -> None:
    facts = {"counts": {"sent_exact": 1, "held_similar": 3}, "rejected_by_reason": {"deal": 33, "type": 12, "place": 4},
             "task": {"deal": "sale"}, "unreadable_sites": []}
    tips = fallback_recommendations(facts)
    advice = [t for t in tips if "аренд" not in t]
    assert not any(re.search(r"сделк|покупк|тип объекта|другой город", t) for t in tips)
    assert any("33 объявлений аренды попали в выдачу" in t for t in tips)  # an explanation, not advice
    assert any("соседние районы" in t for t in advice) and all(re.search(r"\d", t) for t in tips)
    # the same numbers with a small deal count: nothing about the deal at all
    assert not any("аренд" in t for t in fallback_recommendations({**facts, "rejected_by_reason": {"deal": 3}}))

    model = ["Смягчите требование по типу сделки: 33 варианта отклонены по этому критерию.",
             "Рассмотрите покупку вместо аренды: 33 варианта.", "Поищите в другом городе: 4 варианта.",
             "Подойдёт другой тип объекта: 12 вариантов.", "Поднимите бюджет на 10 %: 5 вариантов дороже.",
             "Расширьте поиск без числа."]
    assert clean_recommendations(model, facts)[0] == "Поднимите бюджет на 10 %: 5 вариантов дороже."
    cleaned = clean_recommendations(model, facts)
    assert 2 <= len(cleaned) <= 4 and not any(
        re.search(r"сделк|покупк|тип объекта|другом город", t) for t in cleaned if "отсеяны" not in t)
    assert clean_recommendations(["Поднимите бюджет на 10 %: 5.", "Добавьте район: 3 варианта."], facts) == [
        "Поднимите бюджет на 10 %: 5.", "Добавьте район: 3 варианта."]


async def test_the_model_prompt_forbids_changing_the_deal_type_and_the_report_drops_such_advice() -> None:
    assert "NEVER advise changing the deal type" in SYSTEM
    advice = Advice("Смягчите требование по типу сделки: 33 варианта отклонены.", "Поднимите бюджет на 10 %: 2 дороже.",
                    "Добавьте соседний район: 3 вне района.")
    campaigns, store, messenger, clock, runner, _, cid = await build(advice)
    seed(store, cid)
    await finish(campaigns, store, runner, clock, cid)
    await runner.step(cid)
    [text] = reports_of(messenger)
    tail = text[text.index("Что можно сделать:"):]
    assert "типу сделки" not in tail and "1. Поднимите бюджет на 10 %" in tail and "2. Добавьте соседний район" in tail


def test_only_real_estate_sources_are_shown_and_the_rest_is_one_line() -> None:
    reports = [SiteReport("idealista.com", links=5, read=3), SiteReport("dle.rae.es", links=2, refused=2),
               SiteReport("web2.0calc.es", links=1, refused=1), SiteReport("wumbo.net", links=1, refused=1),
               SiteReport("rtve.es", links=3, read=3), SiteReport("citiesinsider.com", links=2, read=2),
               SiteReport("inmoblog.es", links=2, read=2), SiteReport("habitaclia.com", links=4, refused=4)]
    sources = [SourceCount("website", "rtve.es", 1, 0, 0, 0, 0), SourceCount("website", "inmoblog.es", 1, 2, 1, 1, 0)]
    kept_sources, kept_reports, other = real_estate_sources(sources, reports, ())
    assert sorted(r.host for r in kept_reports) == ["habitaclia.com", "idealista.com", "inmoblog.es"]  # a finding counts
    assert [s.name for s in kept_sources] == ["inmoblog.es"] and other == 5
    counts = Tally(1, 0, 0, 0, 0, 0, {})
    from bot.campaign.final_report import unreadable_sites
    from bot.campaign.summary import site_lines, summary_text

    funnel, _ = site_lines(kept_sources, kept_reports, ())
    text = report_text("Цель", counts, [], funnel, unreadable_sites(kept_sources, kept_reports, ()), [], other)
    assert "dle.rae.es" not in text and "rtve.es" not in text and "wumbo" not in text
    assert "Habitaclia" in text and "Прочие сайты из поиска: 5 (не относятся к недвижимости или без объявлений)" in text
    # the owners' technical summary keeps the raw list
    assert "dle.rae.es" in summary_text("Цель", sources, reports, ())


async def test_the_final_report_lists_no_noise_sites() -> None:
    reports = [*SITES, SiteReport("dle.rae.es", links=2, refused=2), SiteReport("rtve.es", links=3, read=3)]
    campaigns, store, messenger, clock, runner, _, cid = await build(None, reports=reports)
    seed(store, cid)
    await finish(campaigns, store, runner, clock, cid)
    await runner.step(cid)
    [text] = reports_of(messenger)
    assert "dle.rae.es" not in text and "rtve.es" not in text and "Прочие сайты из поиска: 2 (" in text


def test_a_report_with_nothing_sent_says_how_to_get_the_similar_ones() -> None:
    counts = Tally(0, 0, 3, 0, 0, 0, {})
    asked, never = {"similar": "asked"}, {"similar": None}
    assert similar_note(counts, asked) == "Похожие можно открыть кнопкой «Одобрить» выше"
    assert similar_note(counts, never) == "Отправлю похожие, если подтвердите"
    assert similar_note(counts, None) is None and similar_note(counts, {"similar": "declined"}) is None
    assert similar_note(Tally(2, 0, 3, 0, 0, 0, {}), asked) is None and similar_note(Tally(), asked) is None
    text = report_text("Цель", counts, [], [], [], [], 0, similar_note(counts, asked))
    lines = text.splitlines()
    held = lines.index("Похожие, не показаны: 3 (чуть не подошли)")
    assert lines[held - 1] == "Отправлено вам: 0" and lines[held + 1] == "Похожие можно открыть кнопкой «Одобрить» выше"
    assert "кнопкой" not in report_text("Цель", Tally(2, 0, 3, 0, 0, 0, {}), [], [], [], [], 0, None)
