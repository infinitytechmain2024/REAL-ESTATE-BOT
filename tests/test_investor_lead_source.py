"""Q53 (INVESTOR_LEAD_SOURCE_CHECK): investor leads come from investor pages, not from news about deals.

The sim run b4-g7-s5 (co-investors in Málaga) sent 13 investor cards: 7 news articles about funds' deals and 6
crowdfunding projects that seek money themselves. With the flag a news or blog address never reaches the model, the
analysis prompt and the AI check say what an investor lead is, and a project seeking money is no lead for a person
who raises money (user_role raising) but is one for a person who invests (deploying). Off: as before, byte for byte.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from bot.analysis_pipeline import openrouter
from bot.analysis_pipeline.main import analyse_batch
from bot.analysis_pipeline.models import Evidence
from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer, instructions, prompt_version
from bot.analysis_pipeline.pipeline import INVESTOR_NEWS, AnalysisPipeline, metadata
from bot.analysis_pipeline.prefilter import news_page
from bot.analysis_pipeline.store import build_task_hint
from bot.campaign import plan_campaign
from bot.campaign import relevance as rel
from bot.campaign.models import Campaign
from bot.campaign.relevance import OpenRouterRelevanceJudge, ReviewerJudge, finding_data, task_data
from bot.utils import costs

# Addresses of the sim run's cards (docs/v3/SIM_RUBRIC.md §3: all of them miss).
NEWS = [
    "https://www.estrategiasdeinversion.com/actualidad/noticias/empresas/neinor-y-stoneshield-sellan-una-alianza-de-150-n-904087",
    "https://www.eldebate.com/espana/andalucia/malaga/20251020/busca-socio-gestionar-futuro-puerto-banus-malaga_346329_amp.html",
    "https://www.elespanol.com/malaga/malaga-ciudad/20240403/gran-operacion-inmobiliaria-malaga-grupo-inversores-compra/844665617_0.html",
    "https://www.alimarket.es/construccion/noticia_amp/353960/otero-group-y-yield-investment-levantaran-1-000-viviendas-btr-en-malaga",
    "https://www.costar.com/article/1712587055/neinor-and-orion-buy-land-in-malaga-to-develop-300-housing-units",
    "https://www.merca2.es/2026/06/20/genivs-viviendas-protegidas-malaga-2401931/",
    "https://realassets.ipe.com/propertyeu/hig-capital-buys-major-spanish-holiday-home-complex/10097802.article",
    "https://www.examplefund.es/blog/nuestra-ultima-operacion",
]
NOT_NEWS = [
    "https://www.examplefund.es/",
    "https://www.examplefund.es/invertimos-en-malaga",
    "https://www.wecity.com/en/opportunities/malaga-parque-lagunas/",
    "https://wecity.com/en/?p=13051",
    "https://www.linkedin.com/in/maria-business-angel/",
    "https://www.idealista.com/inmueble/12345/",
    "https://www.examplefund.es/proyectos/2026",  # a year alone is no date
]
FUND_TEXT = ("Somos un family office con sede en Málaga. Invertimos en proyectos residenciales en Málaga y la Costa del "
             "Sol, tickets de 50.000 a 500.000 euros por inversión. Buscamos proyectos y promotores con los que coinvertir.")
NEWS_TEXT = ("Neinor Homes y Stoneshield Capital sellan una alianza de 150 millones para invertir en un proyecto "
             "residencial en Marbella con inversores institucionales. Stoneshield tendrá el 70 % del capital.")
CROWD_TEXT = ("Oportunidad de inversión en Málaga: Premier Tilos SL solicita financiación a través de wecity, préstamo "
              "con garantía hipotecaria de 8.500.000 euros, rentabilidad 10 %. Invierte desde 100 euros.")


def ev(url: str, text: str, title: str = "Inversión en Málaga") -> Evidence:
    return Evidence(post_id="p1", source_id="s1", canonical_url=url, title=title, text=text)


def answer(relevant: bool, who: str | None = "Example Family Office") -> dict:
    return {"relevant": relevant, "confidence": 0.9, "summary": "s", "location": "Málaga", "price_signals": [],
            "related_links": [], "category": "investors" if relevant else "other", "reason": "r", "summary_ru": "c",
            "source_language": "es", "price_amount": None, "price_currency": None, "deal_type": None,
            "property_type": None, "rooms": None, "who": who, "listing_kind": "offer" if relevant else "other",
            "country": "ES", "area_m2": None,
            "evidence": {"price": None, "area": None, "rooms": None, "location": None}, "district": None,
            "address": None, "floor": None, "features": [], "condition": None, "listing_date": None}


def mock_model(monkeypatch, decide) -> list[dict]:
    """OpenRouter on a MockTransport: ``decide(prompt, data)`` -> relevant; returns the request bodies."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        prompt = body["messages"][1]["content"]
        data = json.loads(prompt[prompt.index('{"vertical"'):])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer(decide(prompt, data)))}}]})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return sent


def rule_reader(prompt: str, data: dict) -> bool:
    """A stand-in for the model that follows the Q53 rule only when the prompt carries it."""
    text = data["text"]
    if openrouter.INVESTOR_LEAD_RULE not in prompt:
        return True  # the old prompt: «someone offering or seeking investment» -- everything above passed
    if "solicita financiación" in text:
        return (data.get("task_hint") or {}).get("user_role") == "deploying"
    return "Invertimos en" in text or "Buscamos proyectos" in text


# --- the address of a news page ------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", NEWS)
def test_news_and_blog_addresses_are_news(url: str) -> None:
    assert news_page(url)


@pytest.mark.parametrize("url", [*NOT_NEWS, None, ""])
def test_investor_project_and_listing_addresses_are_not_news(url: str | None) -> None:
    assert not news_page(url)


# --- the analysis --------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_news_about_a_deal_is_no_lead_and_costs_no_model_call(monkeypatch) -> None:
    sent = mock_model(monkeypatch, rule_reader)
    pipeline = AnalysisPipeline(OpenRouterAnalyzer("k", "m", investor_lead_check=True))
    outcome = await pipeline.process(ev(NEWS[0], NEWS_TEXT), "investors")
    assert (outcome.accepted, outcome.reason, sent) == (False, INVESTOR_NEWS, [])
    # A news article without a news address: the model reads the rule and rejects it.
    outcome = await pipeline.process(ev("https://thecorner.eu/companies/neinor-stoneshield-marbella/125007/",
                                        NEWS_TEXT), "investors")
    assert not outcome.accepted and outcome.reason == "model_not_relevant" and len(sent) == 1
    assert openrouter.INVESTOR_LEAD_RULE in sent[0]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_a_funds_own_page_that_invests_in_malaga_is_a_lead(monkeypatch) -> None:
    sent = mock_model(monkeypatch, rule_reader)
    pipeline = AnalysisPipeline(OpenRouterAnalyzer("k", "m", investor_lead_check=True))
    outcome = await pipeline.process(ev("https://www.examplefund.es/invertimos-en-malaga", FUND_TEXT), "investors",
                                     task_hint={"goal": "investors · Málaga", "user_role": "raising"})
    assert outcome.accepted and outcome.investor_rule and outcome.formatted.startswith("📈")
    assert len(sent) == 1
    assert metadata("es", "m", investor_rule=outcome.investor_rule)["prompt_version"] == "analysis-v6-isrc"


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "lead"), [("raising", False), ("deploying", True)])
async def test_a_crowdfunding_project_is_a_lead_only_for_someone_who_invests(monkeypatch, role: str, lead: bool) -> None:
    sent = mock_model(monkeypatch, rule_reader)
    hint = build_task_hint("investors · Málaga", "Málaga", {"investor": {"user_role": role}}, user_role=True)
    assert hint == {"goal": "investors · Málaga", "place": "Málaga", "user_role": role}
    pipeline = AnalysisPipeline(OpenRouterAnalyzer("k", "m", investor_lead_check=True))
    outcome = await pipeline.process(ev("https://www.wecity.com/en/opportunities/malaga-parque-lagunas/", CROWD_TEXT),
                                     "investors", task_hint=hint)
    assert outcome.accepted is lead
    prompt = sent[0]["messages"][1]["content"]
    assert json.loads(prompt[prompt.index('{"vertical"'):])["task_hint"]["user_role"] == role


@pytest.mark.asyncio
async def test_flag_off_keeps_the_prompt_the_hint_and_the_old_leads(monkeypatch) -> None:
    sent = mock_model(monkeypatch, rule_reader)
    pipeline = AnalysisPipeline(OpenRouterAnalyzer("k", "m"))
    hint = build_task_hint("g", "Málaga", {"investor": {"user_role": "raising"}})
    assert hint == {"goal": "g", "place": "Málaga"}
    for url, text in ((NEWS[0], NEWS_TEXT), (NOT_NEWS[2], CROWD_TEXT)):
        outcome = await pipeline.process(ev(url, text), "investors", task_hint=hint)
        assert outcome.accepted and not outcome.investor_rule
    assert len(sent) == 2 and all(b["messages"][1]["content"].startswith(openrouter.INSTRUCTIONS) for b in sent)
    assert metadata("es", "m")["prompt_version"] == "analysis-v6"


def test_the_prompts_and_versions_without_the_flag_are_unchanged() -> None:
    for loc in (False, True):
        for agency in (False, True):
            old = instructions(loc, agency)
            assert instructions(loc, agency, False) == old and openrouter.INVESTOR_LEAD_RULE not in old
            new = instructions(loc, agency, True)
            assert new == old.replace(openrouter._NEVER_INVENT, openrouter.INVESTOR_LEAD_RULE + openrouter._NEVER_INVENT)
            assert prompt_version(loc, agency, True) == prompt_version(loc, agency) + "-isrc"
    assert instructions() is openrouter.INSTRUCTIONS
    assert prompt_version(True, True, True) == "analysis-v6-loc-agency-isrc"


@pytest.mark.asyncio
async def test_a_real_estate_post_keeps_the_old_prompt_with_the_flag(monkeypatch) -> None:
    sent = mock_model(monkeypatch, lambda prompt, data: True)
    analyzer = OpenRouterAnalyzer("k", "m", investor_lead_check=True)
    await analyzer.analyze(ev(NEWS[0], NEWS_TEXT), "real_estate")
    assert openrouter.INVESTOR_LEAD_RULE not in sent[0]["messages"][1]["content"]
    outcome = await AnalysisPipeline(analyzer).process(ev(NEWS[0], NEWS_TEXT * 2), "real_estate")
    assert outcome.reason != INVESTOR_NEWS and not outcome.investor_rule


class _Store:
    def __init__(self, url: str) -> None:
        self.url, self.finalized = url, []

    async def pending(self, n, claim_seconds):
        return [{"id": "11111111-1111-1111-1111-111111111111", "source_id": "s", "canonical_url": self.url,
                 "body_text": NEWS_TEXT, "title": "Neinor y Stoneshield", "published_at": None, "comments": "[]",
                 "vertical": "investors", "analysis_claim_token": "tok"}]

    async def task_hint(self, post_id):
        return None

    async def save(self, e, vertical, result, model, token):
        return "fid" if result.accepted else None

    async def finalize(self, post_id, token, *, accepted):
        self.finalized.append(accepted)
        return True


@pytest.mark.asyncio
async def test_a_dropped_news_page_is_booked_as_a_skip(monkeypatch) -> None:
    sent = mock_model(monkeypatch, rule_reader)
    sink = costs.MemoryLedger()
    costs.install(sink)
    try:
        store = _Store(NEWS[3])
        pipeline = AnalysisPipeline(OpenRouterAnalyzer("k", "m", investor_lead_check=True))
        formatted, claimed = await analyse_batch(store, pipeline, batch_size=5, claim_seconds=60, model="m")
    finally:
        costs.install(None)
    assert claimed == 1 and formatted["investors"] == [] and store.finalized == [False] and sent == []
    assert [(e.stage, e.kind, e.code, e.item) for e in sink.entries] == [
        ("llm", "skip", "prefilter_investor_news_page", "alimarket.es")]


def test_the_settings_wire_the_flag(monkeypatch) -> None:
    from bot.analysis_pipeline.settings import AnalysisSettings
    from bot.campaign.settings import CampaignRunnerSettings as CampaignSettings

    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert AnalysisSettings().investor_lead_source_check is False
    assert CampaignSettings().investor_lead_source_check is False
    monkeypatch.setenv("INVESTOR_LEAD_SOURCE_CHECK", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert AnalysisSettings().investor_lead_source_check is True
    for judge in ("legacy", "reviewer"):
        monkeypatch.setenv("CAMPAIGN_JUDGE", judge)
        assert CampaignSettings().relevance_judge().investor_leads is True


# --- the AI check (relevance) --------------------------------------------------------------------------------------


def investor_campaign(role: str | None) -> Campaign:
    text = "Кого ищем: частные инвесторы. Задача: ищу соинвесторов в проекты в Малаге, тикет от 50 000 €"
    plan = plan_campaign(text, vertical="investors", location="Málaga")
    spec = {"investor": {"user_role": role}} if role else None
    return Campaign("c1", plan, "running", 1, 1, text, None, None, datetime.now(UTC), spec=spec)


def judge_on(seen: list[dict], *, investor_leads: bool) -> OpenRouterRelevanceJudge:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        data = json.loads(body["messages"][1]["content"].split("\n", 1)[1])
        verdict = "match"
        if body["messages"][0]["content"] == rel.SYSTEM_INVESTORS and "wecity" in data["finding"]["link_host"]:
            verdict = "match" if data["task"].get("user_role") == "deploying" else "reject"
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"verdict": verdict, "reason": "р", "deviation_ru": ""})}}]})

    return OpenRouterRelevanceJudge(api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                    investor_leads=investor_leads)


CROWD_CARD = {"summary_ru": "Застройщик Premier Tilos SL ищет заём 8,5 млн евро через wecity на проект в Малаге.",
              "location": "Málaga", "country": "ES", "who": "Premier Tilos SL / wecity", "listing_kind": "offer",
              "original_post_link": "https://www.wecity.com/en/opportunities/malaga-parque-lagunas/"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("role", "verdict"), [("raising", "reject"), ("deploying", "match")])
async def test_the_ai_check_knows_the_role_for_a_crowdfunding_project(role: str, verdict: str) -> None:
    seen: list[dict] = []
    judge = judge_on(seen, investor_leads=True)
    campaign = investor_campaign(role)
    task = task_data(campaign, user_role=judge.investor_leads)
    assert task["user_role"] == role
    result = await judge.judge(task, finding_data(CROWD_CARD, vertical="investors"))
    assert result.verdict == verdict
    assert seen[0]["messages"][0]["content"] == rel.SYSTEM_INVESTORS
    assert judge.prompt(task, {})[0] == "relevance-v3-isrc"


@pytest.mark.asyncio
async def test_the_ai_check_without_the_flag_is_as_before() -> None:
    seen: list[dict] = []
    judge = judge_on(seen, investor_leads=False)
    campaign = investor_campaign("raising")
    task = task_data(campaign)
    assert "user_role" not in task and task == task_data(campaign, user_role=False)
    assert (await judge.judge(task, finding_data(CROWD_CARD, vertical="investors"))).verdict == "match"
    assert seen[0]["messages"][0]["content"] == rel.SYSTEM and judge.prompt(task, {})[0] == "relevance-v3"
    # A real-estate finding keeps the old system with the flag on.
    flagged = judge_on(seen, investor_leads=True)
    re_task = {"mode": "real_estate"}
    assert flagged.prompt(re_task, {"vertical": "real_estate"}) == ("relevance-v3", rel.SYSTEM)
    assert rel.SYSTEM_INVESTORS.replace(rel.INVESTOR_RULES, "") == rel.SYSTEM
    assert task_data(investor_campaign(None), user_role=True).get("user_role") is None


def test_the_reviewer_judge_reports_the_flag_of_its_fallback() -> None:
    legacy = OpenRouterRelevanceJudge(api_key="k", investor_leads=True)
    assert ReviewerJudge(object(), legacy).investor_leads is True  # type: ignore[arg-type]
    assert ReviewerJudge(object(), None).investor_leads is False  # type: ignore[arg-type]
