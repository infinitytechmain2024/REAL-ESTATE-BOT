"""H10: auction / nuda propiedad deviation, spend outside the campaign, empty API portals, ranking, advice."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.final_report import (
    HeldEvidence,
    cost_lines,
    evidence_actions,
    fallback_recommendations,
    rank_cards,
    task_facts,
)
from bot.campaign.offers import Deviation, card_line, similar_question
from bot.campaign.relevance import reason_category
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore, SentFinding, StreamFinding
from bot.campaign.summary import site_lines
from bot.campaign.tolerance import Request, deal_kind_of
from bot.control_plane.service import ControlPlane
from bot.control_plane.settings import ControlPlaneSettings
from bot.control_plane.store import MemoryControlPlaneStore
from bot.utils import costs
from bot.web_search.models import SiteReport, SourceRun
from bot.web_search.sources import SourceListing
from bot.web_search.store import MemoryWebStore
from tests.test_near_match import CHAT, GOAL, OWNER, USER, ButtonMessenger, cards, listing, press

pytestmark = pytest.mark.asyncio


# --- (а) auction / nuda propiedad ------------------------------------------------------------------


def test_the_deal_kind_is_read_from_the_text_without_case_or_accents() -> None:
    assert deal_kind_of("Piso en SUBASTA judicial") == "аукцион"
    assert deal_kind_of("venta en subastas BOE") == "аукцион"
    assert deal_kind_of("puja mínima 50.000") == "аукцион"
    assert deal_kind_of("Nuda propiedad, vivienda") == "nuda propiedad"
    assert deal_kind_of("con usufructo vitalicio") == "nuda propiedad"
    assert deal_kind_of("Piso luminoso con terraza", None, "") is None
    assert deal_kind_of("pujante barrio, subastero") is None  # whole words only


def test_the_card_and_the_question_name_the_deal_kind() -> None:
    deviation = Deviation("deal_kind", phrase="аукцион")
    assert card_line(deviation) == "≈ Отличается: аукцион"
    assert similar_question(deviation, exact_found=True) == "Есть ещё варианты другого вида сделки (аукцион). Показать?"
    assert "другого вида сделки (nuda propiedad)" in similar_question(Deviation("deal_kind", phrase="nuda propiedad"),
                                                                       exact_found=False)
    assert reason_category("deal_kind") == "deal_kind"


async def _runner(auction: bool):
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={OWNER},
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=0,
                                                auction_deviation=auction))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=CHAT, requested_by=USER, source_text=GOAL,
                                 actor=f"telegram:{USER}")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    settings = ControlPlaneSettings(telegram_token="t", database_url="postgresql://x",
                                    operator_user_ids=frozenset({OWNER}))

    async def sink(envelope) -> None:
        return None

    return store, messenger, runner, cid, ControlPlane(settings, MemoryControlPlaneStore(), None, sink, offers=store.desk)


def _auction(store: MemoryRunStore, cid: str, fid: str, text: str) -> None:
    store.add_finding(cid, fid, f"🏠 {fid}", payload=listing(45_000), original=text, vertical="real_estate")


async def test_an_auction_is_similar_with_the_flag_and_exact_without_it() -> None:
    store, messenger, runner, cid, control = await _runner(False)
    _auction(store, cid, "a1", "Piso en subasta judicial, 45.000 EUR")
    await runner.tick()
    assert store.buckets["a1"][0] == "exact" and len(cards(messenger)) == 1  # the flag is off: as before

    store, messenger, runner, cid, control = await _runner(True)
    _auction(store, cid, "a1", "Piso en subasta judicial, 45.000 EUR")
    _auction(store, cid, "a2", "Nuda propiedad de piso, 45.000 EUR")
    _auction(store, cid, "ok", "Piso reformado, 45.000 EUR")
    await runner.tick()
    assert [store.buckets[f][0] for f in ("a1", "a2", "ok")] == ["similar", "similar", "exact"]
    assert len(cards(messenger)) == 1 and "Квартира" in cards(messenger)[0]  # only the ordinary flat was sent
    assert messenger.asks[0][1].startswith("Есть ещё варианты другого вида сделки (")
    await press(control, USER, messenger.asks[0][2][0][1])
    await runner.tick()
    sent = cards(messenger)
    assert len(sent) == 3 and sum(c.startswith("≈ Отличается: аукцион") for c in sent) == 1
    assert sum(c.startswith("≈ Отличается: nuda propiedad") for c in sent) == 1
    outcomes = {(o.bucket, o.why) for o in await store.outcome_counts(cid)}
    assert ("similar", "deal_kind") in outcomes


async def test_an_auction_is_not_a_deviation_for_a_rental_or_an_investor_search() -> None:
    store, _messenger, runner, cid, _ = await _runner(True)
    store.add_finding(cid, "r1", "x", payload=listing(500, deal="rent"), original="alquiler, puja", vertical="real_estate")
    await runner.tick()
    assert store.buckets["r1"][0] == "excluded"
    assert runner._deal_kind(Request(deal="rent"), StreamFinding("x", "", original="subasta", vertical="real_estate")) is None
    assert runner._deal_kind(Request(deal="sale"), StreamFinding("x", "", original="subasta", vertical="investors")) is None


# --- (б) spend outside the campaign ----------------------------------------------------------------


def test_the_report_names_the_spend_outside_the_campaign_only_when_there_is_some() -> None:
    spent = costs.CostSummary({"llm": 0.5}, {}, off_campaign={"anthropic/claude-opus-5.5": 7.07, "x/y": 0.2})
    lines = cost_lines(spent)
    assert lines[1] == "Вне кампании за это время: $7.27 (модели: anthropic/claude-opus-5.5 $7.07, x/y $0.20)"
    assert cost_lines(costs.CostSummary({"llm": 0.5}, {}, off_campaign={"x": 0.0})) == ["💶 Расход: $0.50 (ИИ $0.50)"]
    assert costs.CostSummary({"llm": 0.5}).off_campaign == {}


async def test_the_ledger_sums_llm_costs_without_a_campaign_inside_the_window() -> None:
    t0 = datetime(2026, 10, 17, 10, tzinfo=UTC)
    ledger = costs.MemoryLedger()
    for entry in (
        costs.Entry(None, "llm", "cost", item="opus", cost_usd=3.0, at=t0 + timedelta(minutes=5)),
        costs.Entry(None, "llm", "cost", item="opus", cost_usd=4.0, at=t0 + timedelta(minutes=9)),
        costs.Entry(None, "llm", "cost", item="late", cost_usd=9.0, at=t0 + timedelta(hours=3)),   # after the end
        costs.Entry(None, "llm", "cost", item="early", cost_usd=9.0, at=t0 - timedelta(hours=1)),  # before the start
        costs.Entry(None, "llm", "error", item="opus", cost_usd=9.0, at=t0 + timedelta(minutes=5)),  # not a cost
        costs.Entry(None, "fetch", "cost", item="site", cost_usd=9.0, at=t0 + timedelta(minutes=5)),  # not llm
        costs.Entry("c1", "llm", "cost", item="haiku", cost_usd=9.0, at=t0 + timedelta(minutes=5)),  # a campaign's own
    ):
        await ledger.add(entry)
    assert await ledger.off_campaign(t0, t0 + timedelta(hours=1)) == {"opus": 7.0}


async def test_the_runner_adds_the_outside_spend_to_the_summary() -> None:
    _store, _, runner, cid, _ = await _runner(False)
    campaign = await runner.campaigns.get(cid)
    ledger = costs.MemoryLedger()
    await ledger.add(costs.Entry(None, "llm", "cost", item="opus", cost_usd=2.5))
    spent = await runner._with_off_campaign(campaign, ledger, costs.CostSummary({"llm": 0.1}))
    assert spent is not None and spent.off_campaign == {"opus": 2.5} and spent.total == pytest.approx(0.1)
    assert await runner._with_off_campaign(campaign, None, None) is None

    class Broken(costs.MemoryLedger):
        async def off_campaign(self, since, until):
            raise RuntimeError("db down")

    plain = costs.CostSummary({"llm": 0.1})
    assert await runner._with_off_campaign(campaign, Broken(), plain) is plain


# --- (в) an API portal that returned nothing --------------------------------------------------------


async def test_a_portal_whose_api_run_found_nothing_is_not_called_unreached() -> None:
    web = MemoryWebStore()
    web.sources[("c1", "apify_idealista")] = SourceRun("c1", "apify_idealista", "completed")
    web.sources[("c1", "apify_fotocasa")] = SourceRun("c1", "apify_fotocasa", "failed", error_code="x")
    web.sources[("c1", "apify_pisos")] = SourceRun("c1", "apify_pisos", "completed", listings=(
        SourceListing(url="https://www.pisos.com/a/", title="t"),))
    reports = await web.site_report("c1")
    assert {r.host for r in reports if r.api_empty} == {"idealista.com"}
    lines, nothing = site_lines([], reports, ["idealista.com", "fotocasa.es"])
    assert "Idealista — 0 объявлений по запросу" in lines
    assert "Fotocasa — до него не дошла очередь запросов" in lines
    assert nothing == []
    # no source run: the old wording stays
    plain, _ = site_lines([], [SiteReport("idealista.com")], ["idealista.com"])
    assert plain == ["Idealista — до него не дошла очередь запросов"]


# --- (г) the best variants ---------------------------------------------------------------------------


def _sent(fid: str, price: int, number: int, rooms: int = 2) -> SentFinding:
    payload = {"price_amount": price, "price_currency": "EUR", "deal_type": "sale", "property_type": "apartment",
               "rooms": rooms, "area_m2": 70, "location": "Valencia", "original_post_link": f"https://x.es/{fid}"}
    return SentFinding(StreamFinding(fid, "t", payload=payload, vertical="real_estate"), None, number)


async def test_the_best_variants_are_exact_first_then_cheaper_first() -> None:
    request = Request(amount=200_000, deal="sale", location="Valencia", rooms=2)
    sent = [_sent("dear", 190_000, 1), _sent("cheap", 120_000, 2), _sent("mid", 150_000, 3),
            _sent("over", 215_000, 4)]  # +7.5 %: inside the 10 % band, still exact
    sent.append(_sent("similar", 230_000, 5))  # +15 %: similar
    assert [r.card.finding.id for r in rank_cards(sent, request)] == ["cheap", "mid", "dear", "over", "similar"]
    # a similar card never outranks an exact one, even a cheaper one
    cheap_similar = _sent("sim2", 100_000, 6, rooms=1)  # fewer rooms than asked: other
    assert rank_cards([cheap_similar, _sent("dear", 190_000, 1)], request)[0].card.finding.id == "dear"


# --- (д) advice --------------------------------------------------------------------------------------


def test_the_price_is_never_a_question_for_the_seller() -> None:
    held = [HeldEvidence(StreamFinding("f", "t"), {"criteria": [
        {"name": "budget", "verdict": "unknown"}, {"name": "price", "verdict": "unknown"},
        {"name": "must_have:terraza", "verdict": "unknown"}]})]
    actions = evidence_actions(held)
    assert actions == ["Уточните у продавца условие «terraza» для 1 варианта."]
    assert not any("цена" in a for a in actions)


def test_raising_the_budget_is_not_advised_when_the_task_already_allows_it() -> None:
    facts = {"counts": {"sent_exact": 1}, "rejected_by_reason": {"budget": 5}, "unreadable_sites": [],
             "task": task_facts(Request(amount=250_000, is_max=True, budget_pct=10, deal="sale"))}
    assert task_facts(Request(amount=250_000, budget_pct=10))["budget_pct"] == 10
    assert not any("бюджет" in t for t in fallback_recommendations(facts))
    open_facts = {**facts, "task": task_facts(Request(amount=250_000, is_max=True, deal="sale"))}
    assert any("поднимите бюджет на 10 %" in t for t in fallback_recommendations(open_facts))
    from bot.campaign.final_report import clean_recommendations
    model = ["Поднимите бюджет на 10 %: 5 вариантов дороже.", "Добавьте соседний район: 3 варианта.",
             "Поищите позже: 2 дня."]
    assert "Поднимите бюджет на 10 %: 5 вариантов дороже." not in clean_recommendations(model, facts)
    assert "Поднимите бюджет на 10 %: 5 вариантов дороже." in clean_recommendations(model, open_facts)
