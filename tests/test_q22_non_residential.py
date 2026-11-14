"""Q22: a non-residential unit is not a home for the reviewer (REVIEW_NON_RESIDENTIAL, off by default) and the task
card says the budget tolerance is only upward («бюджет до +10 %», no flag). Mocks only (``httpx.MockTransport``).
"""

from __future__ import annotations

import hashlib
import itertools
import json

import httpx
import pytest

from bot.agents import reviewer as rv
from bot.campaign import final_report
from bot.campaign.relevance import ReviewerJudge
from bot.campaign.spec import Deviations, TaskSpec
from tests.test_reviewer import settings

# sha256 of every prompt before Q22 (main at bc94718), by (soft_wishes, townhouse_is_house, quote_hits): the flag off
# sends them byte for byte.
OLD_SHA256 = {
    (False, False, False): "dcfe0a2bc6d951aaa6eaf74a16fb8f93ce3ac8436f7f9d2319f2c35a661b1a51",
    (False, False, True): "c3a7097a7331dab40f88820ea609369ee4f5619c7f6e5dc4f21824fe2e98c109",
    (False, True, False): "91cd5248734674d6be3b939dbc80ce29c29c3d68756a0ae9314def21676adcc3",
    (False, True, True): "17670bc33d7bc6fc9df0b9dd5c38e55a143a170d8c1215369b38625a7279888a",
    (True, False, False): "04a6f5ad8ef13b4288eeec83a846e773206f038da6557c4d0557952101ea8ce0",
    (True, False, True): "1f6714f47077f65727858ea9dea82e9627af491c1b10723e441a5d7a3987f175",
    (True, True, False): "cba94ff36a6e34bbc461eedfc615dd6d43112a85c0caa751cd0961121dafd614",
    (True, True, True): "9a23f079702bdae47433f445c9496f942bf406c17b0ec15823ac24756cc4dc6c",
}
FLAGS = list(itertools.product((False, True), repeat=3))
STUDIO = "Estudio en Valencia, 900 € al mes, IVA incluido, ya que se trata de un estudio"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _prompt(flags: tuple[bool, bool, bool], nr: bool) -> tuple[str, str]:
    soft, th, hits = flags
    return rv.prompt_of(soft_wishes=soft, townhouse_is_house=th, quote_hits=hits, non_residential=nr)


# --- REVIEW_NON_RESIDENTIAL --------------------------------------------------------------------------------------------


def test_flag_off_by_default_and_reaches_the_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_NON_RESIDENTIAL", raising=False)
    off = settings()
    assert off.review_non_residential is False
    judge = off.relevance_judge()
    assert isinstance(judge, ReviewerJudge) and judge.reviewer.non_residential is False
    assert settings(REVIEW_NON_RESIDENTIAL="true").relevance_judge().reviewer.non_residential is True


@pytest.mark.parametrize("flags", FLAGS)
def test_flag_off_prompts_are_byte_for_byte_the_old_ones(flags) -> None:
    version, system = _prompt(flags, nr=False)
    assert sha(system) == OLD_SHA256[flags]
    assert "-nr" not in version and "uso profesional" not in system
    assert (version, system) == rv.prompt_of(soft_wishes=flags[0], townhouse_is_house=flags[1], quote_hits=flags[2])


@pytest.mark.parametrize("flags", FLAGS)
def test_flag_on_prompt_has_its_own_version_and_the_rule(flags) -> None:
    old_version, old_system = _prompt(flags, nr=False)
    version, system = _prompt(flags, nr=True)
    soft, th, hits = flags
    assert version == f"reviewer-v3{'-soft' if soft else ''}{'-th' if th else ''}-nr{'-hits' if hits else ''}"
    assert version.replace("-nr", "") == old_version
    assert sha(system) not in OLD_SHA256.values()
    assert system.replace(rv._NON_RESIDENTIAL, "") == old_system  # only the rule is added
    assert version in final_report._LEGAL_RULE_PROMPTS  # the legal-buildability rule is still in it
    assert rv.is_soft(version) is soft
    rule = system.split("- Not a home:", 1)[1].split("- budget, rooms, area:", 1)[0]
    for word in ("«IVA incluido, ya\n    que se trata de un estudio»", "«uso profesional»",
                 "«sin cédula de habitabilidad»", "«no apto para vivienda»", "«local reconvertido»",
                 "«oficina reconvertida»", '"type" is "fail"', '"type" is "unknown"', "residential studio flat"):
        assert word in rule, word
    assert rule.index('"fail"') < rule.index("indirect sign") < rule.index('"unknown"')


def test_flag_on_rule_keeps_ordinary_homes_out() -> None:
    rule = rv._NON_RESIDENTIAL
    # 1: VAT counts in a rent only on the monthly rent itself, not on the agency fee, a garage, utilities, services
    rent = rule.split("in a RENT only:", 1)[1].split("* In a SALE", 1)[0]
    fail, unknown = rent.split("VAT on the rent alone", 1)
    unknown, no_sign = unknown.split("Not a\n    sign at all:", 1)
    # 7: «fail» only when the VAT is explained by a non-residential use (G5); a bare VAT on the rent is «unknown»
    assert "explains by a non-residential use" in fail and "«IVA incluido, ya\n    que se trata de un estudio»" in fail
    assert "«+ IVA por ser local»" in fail and "«alquiler 900 € + IVA»" not in fail and "más IVA»" not in fail
    for word in ("«alquiler 900 € + IVA»", "«900 €/mes más IVA»", 'indirect sign: "unknown"'):
        assert word in unknown, word
    # 8: a company rent for its staff is no sign
    for word in ("agency fee", "«honorarios + IVA»", "garage", "storage room", "utilities", "services",
                 "a rent to a company for its staff", "«alquiler a empresa para empleados\n    + IVA»"):
        assert word in no_sign, word
    # 2: a sale, obra nueva / promoción: VAT is normal
    sale = rule.split("* In a SALE", 1)[1].split("\n  *", 1)[0]
    for word in ("obra nueva / promoción", "«+ IVA»", "«IVA incluido»", "never a sign"):
        assert word in sale, word
    # 3: professional use or a conversion only when the listing says it is not housing
    use = rule.split("* «uso profesional»", 1)[1].split("\n  * A tourist", 1)[0]
    for word in ("only when the listing says", "plainly that it is not housing", "Housing, no sign:",
                 "«vivienda o uso profesional»", "«reconvertido en vivienda con cédula»"):
        assert word in use, word
    # 4: a tourist flat is housing
    tourist = rule.split("* A tourist flat is housing, no sign:", 1)[1].split("\n  \"A studio", 1)[0]
    for word in ("VUT", "«uso turístico»", "«licencia turística»", "«licencia turística en trámite»"):  # 4, 6
        assert word in tourist, word
    assert "trámite" not in rule.split("An indirect sign only", 1)[1]  # 6: a licence in progress is no sign


def test_all_sixteen_versions_are_distinct_and_trusted_by_the_report() -> None:
    versions = {_prompt(flags, nr)[0] for flags in FLAGS for nr in (False, True)}
    assert len(versions) == 16 and versions <= set(final_report._LEGAL_RULE_PROMPTS)  # Q45 adds the -tn ones


def _answer(criteria: list[str], *, type_verdict: str, quote: str) -> str:
    rows = [{"name": n, "verdict": type_verdict if n == "type" else "pass", "quote": quote, "note_ru": "ок"}
            for n in criteria]
    return json.dumps({"criteria": rows, "overall": "reject" if type_verdict == "fail" else "near",
                       "deviation_ru": None if type_verdict == "fail" else "не жильё?", "confidence": 0.9})


@pytest.mark.parametrize(("on", "type_verdict", "quote", "overall", "verdict"), [
    (True, "fail", "IVA incluido, ya que se trata de un estudio", "reject", "fail"),
    (True, "unknown", "Estudio en Valencia", "near", "unknown"),
    (True, "fail", "no apto para vivienda", "reject", "unknown"),  # a quote not in the listing: no «type» fail
    (False, "fail", "IVA incluido, ya que se trata de un estudio", "reject", "fail"),
])
async def test_the_call_sends_the_selected_prompt_and_a_quoted_fail_rejects(on, type_verdict, quote, overall,
                                                                             verdict) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        task = json.loads(body["messages"][1]["content"].split("\n", 1)[1])["task"]
        content = _answer(task["criteria"], type_verdict=type_verdict, quote=quote)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    reviewer = rv.OpenRouterReviewer(api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                     non_residential=on)
    task = {"text": "квартира в аренду в Валенсии до 1000 €", "criteria": ["place", "type"], "tolerance_pct": 10}
    review = await reviewer.review(task, {"summary": STUDIO, "excerpt": STUDIO})
    await reviewer.aclose()
    assert seen[0]["messages"][0]["content"] == rv.prompt_of(non_residential=on)[1]
    assert review.to_dict()["prompt"] == ("reviewer-v3-nr" if on else "reviewer-v3")
    assert review.overall == overall
    assert {c.name: c.verdict for c in review.criteria}["type"] == verdict


# --- the budget tolerance on the task card (no flag) -------------------------------------------------------------------


def test_budget_tolerance_is_only_upward_on_the_card() -> None:
    assert Deviations(budget_pct=10, area_pct=10).line_ru() == "бюджет до +10 %; площадь −10 %"
    assert "±" not in Deviations(budget_pct=20).line_ru()
    spec = TaskSpec.model_validate({"mode": "real_estate", "deal": "rent", "property_type": "apartment",
                                    "place": {"name": "Valencia", "level": "city", "country": "ES"},
                                    "budget": {"max": 1200, "currency": "EUR"},
                                    "deviations": {"budget_pct": 10, "asked": True}})
    assert "Допустимые отступления: бюджет до +10 %" in spec.summary_ru()
    assert "±" not in spec.summary_ru()
