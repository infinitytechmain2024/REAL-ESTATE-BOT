"""Q18: a townhouse is a house for the reviewer (REVIEW_TOWNHOUSE_IS_HOUSE) and the portal description is kept up
to the analysis limit (PORTAL_DESCRIPTION_FULL). Both off by default. Mocks only (``httpx.MockTransport``).
"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from bot.agents import reviewer as rv
from bot.campaign import final_report
from bot.campaign.relevance import ReviewerJudge
from bot.web_search.queries import QueryTask
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources import portals
from bot.web_search.sources.portals import (
    ApifyFotocasaSource,
    ApifyHabitacliaSource,
    ApifyIdealistaPortalSource,
)
from bot.web_search.worker import WebSearchConfig, WebSearchWorker
from tests.test_reviewer import settings

# sha256 of the prompts before Q18 (main at 0a8cc38): the flag off sends them byte for byte.
SYSTEM_SHA256 = "dcfe0a2bc6d951aaa6eaf74a16fb8f93ce3ac8436f7f9d2319f2c35a661b1a51"
SYSTEM_SOFT_SHA256 = "04a6f5ad8ef13b4288eeec83a846e773206f038da6557c4d0557952101ea8ce0"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- REVIEW_TOWNHOUSE_IS_HOUSE ---------------------------------------------------------------------------------------


def test_reviewer_flag_off_by_default_and_reaches_the_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_TOWNHOUSE_IS_HOUSE", raising=False)
    off = settings()
    assert off.review_townhouse_is_house is False
    judge = off.relevance_judge()
    assert isinstance(judge, ReviewerJudge) and judge.reviewer.townhouse_is_house is False
    on = settings(REVIEW_TOWNHOUSE_IS_HOUSE="true").relevance_judge()
    assert on.reviewer.townhouse_is_house is True


def test_flag_off_prompts_are_the_old_ones() -> None:
    assert sha(rv.SYSTEM) == SYSTEM_SHA256 and sha(rv.SYSTEM_SOFT) == SYSTEM_SOFT_SHA256
    assert rv.prompt_of() == ("reviewer-v3", rv.SYSTEM)
    assert rv.prompt_of(soft_wishes=True) == ("reviewer-v3-soft", rv.SYSTEM_SOFT)
    assert "townhouse for a house" in rv.SYSTEM_SOFT  # the old compromise example stays without the flag


def test_flag_on_prompts_carry_the_rule_under_their_own_version() -> None:
    plain, soft = rv.prompt_of(townhouse_is_house=True), rv.prompt_of(soft_wishes=True, townhouse_is_house=True)
    assert plain == ("reviewer-v3-th", rv.SYSTEM_TH) and soft == ("reviewer-v3-soft-th", rv.SYSTEM_SOFT_TH)
    assert len({rv.PROMPT_VERSION, rv.PROMPT_VERSION_SOFT, plain[0], soft[0]}) == 4
    for system in (rv.SYSTEM_TH, rv.SYSTEM_SOFT_TH):
        assert sha(system) not in (SYSTEM_SHA256, SYSTEM_SOFT_SHA256)
        assert "townhouse for a house" not in system  # no longer an example of a compromise
        rule = system.split("Different kind is a fail.\n", 1)[1].split("- budget", 1)[0]
        for word in ("casa adosada", "adosado", "pareado", "townhouse", '"pass" and no compromise'):
            assert word in rule
        for word in ("«отдельный»", "«independiente»", "detached house only", '"near"'):  # said: the old rule
            assert word in rule
    # Only the rule is added; the rest of each prompt is the old one.
    assert rv.SYSTEM_TH.replace(rv._TYPE_TH, rv._TYPE_V3) == rv.SYSTEM
    assert rv.SYSTEM_SOFT_TH.replace(rv._TYPE_TH, rv._TYPE_V3).replace(rv._KIND_SOFT_TH, rv._KIND_SOFT) == rv.SYSTEM_SOFT
    assert "when the task asks for a detached one" in rv.SYSTEM_SOFT_TH
    assert all(p in final_report._LEGAL_RULE_PROMPTS for p in (plain[0], soft[0]))  # the legal rule is kept


def _answer(criteria: list[str]) -> str:
    rows = [{"name": n, "verdict": "pass", "quote": "Casa adosada en Marbella", "note_ru": "ок"} for n in criteria]
    return json.dumps({"criteria": rows, "overall": "match", "deviation_ru": None, "confidence": 0.9,
                       "wishes": [], "near_reason": None})


@pytest.mark.parametrize(("soft", "on", "system", "version"), [
    (False, False, "SYSTEM", "reviewer-v3"),
    (True, False, "SYSTEM_SOFT", "reviewer-v3-soft"),
    (False, True, "SYSTEM_TH", "reviewer-v3-th"),
    (True, True, "SYSTEM_SOFT_TH", "reviewer-v3-soft-th"),
])
async def test_the_call_sends_the_selected_prompt_and_stores_its_version(soft, on, system, version) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        task = json.loads(body["messages"][1]["content"].split("\n", 1)[1])["task"]
        return httpx.Response(200, json={"choices": [{"message": {"content": _answer(task["criteria"])}}]})

    reviewer = rv.OpenRouterReviewer(api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                     soft_wishes=soft, townhouse_is_house=on)
    task = {"text": "дом в Марбелье, от 200 м², с бассейном", "criteria": ["place", "type"], "tolerance_pct": 10}
    review = await reviewer.review(task, {"summary": "Casa adosada en Marbella", "excerpt": "Casa adosada en Marbella"})
    await reviewer.aclose()
    assert seen[0]["messages"][0]["content"] == getattr(rv, system)
    assert seen[0]["response_format"]["json_schema"]["schema"] == (rv.SCHEMA_SOFT if soft else rv.SCHEMA)
    stored = review.to_dict()
    assert stored["prompt"] == version and review.overall == "match"
    assert ("wishes" in stored) is soft


# --- PORTAL_DESCRIPTION_FULL -------------------------------------------------------------------------------------------

HOUSE = QueryTask("casa", "casa", "Marbella", {}, "real_estate", constraints={"deal": "sale"}, country_code="ES")


def _description(pool_at: int, total: int) -> str:
    """A description with «piscina» at ``pool_at`` (pilot G3: 1693, 2036, 2097, 3408) and ``total`` characters."""
    text = ("Casa adosada luminosa con jardín. " * 200)[:pool_at] + "Piscina comunitaria. "
    return (text + "Cerca de la playa. " * 1000)[:total].strip()


def idealista_row(description: str) -> dict:
    return {"url": "https://www.idealista.com/inmueble/100000901/", "operation": "sale", "propertyType": "chalet",
            "country": "es", "municipality": "Marbella", "province": "Málaga", "address": "Casa adosada en Marbella",
            "price": 950000, "size": 240, "rooms": 4, "latitude": 36.51, "longitude": -4.88,
            "suggestedTexts": {"title": "Casa adosada en Marbella"}, "description": description}


def text_of(listing) -> str:
    return WebSearchWorker._source_text(SimpleNamespace(config=WebSearchConfig()), listing)


@pytest.mark.parametrize("pool_at", [1693, 2036, 2097, 3408])
def test_flag_off_cuts_at_1500_and_on_keeps_the_pool(pool_at: int) -> None:
    row = idealista_row(_description(pool_at, 4000))
    off = ApifyIdealistaPortalSource("secret-token")._normalize(row, HOUSE, None)
    assert off.description == row["description"][:1500] and "Piscina" not in text_of(off)
    on = ApifyIdealistaPortalSource("secret-token", description_full=True)._normalize(row, HOUSE, None)
    assert on.description == row["description"] and "Piscina" in text_of(on)


def test_flag_on_never_exceeds_the_analysis_post() -> None:
    assert portals._DESCRIPTION_CHARS == 1500
    assert portals._DESCRIPTION_CHARS_FULL == WebSearchConfig().max_post_chars == 8000
    row = idealista_row(_description(3408, 20_000))
    on = ApifyIdealistaPortalSource("secret-token", description_full=True)._normalize(row, HOUSE, None)
    assert len(on.description) == 8000
    assert len(text_of(on)) <= WebSearchConfig().max_post_chars  # the worker cuts it again after the facts block
    off = ApifyIdealistaPortalSource("secret-token")._normalize(row, HOUSE, None)
    assert text_of(on).split("\n", 1)[0] == text_of(off).split("\n", 1)[0]  # the facts line does not change


def test_portal_flag_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORTAL_DESCRIPTION_FULL", raising=False)
    assert WebSearchSettings(_env_file=None).portal_description_full is False
    for cls in (ApifyFotocasaSource, ApifyHabitacliaSource, ApifyIdealistaPortalSource):
        assert cls("secret-token").description_full is False


async def test_settings_pass_the_portal_flag_to_every_portal() -> None:
    settings_ = WebSearchSettings(
        _env_file=None, APIFY_TOKEN="secret-token", PORTAL_DESCRIPTION_FULL=True, APIFY_IDEALISTA_ENABLED=True,
        APIFY_IDEALISTA_PROVIDER="igolaizola", APIFY_FOTOCASA_ENABLED=True, APIFY_YAENCONTRE_ENABLED=True,
        APIFY_PISOS_ENABLED=True, APIFY_HABITACLIA_ENABLED=True, APIFY_MILANUNCIOS_ENABLED=True)
    sources = settings_.sources()
    try:
        assert len(sources) == 6 and all(source.description_full is True for source in sources)
    finally:
        for source in sources:
            await source.aclose()
