"""Q9 (RELEVANCE_SOFT_WISHES): soft wishes go to the reviewer apart and never lower «exact» to «similar».

Off: the reviewer-v3 prompt, schema, task data and decision are exactly as before. On: the wishes are sent as
``task.wishes`` under ``reviewer-v3-soft``; an all-pass matrix stays exact even when the model said ``near`` because
of a wish; a hard ``unknown`` or ``fail`` still holds or excludes. Only mocks (``httpx.MockTransport``).
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from bot.agents.reviewer import (
    PROMPT_VERSION,
    PROMPT_VERSION_SOFT,
    SCHEMA,
    SCHEMA_SOFT,
    SYSTEM,
    SYSTEM_SOFT,
    OpenRouterReviewer,
    review_task,
)
from bot.campaign import plan_campaign
from bot.campaign.relevance import ReviewerJudge, finding_data, review_match, task_data
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.store import MemoryCampaignStore
from bot.campaign.tolerance import Match
from tests.test_campaign_relevance import CHAT, TASK, USER, add, plot
from tests.test_campaign_runner import ButtonMessenger
from tests.test_reviewer import settings, valencia

# sha256 of reviewer.SYSTEM at a240f22: the flag off must send the very same prompt.
SYSTEM_V3_SHA256 = "dcfe0a2bc6d951aaa6eaf74a16fb8f93ce3ac8436f7f9d2319f2c35a661b1a51"

SPEC = {"deal": "sale", "property_type": "land", "area_m2": {"min": 2000},
        "place": {"name": "Madrid", "level": "city", "country": "ES"},
        "wishes": [{"text": "вид на горы", "weight": 2}, {"text": "рядом со школой", "weight": 1}],
        "must_have": [], "delivery": {"show_similar": False}}


def chat(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def model_answer(criteria: list[str], *, verdicts: dict[str, str] | None = None, overall: str = "near",
                 deviation: str | None = "без вида на горы", wishes: bool = True) -> str:
    """What the model said in the pilot: every hard criterion passes, but «near» because a wish is not met."""
    verdicts = verdicts or {}
    rows = [{"name": n, "verdict": verdicts.get(n, "pass"),
             "quote": "Parcela en Madrid" if verdicts.get(n, "pass") != "unknown" else None, "note_ru": "ок"}
            for n in criteria]
    data = {"criteria": rows, "overall": overall, "deviation_ru": deviation, "confidence": 0.8}
    if wishes:
        data["wishes"] = [{"text": "вид на горы", "verdict": "fail", "quote": None},
                          {"text": "рядом со школой", "verdict": "unknown", "quote": None}]
    return json.dumps(data, ensure_ascii=False)


def reviewer(soft: bool, seen: list[dict], **answer) -> OpenRouterReviewer:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        task = json.loads(body["messages"][1]["content"].split("\n", 1)[1])["task"]
        return chat(model_answer(task["criteria"], **answer))

    return OpenRouterReviewer(api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                              soft_wishes=soft)


FINDING = finding_data({"summary_ru": "Участок 2500 м² в Мадриде", "price_amount": 450_000, "price_currency": "EUR",
                        "area_m2": 2500, "location": "Madrid"},
                       original="Parcela en Madrid, 2.500 m2, 450.000 €", review=True, vertical="real_estate")


# --- the flag ------------------------------------------------------------------------------------------------------


def test_the_flag_is_off_by_default_and_reaches_the_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RELEVANCE_SOFT_WISHES", raising=False)
    off = settings()
    assert off.relevance_soft_wishes is False
    judge = off.relevance_judge()
    assert isinstance(judge, ReviewerJudge) and judge.soft_wishes is False and judge.reviewer.soft_wishes is False
    on = settings(RELEVANCE_SOFT_WISHES="true").relevance_judge()
    assert on.soft_wishes is True and on.reviewer.soft_wishes is True


def test_the_soft_prompt_differs_and_keeps_strict_json() -> None:
    assert hashlib.sha256(SYSTEM.encode()).hexdigest() == SYSTEM_V3_SHA256  # flag off: the old prompt, byte for byte
    assert PROMPT_VERSION == "reviewer-v3" and PROMPT_VERSION_SOFT != PROMPT_VERSION
    assert SYSTEM_SOFT != SYSTEM and "SOFT WISHES" in SYSTEM_SOFT and "a wish is not met" not in SYSTEM_SOFT
    assert "everything passes and a wish is not met" in SYSTEM
    assert "wishes" not in SCHEMA["properties"] and "wishes" not in SCHEMA["required"]
    assert SCHEMA_SOFT["additionalProperties"] is False and "wishes" in SCHEMA_SOFT["required"]
    assert SCHEMA_SOFT["properties"]["wishes"]["items"]["additionalProperties"] is False
    assert set(SCHEMA_SOFT["required"]) == {*SCHEMA["required"], "wishes"}


def test_task_data_carries_the_wishes_only_with_the_flag() -> None:
    campaign = valencia(SPEC)
    off = task_data(campaign, review=True)
    assert "wishes" not in off and off == task_data(campaign, review=True, soft_wishes=False)
    on = task_data(campaign, review=True, soft_wishes=True)
    assert on["wishes"] == [{"text": "вид на горы", "weight": 2}, {"text": "рядом со школой", "weight": 1}]
    assert on["criteria"] == off["criteria"]  # the hard criteria are the same; no wish becomes one
    assert not any("вид" in c or "школ" in c for c in on["criteria"])
    assert "wishes" not in review_task(valencia({**SPEC, "wishes": []}), soft_wishes=True)


# --- the model call over a fake HTTP and the decision ---------------------------------------------------------------


async def test_flag_off_sends_the_old_prompt_and_keeps_the_old_decision() -> None:
    seen: list[dict] = []
    task = task_data(valencia(SPEC), review=True)
    review = await reviewer(False, seen, wishes=False).review(task, FINDING)
    body = seen[0]
    assert body["messages"][0]["content"] == SYSTEM
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "wishes" not in json.loads(body["messages"][1]["content"].split("\n", 1)[1])["task"]
    assert review.overall == "near" and review.deviation_ru == "без вида на горы"
    stored = review.to_dict()
    assert stored["prompt"] == "reviewer-v3" and "wishes" not in stored
    assert review_match(Match("exact"), stored) == Match("similar", 0.0, "ai")  # as before: held as «similar»


async def test_flag_on_an_unmet_soft_wish_stays_exact() -> None:
    seen: list[dict] = []
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)
    review = await reviewer(True, seen).review(task, FINDING)
    body = seen[0]
    assert body["messages"][0]["content"] == SYSTEM_SOFT
    assert body["response_format"]["json_schema"]["schema"] == SCHEMA_SOFT
    assert body["response_format"]["json_schema"]["strict"] is True
    sent = json.loads(body["messages"][1]["content"].split("\n", 1)[1])["task"]
    assert sent["wishes"][0] == {"text": "вид на горы", "weight": 2}
    assert review.overall == "match" and review.deviation_ru is None
    stored = review.to_dict()
    assert stored["prompt"] == "reviewer-v3-soft"
    assert stored["wishes"] == [{"text": "вид на горы", "verdict": "fail", "quote": None},
                                {"text": "рядом со школой", "verdict": "unknown", "quote": None}]
    assert review_match(Match("exact"), stored) == Match("exact")


@pytest.mark.parametrize(("verdicts", "overall", "bucket", "why"), [
    ({"area": "unknown"}, "near", "similar", "unverified"),  # a hard unknown still holds
    ({"place": "fail"}, "reject", "excluded", "location"),  # a hard fail still excludes
])
async def test_flag_on_hard_criteria_work_as_before(verdicts, overall, bucket, why) -> None:
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)
    review = await reviewer(True, [], verdicts=verdicts, overall=overall).review(task, FINDING)
    result = review_match(Match("exact"), review.to_dict())
    assert (result.bucket, result.why) == (bucket, why)


async def test_flag_on_a_soft_answer_without_wishes_is_still_read() -> None:
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)
    review = await reviewer(True, [], wishes=False).review(task, FINDING)
    assert review.overall == "match" and review.wishes == ()


# --- the runner end to end -----------------------------------------------------------------------------------------


async def run_one(soft: bool) -> tuple[MemoryRunStore, ButtonMessenger, list[dict]]:
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    seen: list[dict] = []
    runner = CampaignRunner(campaigns, store, messenger, None,
                            config=RunnerConfig(window_cooldown_seconds=0, relevance_fail_closed=True),
                            relevance=ReviewerJudge(reviewer(soft, seen, wishes=soft)))
    plan = plan_campaign(TASK, vertical="real_estate", location="Madrid")
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=TASK, actor="telegram:42",
                                 spec=SPEC)
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    add(store, cid, "plot", plot(2500))
    await runner.tick()
    return store, messenger, seen


async def test_runner_flag_off_holds_the_plot_flag_on_sends_it() -> None:
    store, messenger, seen = await run_one(False)
    assert store.buckets["plot"][0] == "similar" and messenger.findings() == []
    assert "wishes" not in json.loads(seen[0]["messages"][1]["content"].split("\n", 1)[1])["task"]

    store, messenger, seen = await run_one(True)
    assert store.buckets["plot"][0] == "exact" and len(messenger.findings()) == 1
    sent = json.loads(seen[0]["messages"][1]["content"].split("\n", 1)[1])["task"]
    assert [w["text"] for w in sent["wishes"]] == ["вид на горы", "рядом со школой"]
