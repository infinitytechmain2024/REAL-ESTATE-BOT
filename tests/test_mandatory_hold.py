"""H11: a finding whose must-have criterion is not mentioned is held and never offered (CAMPAIGN_MANDATORY_UNKNOWN_HOLD)."""

from __future__ import annotations

from bot.campaign.final_report import HeldEvidence, mandatory_note, report_text, tally
from bot.campaign.relevance import MANDATORY_UNKNOWN, Relevance, review_match
from bot.campaign.runs import OutcomeCount
from bot.campaign.tolerance import Match
from tests.test_campaign_relevance import FakeJudge, add, plot, setup


def verdict(*unknown: str, kind: str = "must_have") -> Relevance:
    criteria = [{"name": "place", "verdict": "pass"}] + [
        {"name": f"{kind}:{n}" if kind else n, "verdict": "unknown"} for n in unknown]
    return Relevance("near", "нет упоминания", review={"overall": "near", "criteria": criteria}, model="fake")


RULES = Match("exact", 0.0)


def test_review_match_marks_only_must_have_unknown_and_only_with_the_flag() -> None:
    review = verdict("терраса").review
    held = review_match(RULES, review, hold_mandatory=True)
    assert (held.bucket, held.why, held.note) == ("similar", MANDATORY_UNKNOWN, "Не подтверждено: обязательно: терраса")
    assert review_match(RULES, review).why == "unverified"  # flag off: as before
    optional = {"overall": "near", "criteria": [{"name": "area", "verdict": "unknown"}]}
    assert review_match(RULES, optional, hold_mandatory=True).why == "unverified"


async def test_mandatory_unknown_is_held_not_offered_and_not_sent() -> None:
    _, store, messenger, runner, cid = await setup(FakeJudge(verdict("терраса")), mandatory_unknown_hold=True)
    add(store, cid, "a", plot(2500))
    await runner.tick()
    assert messenger.findings() == [] and messenger.asks == []
    assert store.buckets["a"][0] == "similar" and store.whys["a"] == MANDATORY_UNKNOWN
    assert "Не подтверждено" in (store.hold_reasons["a"] or "") and "терраса" in (store.hold_reasons["a"] or "")


async def test_optional_unknown_is_still_offered() -> None:
    _, store, messenger, runner, cid = await setup(FakeJudge(verdict("площадь", kind="")), mandatory_unknown_hold=True)
    add(store, cid, "a", plot(2500))
    await runner.tick()
    assert store.whys["a"] == "unverified" and len(messenger.asks) == 1


async def test_flag_off_keeps_the_old_offer() -> None:
    _, store, messenger, runner, cid = await setup(FakeJudge(verdict("терраса")))
    add(store, cid, "a", plot(2500))
    await runner.tick()
    assert store.whys["a"] == "unverified" and len(messenger.asks) == 1


async def test_mandatory_held_ones_do_not_join_the_question_of_the_others() -> None:
    judge = FakeJudge(verdict("терраса"), verdict("площадь", kind=""))
    _, store, messenger, runner, cid = await setup(judge, mandatory_unknown_hold=True)
    add(store, cid, "a", plot(2500))
    add(store, cid, "b", plot(2600))
    await runner.tick()
    assert len(messenger.asks) == 1 and "терраса" not in str(messenger.asks[0])


def test_report_says_how_many_were_held_for_the_mandatory_criterion() -> None:
    counts = tally([OutcomeCount("held", "similar", MANDATORY_UNKNOWN, 3), OutcomeCount("held", "similar", "budget", 1)])
    assert counts.held_must == 3 and counts.held_similar == 4
    review = verdict("терраса").review
    held = [HeldEvidence(None, review)]  # type: ignore[arg-type]
    assert mandatory_note(counts, held) == "Ещё 3 без упоминания: терраса"
    assert mandatory_note(tally([OutcomeCount("held", "similar", "budget", 1)]), held) is None
    text = report_text("цель", counts, [], [], [], [], must_note=mandatory_note(counts, held))
    assert "Похожие, не показаны: 4" in text and "Ещё 3 без упоминания: терраса" in text
