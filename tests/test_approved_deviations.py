"""Deviations approved in the interview: the bucketing, the reviewer, the card line and the similar stream."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from bot.agents.reviewer import review_task, tolerance_pct
from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.relevance import review_match
from bot.campaign.runner import APPROVED_LINE, CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.tolerance import Match, Request, classify, request_for
from tests.test_near_match import CHAT, GOAL, USER, ButtonMessenger, add, cards, listing

pytestmark = pytest.mark.asyncio


def valencia(**kw: Any) -> Request:
    return Request(amount=200_000, deal="sale", location="Valencia", **kw)


def offer(price: float, **kw: Any) -> dict[str, Any]:
    return listing(price, location=kw.pop("location", "Valencia, Ruzafa"), **kw)


# --- tolerance ---------------------------------------------------------------------------------------


def test_a_price_inside_the_approved_budget_band_is_exact_with_a_note() -> None:
    request = valencia(budget_pct=10)
    match = classify(offer(216_000), request)  # +8 %
    assert (match.bucket, match.why, match.note) == ("exact", "approved_deviation", "бюджет +8 %")
    assert classify(offer(230_000), request).bucket == "similar"  # +15 %: beyond the approved band
    assert classify(offer(190_000), request) == Match("exact")  # cheaper than a maximum: nothing to approve
    # Without approved deviations the old fixed 10 % band stays and says nothing.
    assert classify(offer(216_000), valencia()) == Match("exact", pytest.approx(0.08))
    # «Только точные» (0 %) tightens the band to the budget itself.
    assert classify(offer(216_000), valencia(budget_pct=0)).bucket == "similar"


def test_area_rooms_and_a_neighbouring_district_are_approved_too() -> None:
    area = classify(offer(150_000), Request(deal="sale", location="Valencia", min_area=100, area_pct=20))
    assert area.bucket == "similar"  # no area in the payload: unverified, never exact
    smaller = classify({**offer(150_000), "area_m2": 85}, Request(deal="sale", location="Valencia", min_area=100, area_pct=20))
    assert (smaller.bucket, smaller.why) == ("exact", "approved_deviation") and "85 м²" in (smaller.note or "")
    assert classify({**offer(150_000), "area_m2": 85}, Request(deal="sale", location="Valencia", min_area=100)).bucket == "similar"
    rooms = classify({**offer(150_000), "rooms": 1}, Request(deal="sale", location="Valencia", rooms=2, rooms_delta=1))
    assert (rooms.bucket, rooms.note) == ("exact", "комнат: 1 при запросе от 2")
    assert classify({**offer(150_000), "rooms": 1}, Request(deal="sale", location="Valencia", rooms=2)).bucket == "other"
    near = Request(deal="sale", location="Valencia", nearby=("Patraix",), districts=("Ruzafa",))
    patraix = classify(offer(150_000, location="Patraix"), near)
    assert (patraix.bucket, patraix.why, patraix.note) == ("exact", "approved_deviation", "район Patraix (соседний)")
    assert classify(offer(150_000, location="Patraix, Valencia"), near).why is None  # the city is named: no deviation
    assert classify(offer(150_000, location="Ruzafa"), near).why is None  # the asked district itself
    elsewhere = classify(offer(150_000, location="Barcelona, Gràcia"), near)
    assert elsewhere.bucket == "other" and elsewhere.why == "location"
    # A known neighbouring city that is approved is the requested place.
    alicante = classify(offer(150_000, location="Alicante"), Request(deal="sale", location="Valencia", nearby=("Alicante",)))
    assert alicante.bucket == "exact" and alicante.note == "район Alicante (соседний)"


def test_request_and_reviewer_read_the_deviations_from_the_spec() -> None:
    plan = plan_campaign(GOAL)
    deviations = {"budget_pct": 12, "area_pct": 15, "rooms_delta": 1, "nearby_areas": ["Patraix"], "asked": True}
    request = request_for(plan.constraints, location=plan.location, vertical=plan.vertical, deviations=deviations)
    assert (request.budget_pct, request.area_pct, request.rooms_delta, request.nearby) == (12.0, 15.0, 1, ("Patraix",))
    assert request_for(plan.constraints, location=plan.location, vertical=plan.vertical).budget_pct is None
    campaign = SimpleNamespace(spec={"deviations": deviations, "context": {"answers": [{"question": "Q?", "answer": "A"}]}},
                               plan=plan, source_text=GOAL)
    assert tolerance_pct(campaign) == 12  # type: ignore[arg-type]
    assert tolerance_pct(SimpleNamespace(spec={"deviations": {"budget_pct": 0}})) == 0  # type: ignore[arg-type]
    assert tolerance_pct(SimpleNamespace(spec={"deviations": {"asked": True}})) == 10  # type: ignore[arg-type]
    task = review_task(campaign)  # type: ignore[arg-type]
    assert task["deviations"] == {"budget_pct": 12, "area_pct": 15, "rooms_delta": 1, "nearby_areas": ["Patraix"]}
    assert task["context"] == [{"q": "Q?", "a": "A"}] and task["tolerance_pct"] == 12


def test_an_approved_deviation_is_not_a_reviewer_fail() -> None:
    rules = Match("exact", 0.08, "approved_deviation", note="бюджет +8 %", covers=("budget",))
    budget_fail = {"overall": "reject", "criteria": [
        {"name": "place", "verdict": "pass"}, {"name": "budget", "verdict": "fail", "quote": "216.000 €"}]}
    assert review_match(rules, budget_fail) == rules
    other_fail = {"overall": "reject", "criteria": [
        {"name": "budget", "verdict": "fail", "quote": "216.000 €"}, {"name": "rooms", "verdict": "fail", "quote": "1 hab"}]}
    assert review_match(rules, other_fail).bucket == "excluded"
    assert review_match(rules, {"overall": "match", "criteria": [{"name": "budget", "verdict": "pass"}]}) == rules


# --- the runner --------------------------------------------------------------------------------------


async def setup(spec: dict[str, Any] | None):
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7},
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=0))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=CHAT, requested_by=USER, source_text=GOAL,
                                 actor=f"telegram:{USER}", spec=spec)
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)  # the search keeps running
    return campaigns, store, messenger, runner, cid


async def test_a_card_inside_the_approved_deviation_says_what_differs() -> None:
    _, store, messenger, runner, cid = await setup({"deviations": {"budget_pct": 10, "asked": True}})
    add(store, cid, "f54", 54_000)  # +8 % of 50 000
    add(store, cid, "f50", 49_000)
    await runner.tick()
    sent = cards(messenger)
    assert len(sent) == 2 and messenger.asks == []  # no «Одобрить?» question for an approved deviation
    marked = next(c for c in sent if "54 000" in c)
    assert marked.startswith(f"{APPROVED_LINE}бюджет +8 %\n\n") and store.buckets["f54"][0] == "exact"
    assert not next(c for c in sent if "49 000" in c).startswith("≈")


async def test_approved_deviations_stream_similar_cards_during_the_search() -> None:
    _, store, messenger, runner, cid = await setup({"deviations": {"budget_pct": 10, "asked": True}})
    add(store, cid, "f60", 60_000)  # +20 %: beyond the approved band, still similar
    await runner.tick()
    await runner.tick()
    assert messenger.asks == [] and len(cards(messenger)) == 1  # no question; the card streams at once
    assert cards(messenger)[0].startswith("≈ Чуть дороже запроса: ~60 000 € при запросе ~50 000 €")
    assert await store.offer_state(cid, "similar") == "approved"
    add(store, cid, "f58", 58_000)
    await runner.tick()
    assert len(cards(messenger)) == 2 and messenger.asks == []


async def test_without_deviations_the_similar_question_comes_with_the_first_similar_finding() -> None:
    _, store, messenger, runner, cid = await setup(None)
    add(store, cid, "f52", 52_000)
    add(store, cid, "f60", 60_000)
    await runner.tick()
    assert len(cards(messenger)) == 1 and len(messenger.asks) == 1  # asked while the search is running
    assert messenger.asks[0][1].startswith("Есть ещё похожие варианты")
    assert await store.offer_state(cid, "similar") == "asked"


async def test_only_exact_is_never_auto_approved() -> None:
    _, store, messenger, runner, cid = await setup(
        {"deviations": {"budget_pct": 0, "area_pct": 0, "asked": True}})
    add(store, cid, "f54", 54_000)  # 8 % over: similar when only exact is allowed
    await runner.tick()
    await runner.tick()
    assert cards(messenger) == [] and len(messenger.asks) == 1 and await store.offer_state(cid, "similar") == "asked"
    # An interview that never reached the deviation question behaves the same.
    _, store, messenger, runner, cid = await setup({"deviations": {"asked": False}})
    add(store, cid, "f60", 60_000)
    await runner.tick()
    assert cards(messenger) == [] and len(messenger.asks) == 1
