"""Q71: a finding inside the approved deviations is not a confirmed match (TOLERANCE_HITS_AS_SIMILAR).

Sim b6-n3-s7 («квартира в Мадриде от 70 м²», ±10 % approved): El Pardo, 65 м², went out as exact with «≈ в пределах
отступления» and the report counted it among «Подтверждённых совпадений».
"""

from __future__ import annotations

from typing import Any

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.final_report import facts_for_model, report_text, tally
from bot.campaign.runner import APPROVED_LINE, CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore, OutcomeCount
from bot.campaign.settings import CampaignRunnerSettings
from bot.campaign.tolerance import TOLERANCE_WHY, Request, classify, tolerance_hit
from tests.test_near_match import ButtonMessenger, cards, listing

GOAL = "Купить квартиру в Мадриде от 70 м² до 400 000 €"
CHAT, USER = 4242, 42
DEVIATIONS = {"deviations": {"budget_pct": 10, "area_pct": 10, "asked": True}}


def flat(area: float, price: float = 375_000) -> dict[str, Any]:
    return {**listing(price, location="El Pardo, Fuencarral, Madrid"), "area_m2": area, "rooms": 3}


async def setup(flag: bool):
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, owner_ids={7},
                            config=RunnerConfig(relevance_fail_closed=False, window_cooldown_seconds=0,
                                                tolerance_hits_as_similar=flag))
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=CHAT, requested_by=USER, source_text=GOAL,
                                 actor=f"telegram:{USER}", spec=DEVIATIONS)
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)  # the search keeps running
    for fid, area in (("f65", 65), ("f70", 70)):
        store.add_finding(cid, fid, f"🏠 {fid}", payload=flat(area), original=f"post {fid}", vertical="real_estate")
    await runner.tick()
    await runner.tick()
    return store, messenger, cid


def test_tolerance_hit_turns_only_an_approved_deviation_into_similar() -> None:
    request = Request(amount=400_000, deal="sale", location="Madrid", min_area=70, area_pct=10)
    inside = classify(flat(65), request)
    assert (inside.bucket, inside.why) == ("exact", "approved_deviation")  # the rules are unchanged
    hit = tolerance_hit(inside)
    assert hit is not None and (hit.bucket, hit.why) == ("similar", TOLERANCE_WHY)
    assert hit.note == "в пределах отступления: площадь ~65 м² при запросе от 70 м²"
    assert tolerance_hit(classify(flat(70), request)) is None  # strictly exact stays exact
    assert tolerance_hit(classify(flat(60), request)) is None  # beyond the band: the usual similar


@pytest.mark.asyncio
async def test_flag_off_a_tolerance_hit_is_exact_as_before() -> None:
    store, messenger, cid = await setup(False)
    sent = cards(messenger)
    assert len(sent) == 2 and messenger.asks == []
    assert store.buckets["f65"][0] == "exact" and store.buckets["f70"][0] == "exact"
    assert tally(await store.outcome_counts(cid)).sent_exact == 2


@pytest.mark.asyncio
async def test_flag_on_a_tolerance_hit_is_similar_sent_at_once_and_counted_apart() -> None:
    store, messenger, cid = await setup(True)
    sent = cards(messenger)
    assert len(sent) == 2 and messenger.asks == []  # approved in advance: no «Показать?»
    assert store.buckets["f65"][0] == "similar" and store.whys["f65"] == TOLERANCE_WHY
    assert store.buckets["f70"][0] == "exact"
    marked = next(c for c in sent if "65 м²" in c)
    assert marked.startswith(f"{APPROVED_LINE}площадь ~65 м² при запросе от 70 м²\n\n")
    assert await store.offer_state(cid, "similar") is None  # never offered as a held similar one
    counts = tally(await store.outcome_counts(cid))
    assert (counts.sent_exact, counts.sent_approved, counts.sent_tolerance, counts.sent) == (1, 0, 1, 2)
    text = report_text("квартира · Мадрид", counts, [], [], [], [])
    assert "Подтверждённых совпадений: 1.\nВ пределах ваших отступлений: 1." in text
    assert "Отправлено вам: 2 (из них 1 — в пределах ваших отступлений)" in text
    assert facts_for_model({}, counts, [], [])["counts"]["sent_within_approved_deviations"] == 1


@pytest.mark.asyncio
async def test_a_failed_send_of_a_tolerance_hit_is_picked_up_again_not_held() -> None:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    cid = await campaigns.create(plan_campaign(GOAL), chat_id=CHAT, requested_by=USER, source_text=GOAL,
                                 actor=f"telegram:{USER}", spec=DEVIATIONS)
    store.add_finding(cid, "f65", "🏠 f65", payload=flat(65), original="post", vertical="real_estate")
    assert await store.claim_finding(cid, "f65", "similar", TOLERANCE_WHY) == 1
    await store.release_finding("f65")
    assert "f65" not in store.buckets and "f65" not in store.held.get(cid, {})
    assert [f.id for f in await store.unstreamed_findings(cid, 10)] == ["f65"]


def test_report_counts_approved_and_tolerance_apart() -> None:
    counts = tally([OutcomeCount("sent", "exact", None, 3), OutcomeCount("sent", "similar", "approved", 2),
                    OutcomeCount("sent", "similar", TOLERANCE_WHY, 1)])
    assert (counts.sent_exact, counts.sent_approved, counts.sent_tolerance) == (3, 2, 1)
    text = report_text("квартира · Мадрид", counts, [], [], [], [])
    assert "Отправлено вам: 6 (из них 2 похожих — по вашему согласию; 1 — в пределах ваших отступлений)" in text
    plain = tally([OutcomeCount("sent", "exact", None, 3), OutcomeCount("sent", "similar", "price", 2)])
    assert "sent_within_approved_deviations" not in facts_for_model({}, plain, [], [])["counts"]
    assert "Отправлено вам: 5 (из них 2 похожих — по вашему согласию)" in report_text("к", plain, [], [], [], [])


def test_setting_reads_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOLERANCE_HITS_AS_SIMILAR", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert CampaignRunnerSettings(_env_file=None).runner_config().tolerance_hits_as_similar is False
    monkeypatch.setenv("TOLERANCE_HITS_AS_SIMILAR", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().tolerance_hits_as_similar is True
