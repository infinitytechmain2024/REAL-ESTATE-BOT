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
from bot.analysis_pipeline import source_place
from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer
from bot.analysis_pipeline.settings import AnalysisSettings
from bot.campaign import final_report, plan_campaign
from bot.campaign.quote_hits import HITS_PER_TERM, MAX_HITS, WINDOW, needles, quote_hits
from bot.campaign.relevance import MAX_REVIEW_TEXT_CHARS, ReviewerJudge, finding_data
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.store import MemoryCampaignStore
from bot.web_search.queries import QueryTask
from bot.web_search.settings import WebSearchSettings
from bot.web_search.sources import portals
from bot.web_search.sources.portals import (
    ApifyFotocasaSource,
    ApifyHabitacliaSource,
    ApifyIdealistaPortalSource,
)
from bot.web_search.worker import WebSearchConfig, WebSearchWorker
from tests.test_campaign_relevance import CHAT, USER
from tests.test_campaign_runner import ButtonMessenger
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
        detached, _, counter = rule.partition("Not a detached-only request:")
        for word in ("«отдельный дом»", "«casa independiente»", "detached house only",  # said: type pass, near
                     'still "pass" on "type"', '"overall" is "near"', "deviation_ru «таунхаус»"):
            assert word in detached
        for word in ("«можно таунхаус»", "«не обязательно отдельный»", "«cocina independiente»",
                     "«entrada independiente»", '"pass",\n  no compromise'):  # counterexamples: a plain pass
            assert word in counter
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


# --- PORTAL_DESCRIPTION_FULL: «hits» for the reviewer ------------------------------------------------------------------

POOL_TEXT = ("Casa adosada luminosa con jardín y vistas. " * 60)[:2000] + "Piscina privada climatizada de 8 metros. " + (
    "Cerca de la playa y del golf. " * 40)


def test_quote_hits_find_spanish_words_for_russian_terms() -> None:
    assert POOL_TEXT.index("Piscina") == 2000
    hits = quote_hits(POOL_TEXT, ["бассейн"], skip_before=MAX_REVIEW_TEXT_CHARS)
    assert len(hits) == 1 and "Piscina privada climatizada" in hits[0] and len(hits[0]) <= 2 * WINDOW + 7
    assert hits[0] in " ".join(POOL_TEXT.split())  # verbatim, whitespace collapsed like the excerpt
    for term, word in (("лифт", "ascensor"), ("терраса", "terraza"), ("гараж", "garaje"), ("парковка", "parking"),
                       ("сад", "jardin"), ("кондиционер", "aire acondicionado"), ("мебель", "amueblado"),
                       ("можно с животными", "mascota")):
        assert word in needles(term), term
    assert quote_hits("Ático amueblado con ascensor y TERRAZA.", ["меблированная", "лифт", "терраса"]) == [
        "Ático amueblado con ascensor y TERRAZA."]  # accents and case ignored, overlapping windows merged
    assert quote_hits(POOL_TEXT, ["бассейн"], skip_before=len(POOL_TEXT)) == []  # already in the excerpt
    assert quote_hits("Cerca de la plaza mayor.", ["парковка"]) == []  # «plaza» alone is not parking
    assert quote_hits("Casa con jardín.", ["first line"]) == []


def test_finding_data_carries_hits_only_with_terms() -> None:
    off = finding_data({"summary_ru": "Дом"}, original=POOL_TEXT, review=True)
    assert "hits" not in off and "Piscina" not in rv.haystack_of(off)
    on = finding_data({"summary_ru": "Дом"}, original=POOL_TEXT, review=True, hit_terms=["бассейн"])
    assert on["hits"] and "piscina privada climatizada" in rv.haystack_of(on)
    assert {k: v for k, v in on.items() if k != "hits"} == off  # nothing else changes


def test_hits_prompt_has_its_own_version_and_allows_quoting_hits() -> None:
    for soft in (False, True):
        for th in (False, True):
            base_version, base = rv.prompt_of(soft_wishes=soft, townhouse_is_house=th)
            version, system = rv.prompt_of(soft_wishes=soft, townhouse_is_house=th, quote_hits=True)
            assert version == base_version + "-hits" and version in final_report._LEGAL_RULE_PROMPTS
            assert '"evidence" and "hits" values' in system and '"hits"' not in base
            assert system.replace(rv._QUOTABLE_HITS, rv._QUOTABLE_V3) == base
    assert sha(rv.prompt_of()[1]) == SYSTEM_SHA256


def test_portal_flag_reaches_the_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORTAL_DESCRIPTION_FULL", raising=False)
    assert settings().relevance_judge().quote_hits is False
    judge = settings(PORTAL_DESCRIPTION_FULL="true").relevance_judge()
    assert judge.quote_hits is True and judge.reviewer.quote_hits is True


HOUSE_SPEC = {"deal": "sale", "property_type": "house", "must_have": ["бассейн"],
              "place": {"name": "Madrid", "level": "city", "country": "ES"}, "delivery": {"show_similar": False}}


async def _run(quote_hits_on: bool) -> tuple[MemoryRunStore, list[dict]]:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        data = json.loads(body["messages"][1]["content"].split("\n", 1)[1])
        hits = data["finding"].get("hits") or []
        rows = []
        for name in data["task"]["criteria"]:
            if name == "must_have:бассейн":  # the model quotes the hit verbatim, or does not know
                quote = "Piscina privada climatizada" if hits else None
                rows.append({"name": name, "verdict": "pass" if hits else "unknown", "quote": quote, "note_ru": "ок"})
            else:
                rows.append({"name": name, "verdict": "pass", "quote": "Casa adosada", "note_ru": "ок"})
        answer = {"criteria": rows, "overall": "match" if hits else "near",
                  "deviation_ru": None if hits else "бассейн не указан", "confidence": 0.9}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    reviewer = rv.OpenRouterReviewer(api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                     quote_hits=quote_hits_on)
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    runner = CampaignRunner(campaigns, store, ButtonMessenger(), None,
                            config=RunnerConfig(window_cooldown_seconds=0, relevance_fail_closed=True),
                            relevance=ReviewerJudge(reviewer))
    plan = plan_campaign(HOUSE_TASK, vertical="real_estate", location="Madrid")
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=HOUSE_TASK, actor="telegram:42",
                                 spec=HOUSE_SPEC)
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    payload = {"schema_version": "analysis-v4", "summary_ru": "Таунхаус в Мадриде", "location": "Madrid",
               "country": "ES", "price_amount": 900_000, "price_currency": "EUR", "deal_type": "sale",
               "property_type": "house", "area_m2": 240, "listing_kind": "offer",
               "original_post_link": "https://www.idealista.com/inmueble/12399/"}
    store.add_finding(cid, "house", "🏠 house", payload=payload, original=POOL_TEXT, vertical="real_estate")
    await runner.tick()
    return store, seen


HOUSE_TASK = "Купить дом в Мадриде с бассейном"


async def test_runner_sends_hits_and_the_pool_at_2000_passes_with_a_verbatim_quote() -> None:
    store, seen = await _run(False)
    finding = json.loads(seen[0]["messages"][1]["content"].split("\n", 1)[1])["finding"]
    assert "hits" not in finding and seen[0]["messages"][0]["content"] == rv.SYSTEM
    assert store.buckets["house"][0] == "similar"  # the pool is past the 900 characters: not confirmed

    store, seen = await _run(True)
    finding = json.loads(seen[0]["messages"][1]["content"].split("\n", 1)[1])["finding"]
    assert any("Piscina privada climatizada" in hit for hit in finding["hits"])
    assert seen[0]["messages"][0]["content"] == rv.prompt_of(quote_hits=True)[1]
    assert store.buckets["house"][0] == "exact"
    review = rv.parse_review(json.dumps({"criteria": [{"name": "must_have:бассейн", "verdict": "fail",
                                                       "quote": "Piscina privada climatizada", "note_ru": "x"}],
                                         "overall": "reject", "deviation_ru": None, "confidence": 1}),
                             expected=["must_have:бассейн"], haystack=rv.haystack_of(finding), quote_hits=True)
    assert review.criteria[0].verdict == "fail" and review.prompt == "reviewer-v3-hits"  # the hit grounds a quote


# --- PORTAL_DESCRIPTION_FULL: the place check reads 1500 characters --------------------------------------------------


def _post(description: str) -> str:
    facts = {"url": "https://www.idealista.com/inmueble/100000902/", "title": "Casa adosada",
             "address": "Nueva Andalucía, Marbella", "property_type": "house", "deal": "sale"}
    return f"JSON-LD: {json.dumps(facts, ensure_ascii=False)}\nCasa adosada\nСсылка: {facts['url']}\n{description}"


AGENCY_TAIL = ("Casa adosada con piscina. " * 80)[:3000] + " Nuestra oficina central situada en Madrid le atenderá."


def test_place_check_reads_only_the_old_1500_characters_of_a_long_description() -> None:
    post = _post(AGENCY_TAIL)
    whole = source_place.conflict(post)
    assert whole is not None and whole.strong  # the false conflict the review found
    assert source_place.conflict(post, description_chars=source_place.PLACE_DESCRIPTION_CHARS) is None
    near = _post("Chalet situado en Madrid, junto al Retiro. " + AGENCY_TAIL)  # a real conflict early stays
    found = source_place.conflict(near, description_chars=source_place.PLACE_DESCRIPTION_CHARS)
    assert found is not None and found == source_place.conflict(near)


def test_analysis_settings_pass_the_place_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PORTAL_DESCRIPTION_FULL", raising=False)
    assert AnalysisSettings(_env_file=None, DATABASE_URL="postgresql://x").portal_description_full is False
    assert OpenRouterAnalyzer("k", "m").place_description_chars is None


@pytest.mark.parametrize(("flag", "chars"), [(False, None), (True, 1500)])
async def test_the_analyzer_hands_the_limit_to_the_place_check(monkeypatch: pytest.MonkeyPatch, flag, chars) -> None:
    from bot.analysis_pipeline import main
    from bot.analysis_pipeline.models import Evidence

    built: list[OpenRouterAnalyzer] = []

    class Stop(Exception):
        pass

    def fake_analyzer(*args, **kwargs):
        built.append(OpenRouterAnalyzer(*args, **kwargs))
        raise Stop

    monkeypatch.setattr(main, "OpenRouterAnalyzer", fake_analyzer)
    s = AnalysisSettings(_env_file=None, DATABASE_URL="postgresql://x", OPENROUTER_API_KEY="k",
                         ANALYSIS_LOCATION_FROM_TEXT=True, PORTAL_DESCRIPTION_FULL=flag)
    with pytest.raises(Stop):
        await main.run_once(store=SimpleNamespace(), settings=s)  # the analyzer is built first: no store call
    analyzer = built[0]
    assert analyzer.place_description_chars == chars
    seen: dict = {}

    def conflict(text, title="", **kwargs):
        seen.update(kwargs)
        raise Stop

    monkeypatch.setattr(source_place, "conflict", conflict)
    with pytest.raises(Stop):
        await analyzer.analyze(Evidence(post_id="p", source_id="s", canonical_url="https://x.es/1",
                                        text=_post(AGENCY_TAIL)), "real_estate")
    assert seen == {"description_chars": chars}


# --- hits per term, word ends, more synonyms, the «sin» line, the agency sentence -----------------------------------


def test_hits_are_shared_round_robin_between_terms() -> None:
    gardens = " ".join(f"Jardín número {n} " + "x " * 150 for n in range(6))  # six windows of «сад» apart
    text = gardens + "Al final, una piscina privada."
    hits = quote_hits(text, ["сад", "бассейн"])
    assert any("piscina privada" in hit for hit in hits)
    assert sum("Jardín número" in hit for hit in hits) == HITS_PER_TERM
    many = " ".join(f"{word} " + "x " * 150 for word in ("jardín", "piscina", "terraza", "ascensor", "garaje")
                    for _ in range(3))
    assert len(quote_hits(many, ["сад", "бассейн", "терраса", "лифт", "гараж"])) == MAX_HITS == 8


def test_latin_words_match_to_the_word_end_cyrillic_from_the_start() -> None:
    assert quote_hits("Tratamiento de lifting facial incluido.", ["лифт"]) == []
    assert quote_hits("Edificio con dos lifts.", ["лифт"]) == ["Edificio con dos lifts."]
    assert quote_hits("Dos piscinas comunitarias.", ["бассейн"]) == ["Dos piscinas comunitarias."]
    assert quote_hits("Дом с бассейном и садом.", ["бассейн"]) == ["Дом с бассейном и садом."]


@pytest.mark.parametrize(("term", "word"), [
    ("балкон", "balcon"), ("вид на море", "vistas al mar"), ("пляж", "playa"), ("первая линия", "primera linea"),
    ("камин", "chimenea"), ("кладовка", "trastero"), ("подвал", "sotano"), ("отопление", "calefaccion"),
    ("охрана", "vigilancia"), ("консьерж", "conserje"), ("барбекю", "barbacoa"), ("солярий", "solarium"),
])
def test_more_synonyms(term: str, word: str) -> None:
    assert word in needles(term)


def test_a_hit_with_sin_is_found_and_the_prompt_says_it_does_not_meet_the_must_have() -> None:
    hits = quote_hits("Chalet adosado, sin piscina, con jardín pequeño.", ["бассейн"])
    assert hits == ["Chalet adosado, sin piscina, con jardín pequeño."]
    system = rv.prompt_of(quote_hits=True)[1]
    assert "a hit only shows where the word occurs" in system
    assert "«sin piscina» or «piscina municipal» do not" in system


def test_the_agency_sentence_only_with_both_flags() -> None:
    from bot.analysis_pipeline import openrouter as orr
    from bot.analysis_pipeline.pipeline import metadata

    assert orr.instructions() == orr.INSTRUCTIONS and orr.instructions(False, True) == orr.INSTRUCTIONS
    assert orr.instructions(True) == orr.INSTRUCTIONS_LOCATION_FROM_TEXT
    agency = orr.instructions(True, True)
    assert orr.AGENCY_OFFICE_SENTENCE in agency and orr.AGENCY_OFFICE_SENTENCE not in orr.instructions(True)
    assert "an agency's office or headquarters named in the text is not the object's place" in agency.lower()
    assert agency.replace(orr.AGENCY_OFFICE_SENTENCE, "") == orr.instructions(True)
    assert [orr.prompt_version(*f) for f in ((False, False), (False, True), (True, False), (True, True))] == [
        "analysis-v6", "analysis-v6", "analysis-v6-loc", "analysis-v6-loc-agency"]
    assert metadata("es", "m", location_from_text=True, agency_rule=True)["prompt_version"] == "analysis-v6-loc-agency"
    assert OpenRouterAnalyzer("k", "m").agency_rule is False
    assert OpenRouterAnalyzer("k", "m", place_description_chars=1500).agency_rule is True
