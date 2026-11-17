"""Q76: a club or community of real-estate investors that meets regularly is a lead (INVESTOR_CLUB_IS_LEAD, off by
default). Sim b7-g7-s8 «соинвесторы для проектов в Малаге»: the meetup page of Property Deal Network Marbella on
skiddle.com was «near» for the AI check («страница билетов»), the reach judge dropped the club («сообщество, а не
частный инвестор»), and the Uppery Club pages (``/evento/enuentroinversores/``) were read but dropped as
``not_a_listing`` before the analysis. Mocks only, no network.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from bot.campaign import MemoryCampaignStore
from bot.campaign import reach as reach_mod
from bot.campaign import relevance as rel
from bot.campaign.reach import (
    Candidate,
    MemoryReachStore,
    OpenRouterReachJudge,
    ReachCampaign,
    ReachConfig,
    ReachWorker,
)
from bot.campaign.relevance import OpenRouterRelevanceJudge
from bot.web_search.queries import FallbackQueryGenerator
from bot.web_search.searxng import SearchHit
from bot.web_search.store import MemoryWebStore
from bot.web_search.urls import url_key
from bot.web_search.worker import WebSearchConfig, WebSearchWorker
from tests.test_investor_web_queries import FixedPlanner, make_campaign
from tests.test_web_search import FakeFetcher, FakeSearcher, run_until_done

TASK = {"mode": "investors", "user_role": "raising", "place": "Málaga"}
FINDING = {"vertical": "investors", "summary": "Free monthly real estate investor networking meetup in Marbella"}


# --- the AI check (relevance) --------------------------------------------------------------------------------------


def _judge(handler: Any = None, **flags: bool) -> OpenRouterRelevanceJudge:
    transport = httpx.MockTransport(handler or (lambda _r: httpx.Response(500)))
    return OpenRouterRelevanceJudge(api_key="k", client=httpx.AsyncClient(transport=transport), **flags)


def test_the_club_rule_is_in_the_prompt_only_with_the_flag() -> None:
    assert "meets regularly" in rel.CLUB_RULES and "ticket site" in rel.CLUB_RULES
    assert "one-off paid conference" in rel.CLUB_RULES and "organiser" in rel.CLUB_RULES
    on = _judge(investor_leads=True, investor_club=True).prompt(TASK, FINDING)
    assert on == ("relevance-v3-isrc-club", rel.SYSTEM_INVESTORS_CLUB)
    assert rel.INVESTOR_RULES in on[1] and rel.CLUB_RULES in on[1]
    assert on[1].endswith(rel.SYSTEM[rel.SYSTEM.index(rel._REASON_LINE):])
    assert _judge(investor_club=True).prompt(TASK, FINDING) == ("relevance-v3-club", rel.SYSTEM_CLUB)
    # off: the prompts as before, byte for byte
    assert _judge(investor_leads=True).prompt(TASK, FINDING) == ("relevance-v3-isrc", rel.SYSTEM_INVESTORS)
    assert _judge().prompt(TASK, FINDING) == ("relevance-v3", rel.SYSTEM)
    assert rel.CLUB_RULES not in rel.SYSTEM_INVESTORS and rel.CLUB_RULES not in rel.SYSTEM


def test_a_real_estate_finding_and_a_seller_keep_their_prompts() -> None:
    judge = _judge(investor_leads=True, investor_club=True, seller_role=True)
    assert judge.prompt({"mode": "real_estate"}, {"vertical": "real_estate"}) == ("relevance-v3", rel.SYSTEM)
    seller = judge.prompt({**TASK, "user_role": "selling"}, FINDING)
    assert seller == (rel.SELLER_PROMPT_VERSION, rel.SYSTEM_SELLER) and rel.CLUB_RULES not in seller[1]


@pytest.mark.asyncio
async def test_the_model_gets_the_club_rule_only_with_the_flag() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"verdict": "match", "reason": "Клуб инвесторов в Марбелье.", "deviation_ru": ""})}}]})

    await _judge(handler, investor_leads=True, investor_club=True).judge(TASK, FINDING)
    await _judge(handler, investor_leads=True).judge(TASK, FINDING)
    assert seen == [rel.SYSTEM_INVESTORS_CLUB, rel.SYSTEM_INVESTORS]


# --- the reach judge -----------------------------------------------------------------------------------------------


CLUB = ReachCampaign("c1", "Málaga", {"es": "Málaga", "en": "Malaga"}, ("es", "en"), "investors · Málaga",
                     "ищу соинвесторов в проекты в Малаге", "ES",
                     {"who": ["private"], "user_role": "raising", "ticket": {"min": 50000, "currency": "EUR"}})


class FakeJSON:
    def __init__(self) -> None:
        self.systems: list[str] = []

    async def complete(self, model, system, user, *, schema, name, max_tokens):  # type: ignore[no-untyped-def]
        self.systems.append(system)
        return json.dumps({"results": []})

    async def aclose(self) -> None:
        return None


def _fields(campaign: ReachCampaign) -> dict[str, Any]:
    return {k: getattr(campaign, k) for k in campaign.__dataclass_fields__}


@pytest.mark.asyncio
async def test_the_reach_model_gets_the_club_rule_only_for_a_club_campaign() -> None:
    judge = OpenRouterReachJudge(api_key="k", model="m")
    judge._client = fake = FakeJSON()  # type: ignore[assignment]
    candidate = Candidate("https://example.com/a", "web", "page", "t", "s")
    await judge.judge(ReachCampaign(**{**_fields(CLUB), "club": True}), [candidate])
    await judge.judge(CLUB, [candidate])
    await judge.judge(ReachCampaign(**{**_fields(CLUB), "club": True, "seller": True}), [candidate])
    assert fake.systems[0] == reach_mod.SYSTEM + reach_mod.SPEC_RULES + reach_mod.CLUB_RULES
    assert fake.systems[1] == reach_mod.SYSTEM + reach_mod.SPEC_RULES  # off: as before
    assert fake.systems[2] == reach_mod.SYSTEM + reach_mod.SPEC_RULES + reach_mod.SELLER_RULES
    assert "even when data.investor.who does" in reach_mod.CLUB_RULES


class _Judge:
    def __init__(self) -> None:
        self.campaigns: list[ReachCampaign] = []

    async def judge(self, campaign: ReachCampaign, candidates: Any) -> list[Any]:
        self.campaigns.append(campaign)
        return [reach_mod.Judged("other", False, 0.0)] * len(candidates)

    async def queries(self, campaign: ReachCampaign, count: int) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_the_reach_worker_marks_a_club_campaign_only_with_the_flag() -> None:
    class AnySearcher:
        async def search(self, query: str, *, language: str | None = None) -> list[SearchHit]:
            return [SearchHit("https://example.com/club-inversores-malaga", "Club de inversores Málaga", "Málaga")]

    for flag in (True, False):
        judge = _Judge()
        searcher = AnySearcher()
        await ReachWorker(MemoryReachStore([CLUB]), searcher, judge,  # type: ignore[arg-type]
                          config=ReachConfig(queries_per_tick=1, investor_club=flag)).tick()
        assert judge.campaigns and all(c.club is flag for c in judge.campaigns)


# --- the web stage -------------------------------------------------------------------------------------------------


EVENT = "https://uppery.club/evento/enuentroinversores/"
EVENT_HTML = ("<html><head><title>Encuentro de Inversores en Málaga – Uppery Club</title></head><body><main>"
              "<h1>GRAN ENCUENTRO DE INVERSORES EN MÁLAGA</h1><p>Tenemos el placer de organizar y coordinar este "
              "evento junto a la comunidad de Zona3, diseñado para reunir a los más apasionados del mundo de la "
              "inversión inmobiliaria. Networking de alto nivel entre inversores de toda España.</p></main></body></html>")


async def _posts(mode: str, *, flag: bool) -> tuple[list[str], str | None]:
    campaigns, store = MemoryCampaignStore(), MemoryWebStore(None)
    store.campaigns = campaigns
    cid = await make_campaign(campaigns, mode)
    texts = {EVENT: ("Encuentro de Inversores en Málaga", "Vive una jornada única de inversión, networking")}
    w = WebSearchWorker(campaigns, store, FakeSearcher(default=[EVENT], texts=texts), FakeFetcher({EVENT: EVENT_HTML}),
                        FallbackQueryGenerator(None), planner=FixedPlanner(),
                        config=WebSearchConfig(cover_portals=False, queries_per_round=2, max_queries_per_campaign=2,
                                               domain_policy="off", investor_web_queries=True,
                                               investor_club_is_lead=flag))
    await run_until_done(w, cid)
    row = store.urls[cid].get(url_key(EVENT))
    return [p["url"] for p in store.posts], row.detail if row else None


@pytest.mark.asyncio
async def test_an_investor_event_page_is_analysed_with_the_flag() -> None:
    posts, _ = await _posts("investors", flag=True)
    assert posts == [EVENT]


@pytest.mark.asyncio
async def test_flag_off_the_page_is_dropped_as_not_a_listing_as_before() -> None:
    posts, detail = await _posts("investors", flag=False)
    assert posts == [] and detail == "not_a_listing"


def test_the_flag_is_off_by_default_and_reaches_the_campaign_runner(monkeypatch) -> None:
    from bot.campaign.settings import CampaignRunnerSettings
    from bot.web_search.settings import WebSearchSettings

    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv("INVESTOR_CLUB_IS_LEAD", raising=False)
    assert CampaignRunnerSettings().investor_club_is_lead is False
    assert CampaignRunnerSettings().reach_config().investor_club is False
    assert WebSearchSettings(_env_file=None).config().investor_club_is_lead is False
    monkeypatch.setenv("INVESTOR_CLUB_IS_LEAD", "true")
    assert CampaignRunnerSettings().reach_config().investor_club is True
    assert CampaignRunnerSettings().relevance_judge().investor_club is True
    assert WebSearchSettings(_env_file=None).config().investor_club_is_lead is True
