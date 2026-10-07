"""The Reviewer (PLAN 4.1): criteria matrix parsing, the combination rules, the judge adapter and the runner wiring."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from bot.agents.reviewer import (
    SCHEMA,
    Criterion,
    OpenRouterReviewer,
    Review,
    expected_criteria,
    hard_criteria,
    haystack_of,
    parse_review,
    tolerance_pct,
)
from bot.campaign import plan_campaign
from bot.campaign.models import Campaign
from bot.campaign.relevance import (
    Relevance,
    RelevanceError,
    ReviewerJudge,
    finding_data,
    reason_category,
    review_match,
    task_data,
)
from bot.campaign.settings import CampaignRunnerSettings
from bot.campaign.tolerance import Match
from tests.test_campaign_relevance import MATCH, FakeJudge, add, plot, setup

GOAL = "Купить квартиру в Валенсии до 200000 € от 2 комнат"


def valencia(spec: dict | None = None) -> Campaign:
    plan = plan_campaign(GOAL, vertical="real_estate", location="Valencia")
    return Campaign("c1", plan, "running", 1, 1, GOAL, None, None, datetime.now(UTC), spec=spec)


FINDING = {
    "summary": "Квартира в Руcафе, 3 комнаты, 190 000 €", "excerpt": "Piso en Ruzafa, Valencia. 3 habitaciones. 190.000 €",
    "location": "Ruzafa, Valencia", "price": 190_000, "currency": "EUR", "deal": "sale", "type": "apartment",
    "area_m2": 85, "rooms": 3, "evidence": {"price": "190.000 €", "rooms": "3 habitaciones"},
}


def answer(*rows: tuple, overall: str = "match", deviation: str | None = None, confidence: float = 0.9) -> str:
    return json.dumps({"criteria": [{"name": n, "verdict": v, "quote": q, "note_ru": f"заметка {n}"}
                                    for n, v, q in rows],
                       "overall": overall, "deviation_ru": deviation, "confidence": confidence}, ensure_ascii=False)


# --- the task's hard criteria --------------------------------------------------------------------------------------


def test_hard_criteria_come_from_the_plan_and_from_the_spec() -> None:
    hard = hard_criteria(valencia())
    assert hard["place"]["name"] == "Valencia" and hard["deal"] == "sale"
    assert hard["budget"] == {"max": 200_000.0, "currency": "EUR"} and hard["rooms"] == {"min": 2.0}
    assert "area" not in hard and "type" not in hard and tolerance_pct(valencia()) == 10
    assert expected_criteria(hard) == ["place", "deal", "budget", "rooms"]

    spec = {"place": {"name": "Valencia", "level": "province", "country": "ES", "districts": ["Ruzafa"]},
            "deal": "sale", "property_type": "apartment", "budget": {"min": 150_000, "max": 200_000, "currency": "EUR"},
            "rooms": {"min": 2, "max": 4}, "area_m2": {"min": 70}, "must_have": ["terraza", "ascensor"],
            "exclude": ["bajo"], "tolerance_pct": 5}
    campaign = valencia(spec)
    hard = hard_criteria(campaign)
    assert hard["place"] == {"name": "Valencia", "level": "province", "country": "ES", "districts": ["Ruzafa"]}
    assert hard["budget"] == {"min": 150_000.0, "max": 200_000.0, "currency": "EUR"} and hard["type"] == "apartment"
    assert hard["rooms"] == {"min": 2.0, "max": 4.0} and hard["area"] == {"min": 70.0, "unit": "m2"}
    assert expected_criteria(hard) == ["place", "deal", "type", "budget", "rooms", "area", "must_have:terraza",
                                       "must_have:ascensor", "exclude:bajo"]
    assert tolerance_pct(campaign) == 5
    assert task_data(campaign, review=True)["criteria"] == expected_criteria(hard)
    assert "criteria" not in task_data(campaign)  # the legacy judge sees what it always saw


# --- parsing -----------------------------------------------------------------------------------------------------------


def test_parse_keeps_grounded_quotes_and_never_lets_a_guess_fail_a_finding() -> None:
    hay = haystack_of(FINDING)
    expected = ["place", "deal", "budget", "rooms"]
    review = parse_review(answer(("place", "pass", "Ruzafa, Valencia"), ("deal", "pass", None),
                                 ("budget", "pass", "190.000 €"), ("rooms", "pass", "3 habitaciones")),
                          expected=expected, haystack=hay, model="m", tolerance=10)
    assert review.overall == "match" and [c.verdict for c in review.criteria] == ["pass"] * 4
    assert (review.confidence, review.model, review.tolerance_pct) == (0.9, "m", 10)
    assert review.to_dict()["criteria"][2] == {"name": "budget", "verdict": "pass", "quote": "190.000 €",
                                               "note_ru": "заметка budget"}

    # A fail with a real quote stays; the overall verdict follows the matrix.
    failed = parse_review(answer(("place", "fail", "piso en ruzafa,  VALENCIA"), ("deal", "pass", None),
                                 ("budget", "pass", None), ("rooms", "pass", None)),
                          expected=expected, haystack=hay)
    assert failed.criteria[0].verdict == "fail" and failed.overall == "reject"

    # No quote, or a quote that is not in the listing: unknown, never fail.
    guessed = parse_review(answer(("place", "fail", None), ("deal", "fail", "Alquiler de piso"),
                                  ("budget", "pass", None), ("rooms", "pass", None), overall="reject"),
                           expected=expected, haystack=hay)
    assert [c.verdict for c in guessed.criteria] == ["unknown", "unknown", "pass", "pass"]
    assert guessed.overall == "reject"  # the model's own rejection stands: the rules and the runner decide
    assert guessed.unknowns[0].note_ru and guessed.fails == ()


def test_parse_fixes_drift_and_fills_missing_criteria_with_unknown() -> None:
    expected = ["place", "budget", "must_have:terraza"]
    content = "```json\n" + answer(("Location", "OK", "Valencia"), ("Price", "unclear", None),
                                   ("extra", "pass", "x"), overall="match", deviation="Без террасы",
                                   confidence=7) + "\n```"
    review = parse_review(content, expected=expected, haystack="valencia")
    by = {c.name: c for c in review.criteria}
    assert [c.name for c in review.criteria] == expected  # unasked ones dropped, order kept
    assert by["place"].verdict == "pass" and by["budget"].verdict == "unknown"
    assert by["must_have:terraza"] == Criterion("must_have:terraza", "unknown", None, "Не проверено")
    assert review.overall == "near" and review.deviation_ru == "без террасы" and review.confidence == 1.0
    english = parse_review(answer(("place", "pass", None), overall="near", deviation="no terrace"), expected=["place"])
    assert english.deviation_ru is None
    with pytest.raises(ValueError):
        parse_review("[1]", expected=["place"])
    with pytest.raises(ValueError):
        parse_review(answer(("place", "pass", None), overall="maybe"), expected=["place"])
    assert set(SCHEMA["required"]) == set(SCHEMA["properties"])


# --- the combination rules -----------------------------------------------------------------------------------------------


def matrix(overall: str = "match", **verdicts: str) -> dict:
    return Review(tuple(Criterion(n.replace("__", ":"), v, "q" if v == "fail" else None, f"n {n}")
                        for n, v in verdicts.items()), overall).to_dict()  # type: ignore[arg-type]


def test_a_hard_fail_excludes_a_hard_unknown_holds_all_pass_stays_exact() -> None:
    exact = Match("exact")
    failed = review_match(exact, matrix("reject", place="fail", budget="pass"))
    assert (failed.bucket, failed.why, failed.note) == ("excluded", "location", "Не подходит: место")
    assert reason_category(failed.why) == "place"
    assert review_match(exact, matrix("reject", budget="fail", rooms="fail")).why == "price"
    assert review_match(exact, matrix("reject", must_have__terraza="fail")).why == "criteria"

    unknown = review_match(exact, matrix("near", place="pass", budget="unknown", must_have__terraza="unknown"))
    assert (unknown.bucket, unknown.why) == ("similar", "unverified")
    assert unknown.note == "Не подтверждено: бюджет, обязательно: terraza"

    passed = review_match(exact, matrix("match", place="pass", deal="pass"))
    assert passed == exact
    # The reviewer never lifts the rules' bucket, and a reject without a failed criterion (a catalog) excludes.
    assert review_match(Match("other", 0.4, "price"), matrix("match", place="pass")).bucket == "other"
    assert review_match(Match("similar", 0.2, "price"), matrix("near", place="pass", budget="unknown")).why == "price"
    assert review_match(exact, matrix("reject", place="pass")) == Match("excluded", float("inf"), "ai")
    assert review_match(exact, matrix("near", place="pass")).why == "ai"


def test_a_number_the_rules_already_hold_as_similar_keeps_its_question() -> None:
    similar = Match("similar", 0.15, "price")
    assert review_match(similar, matrix("reject", budget="fail")) is similar
    assert review_match(Match("similar", 0.2, "area", 1600), matrix("reject", area="fail")).bucket == "similar"
    # ... but not when something else failed, or the rules saw it far off.
    assert review_match(similar, matrix("reject", budget="fail", place="fail")).bucket == "excluded"
    assert review_match(Match("other", 0.4, "price"), matrix("reject", budget="fail")).bucket == "excluded"


# --- the model call over a fake HTTP ------------------------------------------------------------------------------------------


def chat(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def test_the_reviewer_sends_the_hard_criteria_and_the_listing_and_parses_the_matrix() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": "no structured outputs"})
        return chat(answer(("place", "pass", "Ruzafa, Valencia"), ("deal", "pass", None),
                           ("budget", "pass", "190.000 €"), ("rooms", "fail", "3 habitaciones")))

    reviewer = OpenRouterReviewer(api_key="k", model="anthropic/claude-opus-4.5",
                                  client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    campaign = valencia()
    payload = {"summary_ru": "Квартира в Руcафе", "evidence": {"price": "190.000 €", "rooms": "3 habitaciones"},
               "location": "Ruzafa, Valencia", "price_amount": 190_000, "price_currency": "EUR", "rooms": 3,
               "features": ["terraza"]}
    original = "Piso en Ruzafa, Valencia. 3 habitaciones. 190.000 €. " + "x" * 2000
    finding = finding_data(payload, original=original, review=True, vertical="real_estate")
    review = await reviewer.review(task_data(campaign, review=True), finding)

    assert [b["response_format"]["type"] for b in seen] == ["json_schema", "json_object"]
    assert seen[0]["model"] == "anthropic/claude-opus-4.5" and seen[0]["response_format"]["json_schema"]["strict"]
    assert "never guess" in seen[0]["messages"][0]["content"].lower() or "Never guess" in seen[0]["messages"][0]["content"]
    data = json.loads(seen[0]["messages"][1]["content"].split("\n", 1)[1])
    assert data["task"]["criteria"] == ["place", "deal", "budget", "rooms"] and data["task"]["tolerance_pct"] == 10
    assert data["task"]["hard"]["budget"] == {"max": 200000.0, "currency": "EUR"}
    assert data["finding"]["evidence"] == {"price": "190.000 €", "rooms": "3 habitaciones"}
    assert len(data["finding"]["excerpt"]) == 900 and data["finding"]["features"] == ["terraza"]
    # rooms "fail" with a quote that is in the listing stays a fail
    assert review.overall == "reject" and [c.verdict for c in review.criteria] == ["pass", "pass", "pass", "fail"]
    assert review.model == "anthropic/claude-opus-4.5" and review.tolerance_pct == 10

    failing = OpenRouterReviewer(api_key="k", client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503))))
    with pytest.raises(RelevanceError, match="http_503"):
        await failing.review({"criteria": ["place"]}, {})
    garbage = OpenRouterReviewer(api_key="k", client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: chat("not json"))))
    with pytest.raises(RelevanceError, match="invalid_response"):
        await garbage.review({"criteria": ["place"]}, {})
    with pytest.raises(ValueError):
        OpenRouterReviewer(api_key="")


async def test_the_judge_adapter_maps_overall_to_a_verdict_and_sends_investors_to_the_legacy_judge() -> None:
    class Fixed:
        model = "fake/reviewer"

        async def review(self, task, finding):
            return Review((Criterion("place", "unknown", None, "не сказано"),), "near", "цена не указана", 0.6,
                          self.model, 10)

    judge = ReviewerJudge(Fixed(), FakeJudge(MATCH))
    verdict = await judge.judge({"mode": "real_estate"}, {"summary": "x"})
    assert (verdict.verdict, verdict.deviation, verdict.model) == ("near", "цена не указана", "fake/reviewer")
    assert verdict.review["criteria"][0]["verdict"] == "unknown" and verdict.reason == "place: не подтверждено"
    assert await judge.judge({"mode": "investors"}, {}) == MATCH  # the legacy judge answers
    assert await judge.judge({"mode": "both"}, {"vertical": "investors"}) == MATCH
    alone = await ReviewerJudge(Fixed()).judge({"mode": "investors"}, {})
    assert alone.verdict is None and alone.review is None


# --- the runner: fail -> excluded, unknown -> similar with the reason, pass -> exact ---------------------------------------------


class ScriptedReviewer:
    """Answers per finding id (in the finding's summary): the verdict of every asked criterion."""

    model = "fake/reviewer"

    def __init__(self, script: dict[str, tuple[str, str]]) -> None:
        self.script, self.calls = script, []

    async def review(self, task, finding):
        self.calls.append((task, finding))
        key = next(k for k in self.script if k in finding["summary"])
        verdict, overall = self.script[key]
        criteria = tuple(Criterion(n, verdict if n == "place" else "pass", "Boadilla" if verdict == "fail" else None,
                                   f"заметка {n}") for n in task["criteria"])
        return Review(criteria, overall, None, 0.8, self.model, task["tolerance_pct"])  # type: ignore[arg-type]


async def test_runner_combines_the_matrix_stores_it_and_counts_reasons() -> None:
    reviewer = ScriptedReviewer({"PASS": ("pass", "match"), "FAIL": ("fail", "reject"), "UNKNOWN": ("unknown", "near")})
    _, store, messenger, runner, cid = await setup(ReviewerJudge(reviewer), relevance_fail_closed=True)
    for fid, word in (("ok", "PASS"), ("bad", "FAIL"), ("maybe", "UNKNOWN")):
        add(store, cid, fid, {**plot(2500), "summary_ru": f"Участок {word} под застройку"})
    await runner.tick()
    assert {f: b for f, (b, _) in store.buckets.items()} == {"ok": "exact", "bad": "excluded", "maybe": "similar"}
    assert len(messenger.findings()) == 1
    assert store.whys == {"bad": "place", "maybe": "unverified"}
    assert store.hold_reasons["maybe"] == "Не подтверждено: место"
    # the reviewer was asked with the task's hard criteria, the matrix is stored once per finding
    task, finding = reviewer.calls[0]
    assert task["criteria"][0] == "place" and task["tolerance_pct"] == 10 and finding["vertical"] == "real_estate"
    stored = await store.relevance(cid, "bad")
    assert stored.verdict == "reject" and stored.review["criteria"][0] == {
        "name": "place", "verdict": "fail", "quote": "Boadilla", "note_ru": "заметка place"}
    assert len(reviewer.calls) == 3
    held = await store.held_findings(cid, "similar", 5)
    assert [f.id for f in held] == ["maybe"]
    deviation = await runner._deviation(await runner.campaigns.get(cid), (await _request(runner, cid)), held[0])
    assert deviation.kind == "unverified"  # a neutral question, nothing technical for the user


async def _request(runner, cid):
    from bot.campaign.runner import campaign_request

    return campaign_request(await runner.campaigns.get(cid))


async def test_a_reviewer_that_fails_is_handled_like_a_failed_judge() -> None:
    class Down:
        model = "fake/reviewer"

        async def review(self, task, finding):
            raise RelevanceError("timeout")

    _, store, messenger, runner, cid = await setup(ReviewerJudge(Down()), relevance_fail_closed=True)
    add(store, cid, "a", plot(2500))
    await runner.tick()
    assert "a" not in store.buckets and messenger.findings() == [] and "a" in runner._relevance_misses  # retried later
    campaign = await runner.campaigns.get(cid)
    assert await runner._stream(campaign, final=True) == 0  # ending: held as unverified, never lost
    assert store.buckets["a"][0] == "similar" and store.whys["a"] == "unverified"


# --- CAMPAIGN_JUDGE ---------------------------------------------------------------------------------------------------------------


def settings(**env: str) -> CampaignRunnerSettings:
    base = {"DATABASE_URL": "postgresql://x", "TELEGRAM_TOKEN": "t", "OPENROUTER_API_KEY": "k"}
    return CampaignRunnerSettings(_env_file=None, **{**base, **env})


def test_campaign_judge_selects_the_reviewer_or_the_legacy_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.campaign.relevance import OpenRouterRelevanceJudge

    for name in ("CAMPAIGN_JUDGE", "OPENROUTER_REVIEW_MODEL", "OPENROUTER_FINAL_MODEL"):
        monkeypatch.delenv(name, raising=False)
    default = settings()
    assert default.judge == "reviewer" and default.review_model == "anthropic/claude-sonnet-4.5"
    assert default.final_model == "anthropic/claude-sonnet-4.5" and default.final_report_enabled
    judge = default.relevance_judge()
    assert isinstance(judge, ReviewerJudge) and judge.model == "anthropic/claude-sonnet-4.5"
    assert isinstance(judge.fallback, OpenRouterRelevanceJudge)
    monkeypatch.setenv("CAMPAIGN_JUDGE", "legacy")
    monkeypatch.setenv("OPENROUTER_REVIEW_MODEL", "anthropic/claude-opus-4.5")
    legacy = settings().relevance_judge()
    assert isinstance(legacy, OpenRouterRelevanceJudge) and legacy.model == "openai/gpt-4o-mini"
    assert not getattr(legacy, "reviews", False)  # the runner hands it the same data as before
    monkeypatch.setenv("CAMPAIGN_JUDGE", "reviewer")
    assert settings().relevance_judge().model == "anthropic/claude-opus-4.5"
    assert settings(OPENROUTER_API_KEY="").relevance_judge() is None
    assert settings(CAMPAIGN_RELEVANCE_MAX_CALLS=0).relevance_judge() is None
    with pytest.raises(ValueError):
        settings(CAMPAIGN_JUDGE="oracle")


async def test_the_legacy_judge_keeps_the_old_path_and_data() -> None:
    judge = FakeJudge(Relevance("near", "Метро дальше.", "дальше от метро"))
    _, store, messenger, runner, cid = await setup(judge)
    add(store, cid, "far", plot(2500, evidence={"area": "2500 m2"}))
    await runner.tick()
    task, finding = judge.calls[0]
    assert "criteria" not in task and "hard" not in task and "evidence" not in finding and "vertical" not in finding
    assert store.buckets["far"][0] == "similar" and store.whys["far"] == "ai" and messenger.findings() == []
    assert (await store.relevance(cid, "far")).review is None


# --- review fixes: grounding, the near band, the cost cap ---------------------------------------------------------------------


@pytest.mark.parametrize("quote", ["...", "«»", "3", "в", "  ..  ", "€ 1"])
def test_a_fail_quote_that_is_too_short_or_empty_of_content_is_unknown(quote: str) -> None:
    finding = {"summary": "Piso 3 en venta, в центре ... «»", "excerpt": "precio 250.000 € 3 в", "evidence": {}}
    review = parse_review(answer(("budget", "fail", quote)), expected=["budget"], haystack=haystack_of(finding))
    assert review.criteria[0].verdict == "unknown" and review.fails == ()


def test_a_real_quote_with_letters_or_digit_runs_still_fails_and_payload_fields_do_not_ground_a_quote() -> None:
    finding = {"summary": "Piso en venta", "excerpt": "Precio 250.000 € negociable", "evidence": {"price": "250.000 €"},
               "location": "Ruzafa, Valencia", "price": 250_000, "rooms": 3}
    hay = haystack_of(finding)
    ok = parse_review(answer(("budget", "fail", "precio 250.000 €")), expected=["budget"], haystack=hay)
    assert ok.criteria[0].verdict == "fail" and ok.overall == "reject"
    digits = parse_review(answer(("budget", "fail", "250.000")), expected=["budget"], haystack=hay)
    assert digits.criteria[0].verdict == "fail"
    # a quote found only in the extracted payload fields (location, price) is not listing text
    payload_only = parse_review(answer(("place", "fail", "Ruzafa, Valencia")), expected=["place"], haystack=hay)
    assert payload_only.criteria[0].verdict == "unknown"
    # finding_data keeps the facts for the model but the grounding text is only summary + excerpt + evidence
    data = finding_data({"summary_ru": "Квартира", "location": "Zzyzx", "evidence": {"price": "190 000 €"}},
                        original="Piso barato", review=True)
    assert data["location"] == "Zzyzx" and "zzyzx" not in haystack_of(data)
    assert "190 000 €" in haystack_of(data) and "piso barato" in haystack_of(data)


def test_the_near_band_is_kept_only_for_the_one_criterion_the_rules_measured() -> None:
    price = Match("similar", 0.15, "price")
    area = Match("similar", 0.2, "area", 1600)
    assert review_match(price, matrix("reject", budget="fail")) is price
    assert review_match(area, matrix("reject", area="fail")) is area
    # price band, but the area failed (or the rooms): not the measured number -> excluded
    assert review_match(price, matrix("reject", area="fail")).bucket == "excluded"
    assert review_match(price, matrix("reject", rooms="fail")).bucket == "excluded"
    assert review_match(area, matrix("reject", budget="fail")).bucket == "excluded"
    assert review_match(area, matrix("reject", area="fail", rooms="fail")).bucket == "excluded"
    # a band whose measure is neither price nor area never keeps a fail
    assert review_match(Match("similar", 0.2, "unverified"), matrix("reject", budget="fail")).bucket == "excluded"


async def test_the_reviewer_judge_counts_attempts_including_failed_ones_and_stops_at_the_cap() -> None:
    class Flaky:
        model = "fake/reviewer"

        def __init__(self) -> None:
            self.calls, self.fail = 0, False

        async def review(self, task, finding):
            self.calls += 1
            assert "campaign_id" not in task, "bookkeeping is not sent to the model"
            if self.fail:
                raise RelevanceError("http_error")
            return Review((Criterion("place", "pass", None, "ок"),), "match", None, 0.9, self.model, 10)

    reviewer = Flaky()
    judge = ReviewerJudge(reviewer, max_calls=3)
    task = {"mode": "real_estate", "campaign_id": "c1"}
    assert not judge.calls_exhausted("c1")
    assert (await judge.judge(task, {"summary": "a"})).verdict == "match"
    reviewer.fail = True
    for _ in range(2):
        with pytest.raises(RelevanceError):
            await judge.judge(task, {"summary": "a"})  # failed attempts count too
    assert judge.calls_exhausted("c1") and not judge.calls_exhausted("c2")
    reviewer.fail = False
    capped = await judge.judge(task, {"summary": "a"})
    assert capped.verdict is None and capped.review is None and reviewer.calls == 3, "no further call is made"
    assert (await judge.judge({"mode": "real_estate", "campaign_id": "c2"}, {"summary": "a"})).verdict == "match"
    assert task_data(valencia(), review=True)["campaign_id"] == "c1"


def test_the_review_call_cap_is_a_setting_with_a_default_of_300(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAMPAIGN_REVIEW_MAX_CALLS", raising=False)
    assert settings().review_max_calls == 300 and settings().relevance_judge().max_calls == 300
    assert settings(CAMPAIGN_REVIEW_MAX_CALLS="7").relevance_judge().max_calls == 7
