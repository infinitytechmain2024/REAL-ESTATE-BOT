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
                 deviation: str | None = "без вида на горы", wishes: bool = True,
                 wish_rows: object = ..., near_reason: object = "wish") -> str:
    """What the model said in the pilot: every hard criterion passes, but «near» because a wish is not met."""
    verdicts = verdicts or {}
    rows = [{"name": n, "verdict": verdicts.get(n, "pass"),
             "quote": "Parcela en Madrid" if verdicts.get(n, "pass") != "unknown" else None, "note_ru": "ок"}
            for n in criteria]
    data = {"criteria": rows, "overall": overall, "deviation_ru": deviation, "confidence": 0.8}
    if near_reason is not ...:
        data["near_reason"] = near_reason
    if wish_rows is not ...:  # an explicit value, None included
        data["wishes"] = wish_rows
    elif wishes:
        data["wishes"] = [{"id": 1, "text": "вид на горы", "verdict": "fail", "quote": None},
                          {"id": 2, "text": "рядом со школой", "verdict": "unknown", "quote": None}]
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
    assert SYSTEM_SOFT != SYSTEM and "SOFT WISHES" in SYSTEM_SOFT and "everything passes and a wish is not met" not in SYSTEM_SOFT
    assert "everything passes and a wish is not met" in SYSTEM
    assert "wishes" not in SCHEMA["properties"] and "wishes" not in SCHEMA["required"]
    assert SCHEMA_SOFT["additionalProperties"] is False and "wishes" in SCHEMA_SOFT["required"]
    assert SCHEMA_SOFT["properties"]["wishes"]["items"]["additionalProperties"] is False
    assert set(SCHEMA_SOFT["required"]) == {*SCHEMA["required"], "wishes", "near_reason"}
    assert SCHEMA_SOFT["properties"]["near_reason"]["enum"] == ["wish", "other", None]
    assert "id" in SCHEMA_SOFT["properties"]["wishes"]["items"]["required"]
    assert '"wish"' in SYSTEM_SOFT and '"other"' in SYSTEM_SOFT and "near_reason" in SYSTEM_SOFT


def test_task_data_carries_the_wishes_only_with_the_flag() -> None:
    campaign = valencia(SPEC)
    off = task_data(campaign, review=True)
    assert "wishes" not in off and off == task_data(campaign, review=True, soft_wishes=False)
    on = task_data(campaign, review=True, soft_wishes=True)
    assert on["wishes"] == [{"id": 1, "text": "вид на горы", "weight": 2},
                            {"id": 2, "text": "рядом со школой", "weight": 1}]
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
    assert sent["wishes"][0] == {"id": 1, "text": "вид на горы", "weight": 2}
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


async def test_flag_on_near_without_wishes_in_the_answer_stays_near() -> None:
    # e.g. «в пригороде, а не в самом Мадриде»: the model's near is not about a wish, so it is kept.
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)
    review = await reviewer(True, [], wishes=False, deviation="в пригороде").review(task, FINDING)
    assert review.overall == "near" and review.deviation_ru == "в пригороде" and review.wishes == ()
    assert review_match(Match("exact"), review.to_dict()).bucket == "similar"


async def test_flag_on_near_when_every_wish_is_met_stays_near() -> None:
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)
    rows = [{"text": "вид на горы", "verdict": "pass", "quote": "vistas"},
            {"text": "рядом со школой", "verdict": "pass", "quote": None}]
    review = await reviewer(True, [], wish_rows=rows, deviation="таунхаус вместо дома").review(task, FINDING)
    assert review.overall == "near" and review.deviation_ru == "таунхаус вместо дома"


async def test_flag_on_near_in_a_task_without_wishes_stays_near() -> None:
    task = task_data(valencia({**SPEC, "wishes": []}), review=True, soft_wishes=True)
    assert "wishes" not in task
    review = await reviewer(True, [], deviation="промоция из нескольких квартир").review(task, FINDING)
    assert review.overall == "near" and review.wishes == ()  # the model's rows of wishes the task has not are dropped


async def test_flag_on_an_unmet_weight_3_wish_keeps_near() -> None:
    assert "weight 3" in SYSTEM_SOFT and "очень важно" in SYSTEM_SOFT
    spec = {**SPEC, "wishes": [{"text": "вид на горы", "weight": 3}, {"text": "рядом со школой", "weight": 1}]}
    task = task_data(valencia(spec), review=True, soft_wishes=True)
    for verdict in ("fail", "unknown"):
        rows = [{"text": "вид на горы", "verdict": verdict, "quote": None},
                {"text": "рядом со школой", "verdict": "fail", "quote": None}]
        review = await reviewer(True, [], wish_rows=rows).review(task, FINDING)
        assert review.overall == "near" and review.deviation_ru == "без вида на горы"
    # the weight-3 wish met, only the weight-1 one not: exact
    rows = [{"text": "вид на горы", "verdict": "pass", "quote": "vistas"},
            {"text": "рядом со школой", "verdict": "fail", "quote": None}]
    assert (await reviewer(True, [], wish_rows=rows).review(task, FINDING)).overall == "match"
    # the weight-3 wish not reported at all: not proven, near stays
    rows = [{"text": "рядом со школой", "verdict": "fail", "quote": None}]
    assert (await reviewer(True, [], wish_rows=rows).review(task, FINDING)).overall == "near"


@pytest.mark.parametrize("rows", [
    "вид на горы", None, 42, {"text": "вид на горы"},  # not a list
    ["вид на горы", 3, None, ["x"]],  # rows that are not objects
    [{"verdict": "fail"}, {"text": None, "verdict": "fail"}, {"text": "  ", "verdict": "fail"}],  # no text
    [{"text": "бассейн", "verdict": "fail"}],  # a wish the task does not have
])
async def test_flag_on_garbage_wishes_are_dropped_and_near_stays(rows) -> None:
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)
    review = await reviewer(True, [], wish_rows=rows).review(task, FINDING)
    assert review.overall == "near" and review.wishes == ()
    assert review.to_dict()["wishes"] == []


async def test_flag_on_garbage_rows_next_to_a_real_one_keep_only_the_real_one() -> None:
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)
    rows = ["x", {"verdict": "fail"}, {"text": "Вид на горы.", "verdict": "FAIL", "quote": None},
            {"text": "вид на горы", "verdict": "pass"}]  # the duplicate is ignored
    review = await reviewer(True, [], wish_rows=rows).review(task, FINDING)
    assert review.overall == "match"
    assert review.to_dict()["wishes"] == [{"text": "вид на горы", "verdict": "fail", "quote": None}]


@pytest.mark.parametrize(("response", "code"), [
    (httpx.Response(503), "http_503"),
    (chat("not json"), "invalid_response"),
    (chat(json.dumps({"criteria": [], "overall": "maybe", "deviation_ru": None, "confidence": 1, "wishes": []})),
     "invalid_response"),
])
async def test_flag_on_an_llm_error_is_a_coded_error(response, code) -> None:
    from bot.campaign.relevance import RelevanceError

    broken = OpenRouterReviewer(api_key="k", soft_wishes=True,
                                client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: response)))
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)
    with pytest.raises(RelevanceError, match=code):
        await broken.review(task, FINDING)


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


# --- near_reason, ids, duplicates -----------------------------------------------------------------------------------

METRO = {"deal": "sale", "property_type": "apartment", "place": {"name": "Madrid", "level": "city", "country": "ES"},
         "wishes": [{"text": "рядом с метро", "weight": 1}]}


async def test_flag_on_near_for_a_suburb_with_an_unknown_wish_stays_near() -> None:
    # «квартира в Мадриде, желательно рядом с метро»: near because of a suburb, the metro wish unknown.
    task = task_data(valencia(METRO), review=True, soft_wishes=True)
    rows = [{"id": 1, "text": "рядом с метро", "verdict": "unknown", "quote": None}]
    review = await reviewer(True, [], wish_rows=rows, near_reason="other",
                            deviation="в пригороде, а не в самом Мадриде").review(task, FINDING)
    assert review.overall == "near" and review.deviation_ru == "в пригороде, а не в самом Мадриде"
    assert review.to_dict()["near_reason"] == "other"
    assert review_match(Match("exact"), review.to_dict()).bucket == "similar"


@pytest.mark.parametrize("reason", [None, ..., "", "WISHES", 1, ["wish"]])
async def test_flag_on_near_without_a_wish_reason_stays_near(reason) -> None:
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)
    review = await reviewer(True, [], near_reason=reason).review(task, FINDING)
    assert review.overall == "near" and review.deviation_ru == "без вида на горы" and review.near_reason is None


async def test_flag_on_wish_rows_match_by_id_even_when_paraphrased() -> None:
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)
    rows = [{"id": 1, "text": "с видом на горы (вид не указан)", "verdict": "fail", "quote": None},
            {"id": "2", "text": "школа рядом", "verdict": "pass", "quote": "colegio"}]
    review = await reviewer(True, [], wish_rows=rows).review(task, FINDING)
    assert review.overall == "match"
    assert review.to_dict()["wishes"] == [{"text": "вид на горы", "verdict": "fail", "quote": None},
                                          {"text": "рядом со школой", "verdict": "pass", "quote": "colegio"}]
    # an unknown id falls back to the text; a paraphrase with an unknown id is dropped
    rows = [{"id": 9, "text": "Вид на горы", "verdict": "fail", "quote": None},
            {"id": 7, "text": "школа рядом", "verdict": "fail", "quote": None}]
    review = await reviewer(True, [], wish_rows=rows).review(task, FINDING)
    assert [(w.name, w.verdict) for w in review.wishes] == [("вид на горы", "fail")]


async def test_duplicate_wishes_merge_with_the_highest_weight() -> None:
    spec = {**SPEC, "wishes": [{"text": "вид на горы", "weight": 1}, {"text": "Вид на горы.", "weight": 3},
                               {"text": "рядом со школой", "weight": 2}]}
    task = task_data(valencia(spec), review=True, soft_wishes=True)
    assert task["wishes"] == [{"id": 1, "text": "вид на горы", "weight": 3},
                              {"id": 2, "text": "рядом со школой", "weight": 2}]
    # the merged weight-3 wish unmet: near stays, even with near_reason "wish"
    review = await reviewer(True, []).review(task, FINDING)
    assert review.overall == "near"
    # the parser merges duplicates in task.wishes the same way (max weight)
    dup = {**task, "wishes": [{"id": 1, "text": "вид на горы", "weight": 1},
                              {"id": 2, "text": "вид на горы", "weight": 3}]}
    rows = [{"id": 1, "text": "вид на горы", "verdict": "fail", "quote": None}]
    assert (await reviewer(True, [], wish_rows=rows).review(dup, FINDING)).overall == "near"


async def test_a_row_whose_id_and_text_name_different_wishes_is_dropped() -> None:
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)  # 1: вид на горы, 2: рядом со школой
    rows = [{"id": 2, "text": "вид на горы", "verdict": "fail", "quote": None},
            {"id": 1, "text": "рядом со школой", "verdict": "pass", "quote": None}]
    review = await reviewer(True, [], wish_rows=rows).review(task, FINDING)
    assert review.wishes == () and review.overall == "near"  # swapped ids never make a match
    # a paraphrase (a text that is no known wish) still matches by id
    rows = [{"id": 1, "text": "горы видно", "verdict": "fail", "quote": None}]
    assert (await reviewer(True, [], wish_rows=rows).review(task, FINDING)).overall == "match"


@pytest.mark.parametrize("bad_id", ["²", "1²", "١", " ", "-1", "1.0", True])
async def test_an_id_that_is_not_a_plain_number_falls_back_to_the_text(bad_id) -> None:
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)
    rows = [{"id": bad_id, "text": "вид на горы", "verdict": "fail", "quote": None}]
    review = await reviewer(True, [], wish_rows=rows).review(task, FINDING)
    assert [(w.name, w.verdict) for w in review.wishes] == [("вид на горы", "fail")] and review.overall == "match"
    rows = [{"id": bad_id, "text": "горы видно", "verdict": "fail", "quote": None}]
    assert (await reviewer(True, [], wish_rows=rows).review(task, FINDING)).wishes == ()


async def test_near_reason_other_with_an_unmet_wish_and_all_hard_pass_stays_near() -> None:
    task = task_data(valencia(SPEC), review=True, soft_wishes=True)
    review = await reviewer(True, [], near_reason="other", deviation="промоция из нескольких квартир").review(
        task, FINDING)
    assert all(c.verdict == "pass" for c in review.criteria) and any(w.verdict == "fail" for w in review.wishes)
    assert review.overall == "near" and review.deviation_ru == "промоция из нескольких квартир"
    assert review_match(Match("exact"), review.to_dict()).bucket == "similar"
