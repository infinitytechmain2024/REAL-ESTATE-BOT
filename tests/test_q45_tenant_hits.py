"""Q45: for a home buyer a tenanted, occupied or auctioned flat is a similar variant, not an exact one
(REVIEW_TENANT_HITS, off by default; rubric rule «Жилец»): the reviewer's «hits» show those words and the prompt
(«-tn») says what they mean. Mocks only (``httpx.MockTransport``).
"""

from __future__ import annotations

import hashlib
import itertools
import json

import httpx
import pytest

from bot.agents import reviewer as rv
from bot.campaign import final_report, plan_campaign
from bot.campaign.quote_hits import home_purchase, quote_hits, tenant_terms
from bot.campaign.relevance import ReviewerJudge
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.store import MemoryCampaignStore
from tests.test_campaign_relevance import CHAT, USER
from tests.test_campaign_runner import ButtonMessenger
from tests.test_reviewer import settings

# sha256 of every prompt before Q45 (main at 0327afd), by (soft_wishes, townhouse_is_house, quote_hits,
# non_residential): the flag off sends them byte for byte.
OLD_SHA256 = {
    (False, False, False, False): "dcfe0a2bc6d951aaa6eaf74a16fb8f93ce3ac8436f7f9d2319f2c35a661b1a51",
    (False, False, False, True): "210780ecba9cfcffdcd453b558a999f916b330c90213e524a4ebcde3b55edaeb",
    (False, False, True, False): "c3a7097a7331dab40f88820ea609369ee4f5619c7f6e5dc4f21824fe2e98c109",
    (False, False, True, True): "66a7461e1040c3eb87e4de783f0a02a4d2e15d8e2dcad30decc1faf3f7fcf573",
    (False, True, False, False): "91cd5248734674d6be3b939dbc80ce29c29c3d68756a0ae9314def21676adcc3",
    (False, True, False, True): "569145f32ffc28df18e12f7b1cab09578f30d2cbf7a80f4c2c0cbf0654037ade",
    (False, True, True, False): "17670bc33d7bc6fc9df0b9dd5c38e55a143a170d8c1215369b38625a7279888a",
    (False, True, True, True): "ead7f4d204b7659cc4d1e1fa32ba63ca69af65c346dd782e5203159e9d70c208",
    (True, False, False, False): "04a6f5ad8ef13b4288eeec83a846e773206f038da6557c4d0557952101ea8ce0",
    (True, False, False, True): "c68cee7368970e1a0c5285db290c5c1d27ddd7778ed61a9d91d5c90b67e3f8e6",
    (True, False, True, False): "1f6714f47077f65727858ea9dea82e9627af491c1b10723e441a5d7a3987f175",
    (True, False, True, True): "d8a795675e11589b475433b43a789d5758b3d790d12d4adacb6c78afdbbbaa72",
    (True, True, False, False): "cba94ff36a6e34bbc461eedfc615dd6d43112a85c0caa751cd0961121dafd614",
    (True, True, False, True): "ded5633bc7d77e174d055628732523cfe6d45c00c13985abb5c11ced665474dc",
    (True, True, True, False): "9a23f079702bdae47433f445c9496f942bf406c17b0ec15823ac24756cc4dc6c",
    (True, True, True, True): "4568c2650341993a2a7ffecbc98519c211416b8f849c51824ed182f770035c70",
}
FLAGS = list(itertools.product((False, True), repeat=4))
FILLER = "Piso luminoso de tres habitaciones, cocina equipada y baño reformado, cerca del metro. " * 12
# habitaclia i60699000000012 of the sim run b4-g2-s5: the tenant is named far into the description.
RENTED = FILLER + "Planta baja en Camí Fondo, 64 m², 180.000 €. Actualmente alquilada, contrato hasta 2027."


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _prompt(flags: tuple[bool, bool, bool, bool], tn: bool) -> tuple[str, str]:
    soft, th, hits, nr = flags
    return rv.prompt_of(soft_wishes=soft, townhouse_is_house=th, quote_hits=hits, non_residential=nr, tenant=tn)


# --- the flag and the prompt -------------------------------------------------------------------------------------------


def test_flag_off_by_default_and_reaches_the_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_TENANT_HITS", raising=False)
    off = settings()
    assert off.review_tenant_hits is False
    judge = off.relevance_judge()
    assert isinstance(judge, ReviewerJudge) and judge.reviewer.tenant_hits is False and judge.tenant_hits is False
    on = settings(REVIEW_TENANT_HITS="true").relevance_judge()
    assert on.reviewer.tenant_hits is True and on.tenant_hits is True


@pytest.mark.parametrize("flags", FLAGS)
def test_flag_off_prompts_are_byte_for_byte_the_old_ones(flags) -> None:
    version, system = _prompt(flags, tn=False)
    assert sha(system) == OLD_SHA256[flags]
    assert "-tn" not in version and "Not free to move in" not in system
    soft, th, hits, nr = flags
    assert (version, system) == rv.prompt_of(soft_wishes=soft, townhouse_is_house=th, quote_hits=hits,
                                             non_residential=nr)


@pytest.mark.parametrize("flags", FLAGS)
def test_flag_on_prompt_has_its_own_version_and_the_rule(flags) -> None:
    old_version, old_system = _prompt(flags, tn=False)
    version, system = _prompt(flags, tn=True)
    soft, th, hits, nr = flags
    assert version == (f"reviewer-v3{'-soft' if soft else ''}{'-th' if th else ''}{'-nr' if nr else ''}-tn"
                       f"{'-hits' if hits else ''}")
    assert version.replace("-tn", "") == old_version
    assert sha(system) not in OLD_SHA256.values()
    assert system.replace(rv._TENANT, "") == old_system  # only the rule is added
    assert version in final_report._LEGAL_RULE_PROMPTS and rv.is_soft(version) is soft
    rule = system.split("- Not free to move in:", 1)[1].split("- budget, rooms, area:", 1)[0]
    for word in ('"task.hard.deal" is "sale"', "apartment or house", "«alquilada»", "«ocupada»", "«sin posesión»",
                 "«nuda propiedad»", "«subasta»", "«pujas»", '"deal" is\n  "unknown"', '"overall" is "near"',
                 "«с арендатором»", "«аукцион»", "«libre de inquilinos»", "«ideal para alquilar»", "«inversión»"):
        assert word in rule, word
    if nr:  # after the non-residential rule
        assert system.index("- Not a home:") < system.index("- Not free to move in:")


def test_all_thirty_two_versions_are_distinct_and_trusted_by_the_report() -> None:
    versions = {_prompt(flags, tn)[0] for flags in FLAGS for tn in (False, True)}
    assert len(versions) == 32 and versions == set(final_report._LEGAL_RULE_PROMPTS)


# --- the hits ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "word"), [
    ("Se vende piso actualmente alquilada a buen inquilino.", "alquilada"),
    ("Vivienda ocupada, sin posesión.", "ocupada"),
    ("Piso ocupado por terceros.", "ocupado"),
    ("Venta en subasta judicial, pujas desde 120.000 €.", "subasta"),
    ("Se vende la nuda propiedad del piso.", "nuda propiedad"),
])
def test_tenant_and_auction_words_give_hits(text: str, word: str) -> None:
    hits = quote_hits(text, tenant_terms())
    assert hits and word in hits[0].casefold()
    assert quote_hits(text, []) == [] and quote_hits(text, ["бассейн"]) == []  # only the Q45 terms find them


def test_tenant_hits_skip_the_excerpt_and_ignore_neighbour_words() -> None:
    hits = quote_hits(RENTED, tenant_terms(), skip_before=900)
    assert len(hits) == 1 and "Actualmente alquilada" in hits[0] and hits[0] in " ".join(RENTED.split())
    assert quote_hits(RENTED, tenant_terms(), skip_before=len(RENTED)) == []  # already in the excerpt
    assert quote_hits("Ideal para alquilar, buena rentabilidad.", tenant_terms()) == []  # «alquilar» is no word
    assert quote_hits("La cocina ocupa todo el fondo.", tenant_terms()) == []


def test_home_purchase_only_for_a_home_sale_not_investors_nor_rent() -> None:
    assert home_purchase({"deal": "sale", "type": "apartment"}, "real_estate") is True
    assert home_purchase({"deal": "sale", "type": "house"}, "real_estate") is True
    assert home_purchase({"deal": "sale", "type": "apartment"}, "investors") is False
    assert home_purchase({"deal": "rent", "type": "apartment"}, "real_estate") is False
    assert home_purchase({"deal": "sale", "type": "land"}, "real_estate") is False
    assert home_purchase({"deal": "sale"}, "real_estate") is False


# --- the runner --------------------------------------------------------------------------------------------------------

FLAT_TASK = "Busco piso para comprar en Valencia, 2-3 habitaciones, hasta 250.000 €"
FLAT_SPEC = {"deal": "sale", "property_type": "apartment", "rooms": {"min": 2},
             "budget": {"max": 250_000, "currency": "EUR"},
             "place": {"name": "Valencia", "level": "city", "country": "ES"}, "delivery": {"show_similar": False}}


async def _run(*, tenant: bool, spec: dict, vertical: str = "real_estate") -> tuple[MemoryRunStore, list[dict]]:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        data = json.loads(body["messages"][1]["content"].split("\n", 1)[1])
        hits = " ".join(data["finding"].get("hits") or [])
        rows = []
        for name in data["task"]["criteria"]:  # the model follows the rule only when it sees the word
            if name == "deal" and "alquilada" in hits:
                rows.append({"name": name, "verdict": "unknown", "quote": "Actualmente alquilada", "note_ru": "жилец"})
            else:
                rows.append({"name": name, "verdict": "pass", "quote": "Planta baja en Camí Fondo", "note_ru": "ок"})
        tenant_seen = "alquilada" in hits
        answer = {"criteria": rows, "overall": "near" if tenant_seen else "match",
                  "deviation_ru": "с арендатором" if tenant_seen else None, "confidence": 0.9}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

    reviewer = rv.OpenRouterReviewer(api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                     quote_hits=True, tenant=tenant)
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    runner = CampaignRunner(campaigns, store, ButtonMessenger(), None,
                            config=RunnerConfig(window_cooldown_seconds=0, relevance_fail_closed=True),
                            relevance=ReviewerJudge(reviewer))
    plan = plan_campaign(FLAT_TASK, vertical="real_estate", location="Valencia")
    cid = await campaigns.create(plan, chat_id=CHAT, requested_by=USER, source_text=FLAT_TASK, actor="telegram:42",
                                 spec=spec)
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    payload = {"schema_version": "analysis-v6", "summary_ru": "Квартира в Валенсии", "location": "Valencia",
               "country": "ES", "price_amount": 180_000, "price_currency": "EUR", "deal_type": "sale",
               "property_type": "apartment", "area_m2": 64, "rooms": 3, "listing_kind": "offer",
               "original_post_link": "https://www.habitaclia.com/i60699000000012.htm"}
    store.add_finding(cid, "flat", "🏠 flat", payload=payload, original=RENTED, vertical=vertical)
    await runner.tick()
    return store, seen


def _sent(seen: list[dict]) -> tuple[str, dict]:
    return seen[0]["messages"][0]["content"], json.loads(seen[0]["messages"][1]["content"].split("\n", 1)[1])["finding"]


async def test_flag_off_the_runner_sends_the_old_prompt_and_hits() -> None:
    store, seen = await _run(tenant=False, spec=FLAT_SPEC)
    system, finding = _sent(seen)
    assert system == rv.prompt_of(quote_hits=True)[1]
    assert not any("alquilada" in hit for hit in finding.get("hits") or [])
    assert store.buckets["flat"][0] == "exact"  # as before Q45


async def test_flag_on_a_rented_flat_for_a_buyer_is_similar() -> None:
    store, seen = await _run(tenant=True, spec=FLAT_SPEC)
    system, finding = _sent(seen)
    assert system == rv.prompt_of(quote_hits=True, tenant=True)[1]
    assert any("Actualmente alquilada" in hit for hit in finding["hits"])
    assert "actualmente alquilada" in rv.haystack_of(finding)
    assert store.buckets["flat"][0] == "similar"


async def test_flag_on_a_rent_task_gets_no_tenant_hits() -> None:
    _, seen = await _run(tenant=True, spec={**FLAT_SPEC, "deal": "rent"})
    _, finding = _sent(seen)
    assert not any("alquilada" in hit for hit in finding.get("hits") or [])


async def test_flag_on_investor_findings_are_unchanged() -> None:
    _, seen = await _run(tenant=True, spec=FLAT_SPEC, vertical="investors")
    assert seen == []  # investors are never reviewed: no prompt, no hits, as before
