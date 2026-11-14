"""Q63 (INVESTOR_SELLER_ROLE): a person who sells a property and seeks its buyers is not raising money.

Sim b5-n2-s6: «Im an agent, have a 4 bed villa in Estepona asking 1.2M EUR, need serious buyers or investors for it»
was stored as user_role=raising («привлекаю деньги») and the web and reach stages searched for business angels and
startups. With the flag the role is «selling»: the interviewer and the rules read it, the card shows it, the investor
rounds and the reach look for buyers of that property, and the analysis, the AI check and the reach judge are told
who fits. Off: two roles and every prompt byte for byte as before. Mocks only (fake models, MockTransport).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from bot.analysis_pipeline import openrouter
from bot.analysis_pipeline.openrouter import OpenRouterAnalyzer, instructions, prompt_version
from bot.analysis_pipeline.pipeline import AnalysisPipeline, metadata
from bot.analysis_pipeline.store import build_task_hint
from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign import reach as reach_mod
from bot.campaign import relevance as rel
from bot.campaign.models import Campaign
from bot.campaign.reach import (
    Candidate,
    MemoryReachStore,
    OpenRouterReachJudge,
    ReachCampaign,
    ReachConfig,
    ReachWorker,
    plan_queries,
    rule_judge,
)
from bot.campaign.relevance import OpenRouterRelevanceJudge, ReviewerJudge, task_data
from bot.campaign.spec import TaskSpec
from bot.control_plane import interviewer as iv
from bot.control_plane.anthropic_interviewer import system_prompt
from bot.control_plane.interviewer import OpenRouterInterviewer, parse_turn
from bot.control_plane.rules import RuleInterviewer, options_for, question_for, selling_in_text
from bot.web_search.queries import (
    INVESTOR_SYSTEM,
    SELLER_SYSTEM,
    OpenRouterQueryGenerator,
    QueryTask,
    TemplateQueryGenerator,
)
from bot.web_search.worker import investor_part, query_task
from tests.test_interviewer import FakeInterviewer, ask, with_fake
from tests.test_investor_lead_source import answer, ev, mock_model
from tests.test_investor_reach import FakeSearcher
from tests.test_user_intake import USER, callbacks, enough, plane, press, say

TASK = "Im an agent, have a 4 bed villa in Estepona asking 1.2M EUR, need serious buyers or investors for it"
ESTEPONA = {"name": "Estepona", "country": "ES", "level": "city",
            "names": {"ru": "Эстепона", "es": "Estepona", "ru_in": "Эстепоне"}}
STARTUP = re.compile(r"startup|стартап|business angel|angel investor|бизнес-ангел|venture|венчур|emprendedor|"
                     r"entrepreneur|предпринимат|crowdfunding|краудфандинг", re.IGNORECASE)
SELLER_SPEC = {"place": ESTEPONA, "property_type": "house",
               "investor": {"who": ["private"], "ticket": {"min": 1200000, "currency": "EUR"},
                            "user_role": "selling"}}


# --- the spec and the card ---------------------------------------------------------------------------------------


def test_the_spec_keeps_selling_and_the_card_shows_it() -> None:
    spec = TaskSpec.model_validate({"mode": "investors", **SELLER_SPEC})
    assert spec.investor.user_role == "selling"
    assert "Ваша роль: продаю объект, ищу покупателя" in spec.summary_ru()
    raising = spec.merged({"investor": {"user_role": "raising"}})
    assert "Ваша роль: привлекаю деньги" in raising.summary_ru()
    assert TaskSpec.model_validate({"investor": {"user_role": "something"}}).investor.user_role is None


# --- the interviewer (prompt and parsing) ------------------------------------------------------------------------


def test_the_seller_prompt_adds_the_role_and_off_is_the_old_prompt() -> None:
    assert iv.system_text() is iv.SYSTEM and iv.system_text(False) == iv.SYSTEM
    assert "selling" not in iv.SYSTEM
    seller = iv.system_text(True)
    assert seller == iv.SYSTEM_SELLER and seller.startswith(iv.SYSTEM.split('"user_role"')[0])
    assert "selling (the person has a property to sell" in seller and "ROLE selling" in seller
    assert "need serious buyers or investors" in seller
    assert system_prompt("investors").startswith(iv.SYSTEM)
    assert system_prompt("investors", seller_role=True).startswith(iv.SYSTEM_SELLER)
    assert system_prompt("unknown") == iv.SYSTEM and system_prompt("unknown", seller_role=True) == iv.SYSTEM_SELLER


def test_a_selling_role_from_the_model_is_taken_only_with_the_flag() -> None:
    content = json.dumps({"spec": {"investor": {"user_role": "selling"}}, "question": "Какой тикет?", "done": False})
    current = TaskSpec(mode="investors")
    assert parse_turn(content, current).spec.investor.user_role is None
    assert parse_turn(content, current, seller_role=True).spec.investor.user_role == "selling"
    raising = current.merged({"investor": {"user_role": "raising"}})
    assert parse_turn(content, raising).spec.investor.user_role == "raising"  # off: the old role stays


@pytest.mark.asyncio
async def test_the_openrouter_interviewer_sends_the_seller_prompt_only_with_the_flag() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        content = json.dumps({"spec": {"place": ESTEPONA, "investor": {"user_role": "selling"}},
                              "question": "Какой бюджет у покупателя?", "done": False, "asking": "investor.ticket"})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    turns = []
    for flag in (False, True):
        model = OpenRouterInterviewer(api_key="k", model="m", timeout_seconds=5, seller_role=flag,
                                      client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        turns.append(await model.interview(mode="investors", spec=TaskSpec(mode="investors"), dialogue=[],
                                           message=TASK))
    assert sent[0]["messages"][0]["content"] == iv.SYSTEM
    assert sent[1]["messages"][0]["content"] == iv.SYSTEM_SELLER
    assert sent[0]["messages"][1] == sent[1]["messages"][1]  # the state is the same
    assert [t.spec.investor.user_role for t in turns] == [None, "selling"]


# --- the rules -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    TASK, "продаю виллу в Марбелье, ищу покупателя", "vendo chalet en Estepona, busco comprador",
    "Продам дом в Михасе, нужен покупатель или инвестор", "we are selling our villa in Marbella",
    "шукаю покупця на квартиру у Валенсії"])
def test_the_seller_words_are_recognised(text: str) -> None:
    assert selling_in_text(text)


@pytest.mark.parametrize("text", [
    "ищу инвесторов для стартапа в Барселоне", "хочу вложить 200 тыс в Малаге", "не продаю, ищу инвесторов в проект",
    "looking for investors for my project", "I want to buy a villa in Estepona", "busco inversores para mi proyecto",
    # the review's false positives: someone else's sale, a share or a business, a sale to invest, a buyer's agent
    "ищу виллу, продавцы пишите", "ищу виллу в Марбелье, которую продаёт собственник",
    "Хочу купить квартиру, продаётся рядом с морем", "продаю долю в проекте, ищу инвестора",
    "выход из проекта, ищу инвестора на мою долю", "агент ищет объекты для покупателя",
    "ищу объекты для покупателя в Малаге", "ищу виллу для покупателя", "looking for villas for buyers in Marbella",
    "I'm a buyer's agent, looking for villas for my buyers", "we are selling our startup, need investors",
    "I'm selling my apartment to invest, looking for projects in Malaga",
    "продаю квартиру в Киеве, хочу вложить деньги в проект в Малаге",
    "vendo participaciones de mi empresa, busco inversores", "sell-side advisory, looking for investors",
    "не продаю, ищу деньги на проект", "I have buyers looking for villas in Estepona", "продаю бизнес в Малаге"])
def test_other_tasks_are_not_selling(text: str) -> None:
    assert not selling_in_text(text)


@pytest.mark.asyncio
async def test_the_rules_read_selling_only_with_the_flag() -> None:
    spec = TaskSpec(mode="investors")
    on = await RuleInterviewer(seller_role=True).interview(mode="investors", spec=spec, dialogue=[],
                                                           message="продаю виллу в Марбелье, ищу покупателя")
    off = await RuleInterviewer().interview(mode="investors", spec=spec, dialogue=[],
                                            message="продаю виллу в Марбелье, ищу покупателя")
    assert on.spec.investor.user_role == "selling" and off.spec.investor.user_role is None
    later = await RuleInterviewer(seller_role=True).interview(  # not the task, not the role question: not read
        mode="investors", spec=spec, dialogue=[{"role": "user", "text": "инвесторы в Малаге"}],
        message="продаю виллу, ищу покупателя", asking="context.answers")
    assert later.spec.investor.user_role is None
    asked = await RuleInterviewer(seller_role=True).interview(
        mode="investors", spec=spec, dialogue=[{"role": "user", "text": "инвесторы в Малаге"}],
        message="продаю виллу, ищу покупателя", asking="investor.user_role")
    assert asked.spec.investor.user_role == "selling"
    assert options_for("investor.user_role") == (("Ищу деньги", "role:raising"), ("Хочу вкладывать", "role:deploying"))
    assert options_for("investor.user_role", seller=True)[-1] == ("Продаю объект, ищу покупателя", "role:selling")
    assert "продаёте объект" in question_for(spec, "investor.user_role", seller=True)
    assert "продаёте" not in question_for(spec, "investor.user_role")


# --- the intake ----------------------------------------------------------------------------------------------------


class SellerFake(FakeInterviewer):
    """The scripted model with the flag's parsing (a selling role is taken)."""

    async def interview(self, *, mode, spec, dialogue, message, asking=None, editing=False):  # type: ignore[no-untyped-def]
        self.calls.append({"message": message, "asking": asking})
        item = self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]
        return parse_turn(json.dumps(item, ensure_ascii=False), spec, seller_role=True)


TICKET_Q = "Какую сумму должен быть готов вложить один покупатель или инвестор? Например: около 1,2 млн €."
RAISING = {"place": ESTEPONA, "investor": {"who": ["private"], "asset_class": ["real_estate"], "user_role": "raising"}}


async def _card(flag: bool) -> str:
    """The sim's dialogue: the model reads the task as raising (b5-n2-s6); the card after «Хватит, ищи»."""
    control, _, _ = with_fake(SellerFake(ask(TICKET_Q, "investor.ticket", RAISING)) if flag
                              else FakeInterviewer(ask(TICKET_Q, "investor.ticket", RAISING)))
    control.intake.seller_role = flag
    await press(control, USER, "mode:investors")
    assert (await say(control, USER, TASK)).text.endswith(TICKET_Q)
    return (await enough(control, USER)).text


@pytest.mark.asyncio
async def test_the_sim_task_is_selling_on_the_card_with_the_flag_and_raising_without() -> None:
    on, off = await _card(True), await _card(False)
    assert "Ваша роль: продаю объект, ищу покупателя" in on and "привлекаю деньги" not in on
    assert "Ваша роль: привлекаю деньги" in off and "продаю объект" not in off


@pytest.mark.asyncio
async def test_the_model_s_deploying_is_never_overridden() -> None:
    deploying = {"place": ESTEPONA, "investor": {"who": ["private"], "user_role": "deploying"}}
    control, _, _ = with_fake(SellerFake(ask(TICKET_Q, "investor.ticket", deploying)))
    control.intake.seller_role = True
    await press(control, USER, "mode:investors")
    await say(control, USER, TASK)
    assert "Ваша роль: вкладываю деньги" in (await enough(control, USER)).text


@pytest.mark.asyncio
async def test_the_role_buttons_get_selling_with_the_flag() -> None:
    for flag in (False, True):
        control, _, _ = plane()
        control.intake.seller_role, control.intake.rules = flag, RuleInterviewer(seller_role=flag)
        await press(control, USER, "mode:investors")
        await say(control, USER, "инвесторы в Малаге")
        await say(control, USER, "частные инвесторы")
        reply = await say(control, USER, "от 1 млн евро")
        roles = [c for c in callbacks(reply) if c.startswith("task:role:")]
        if not flag:
            assert roles == ["task:role:raising", "task:role:deploying"]
            assert (await press(control, USER, "task:role:selling")).text == "Эта кнопка устарела."
            continue
        assert roles == ["task:role:raising", "task:role:deploying", "task:role:selling"]
        assert "Продаю объект, ищу покупателя" in [b.text for b in reply.buttons]
        await press(control, USER, "task:role:selling")
        card = (await enough(control, USER)).text
        assert "Ваша роль: продаю объект, ищу покупателя" in card


@pytest.mark.asyncio
async def test_the_role_conflict_question_gets_the_third_button() -> None:
    control, _, _ = with_fake(SellerFake(
        ask(TICKET_Q, "investor.ticket", {**SELLER_SPEC, "investor": {"who": ["private"]}}),
        ask("Ищете деньги или вкладываете?", "investor.user_role", {}),
        ask("Какой срок?", "context.answers", {"investor": {"user_role": "raising"}})))
    control.intake.seller_role = control.intake.role_conflict_check = True
    await press(control, USER, "mode:investors")
    await say(control, USER, TASK)
    await say(control, USER, "от 1,2 млн евро")
    clarify = await say(control, USER, "ищу деньги")
    assert clarify.text.startswith("Вы писали, что продаёте объект")
    assert callbacks(clarify) == ["task:role:raising", "task:role:deploying", "task:role:selling", "task:cancel"]
    await press(control, USER, "task:role:selling")
    assert "Ваша роль: продаю объект, ищу покупателя" in (await enough(control, USER)).text


# --- the web queries -----------------------------------------------------------------------------------------------


def _seller_task(**changes: Any) -> QueryTask:
    return QueryTask(goal="investors · Estepona", task_text=TASK, location="Estepona",
                     location_aliases={"es": "Estepona", "en": "Estepona", "ru": "Эстепона"}, vertical="investors",
                     country_code="ES", **{"investor_queries": True, "seller": True, **changes})


def test_the_seller_prompt_and_templates_look_for_buyers_without_startup_words() -> None:
    examples = " ".join(re.findall(r'"text": "([^"]+)"', SELLER_SYSTEM))
    assert "looking to buy villa Estepona" in examples and "busco comprar villa Costa del Sol" in examples
    assert not STARTUP.search(examples)
    texts = [q.text for q in TemplateQueryGenerator().candidates(_seller_task())]
    joined = " | ".join(texts)
    assert "busco comprar villa Estepona" in joined and "looking to buy villa Estepona" in joined
    assert "family office" in joined and "хочу купить виллу Estepona" in joined
    assert texts and not STARTUP.search(joined) and "site:" not in joined
    plain = " | ".join(q.text for q in TemplateQueryGenerator().candidates(_seller_task(seller=False)))
    assert "business angels" in plain and "comprar" not in plain  # off: the Q51 investor terms
    assert not _seller_task(investor_queries=False).seller_web  # only the investor round


@pytest.mark.asyncio
async def test_the_model_round_gets_the_seller_prompt_and_off_is_byte_for_byte() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        content = json.dumps({"queries": [{"text": "looking to buy villa Estepona", "language": "en"}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    generator = OpenRouterQueryGenerator(api_key="k", model="m", timeout_seconds=5,
                                         client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await generator.generate(_seller_task(), used=[], count=1)
    await generator.generate(_seller_task(seller=False), used=[], count=1)
    assert sent[0]["messages"][0]["content"] == SELLER_SYSTEM
    assert json.loads(sent[0]["messages"][1]["content"].split("\n", 1)[1])["user_role"] == "selling"
    assert sent[1]["messages"][0]["content"] == INVESTOR_SYSTEM
    assert "user_role" not in sent[1]["messages"][1]["content"]


async def _campaign(mode: str = "investors", role: str = "selling") -> Campaign:
    campaigns = MemoryCampaignStore()
    spec = TaskSpec.model_validate({"mode": mode, **SELLER_SPEC, "investor": {**SELLER_SPEC["investor"],
                                                                             "user_role": role}})
    plan = plan_campaign(TASK, vertical=mode, place={"en": "Estepona", "es": "Estepona", "country": "ES"}, spec=spec)  # type: ignore[arg-type]
    cid = await campaigns.create(plan, chat_id=1, requested_by=1, source_text=TASK, actor="test",
                                 spec=spec.model_dump(mode="json"))
    return await campaigns.get(cid)


@pytest.mark.asyncio
async def test_query_task_marks_a_seller_only_with_the_flag() -> None:
    campaign = await _campaign()
    assert not query_task(campaign, investor_queries=True).seller
    assert query_task(campaign, investor_queries=True, seller_role=True).seller_web
    raising = await _campaign(role="raising")
    assert not query_task(raising, investor_queries=True, seller_role=True).seller
    both = query_task(await _campaign("both"), seller_role=True)
    assert both.seller and not both.seller_web and investor_part(both).seller_web  # the both-mode investor round


# --- the reach ----------------------------------------------------------------------------------------------------


SELLER = ReachCampaign("s1", "Estepona", {"es": "Estepona", "en": "Estepona", "ru": "Эстепона"}, ("es", "en", "ru"),
                       "investors · Estepona", TASK, "ES", dict(SELLER_SPEC["investor"]), seller=True)


def test_the_reach_queries_of_a_seller_look_for_buyers() -> None:
    texts = [q.text for q in plan_queries(SELLER)]
    joined = " | ".join(texts)
    assert "looking to buy villa Estepona" in joined and "busco comprar villa Estepona" in joined
    assert "relocating to Estepona buying villa forum" in joined
    assert not STARTUP.search(joined) and "agente inmobiliario" not in joined
    plain = " | ".join(q.text for q in plan_queries(ReachCampaign(**{**_fields(SELLER), "seller": False})))
    assert "business angel" in plain  # off: the investor templates as before


def _fields(campaign: ReachCampaign) -> dict[str, Any]:
    return {k: getattr(campaign, k) for k in campaign.__dataclass_fields__}


@pytest.mark.parametrize(("title", "snippet", "relevant"), [
    ("Looking to buy a villa in Estepona", "Checked villas for sale in Estepona, budget 1.5M", True),
    ("Looking to buy a villa in Estepona", "Currently in alquiler, want to own", True),
    ("Looking to buy a villa in Estepona", "Our next adventure: moving to Spain", True),
    ("Want to buy a house in Estepona", "Selling our UK home first, budget 1.3M", True),
    ("Busco casa en Estepona para comprar", "presupuesto 1,2M", True),
    ("Moving to Estepona, buying a villa", "budget 1.5M EUR", True),
    ("Хочу купить виллу, Estepona", "рассматриваю предложения о продаже от собственников", True),
    ("Buyer\u2019s agent Estepona", "We find homes for our clients", False),
    ("Villas for sale in Estepona", "Looking to buy? Call us", False),
    ("Estepona startups looking to buy", "seed round", False)])
def test_the_review_cases_of_the_seller_rules(title: str, snippet: str, relevant: bool) -> None:
    judged = rule_judge(Candidate("https://x.test/a", "web", "page", title, snippet), SELLER)
    assert judged.relevant is relevant


def test_the_rules_take_a_buyer_and_not_an_agent_for_a_seller() -> None:
    buyer = Candidate("https://www.reddit.com/r/x/comments/1/a/", "reddit", "post",
                      "Looking to buy a villa in Estepona", "Budget 1.5M EUR, sea view, 4 bedrooms")
    agent = Candidate("https://www.instagram.com/inmo/", "instagram", "profile",
                      "Agencia inmobiliaria Estepona", "Villas for sale in Estepona")
    angels = Candidate("https://aban.es/estepona", "web", "page", "Red de business angels Estepona", "Invertimos")
    assert [rule_judge(c, SELLER).relevant for c in (buyer, agent, angels)] == [True, False, False]
    assert rule_judge(buyer, SELLER).kind == "investor"
    off = ReachCampaign(**{**_fields(SELLER), "seller": False})
    assert rule_judge(angels, off).relevant  # off: a network in the city counts, as before


class FakeJSON:
    def __init__(self) -> None:
        self.systems: list[str] = []

    async def complete(self, model, system, user, *, schema, name, max_tokens):  # type: ignore[no-untyped-def]
        self.systems.append(system)
        if name == "reach_queries":
            return json.dumps({"queries": [{"platform": "web", "language": "en", "text": "looking to buy villa Estepona"}]})
        return json.dumps({"results": []})

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_the_reach_model_gets_the_seller_rules_only_for_a_seller() -> None:
    judge = OpenRouterReachJudge(api_key="k", model="m")
    judge._client = fake = FakeJSON()  # type: ignore[assignment]
    candidate = Candidate("https://example.com/a", "web", "page", "t", "s")
    for campaign in (SELLER, ReachCampaign(**{**_fields(SELLER), "seller": False})):
        await judge.judge(campaign, [candidate])
        await judge.queries(campaign, 2)
    assert fake.systems[0] == reach_mod.SYSTEM + reach_mod.SPEC_RULES + reach_mod.SELLER_RULES
    assert fake.systems[1].endswith(reach_mod.QUERY_SPEC_RULES + reach_mod.QUERY_SELLER_RULES)
    assert fake.systems[2] == reach_mod.SYSTEM + reach_mod.SPEC_RULES
    assert fake.systems[3] == reach_mod.QUERY_SYSTEM + reach_mod.QUERY_SPEC_RULES


@pytest.mark.asyncio
async def test_the_reach_worker_marks_a_seller_only_with_the_flag() -> None:
    stored = ReachCampaign(**{**_fields(SELLER), "seller": False})
    for flag, first in ((True, "site:reddit.com looking to buy villa Estepona"), (False, None)):
        searcher = FakeSearcher()
        await ReachWorker(MemoryReachStore([stored]), searcher,
                          config=ReachConfig(queries_per_tick=1, seller_role=flag)).tick()
        query = searcher.queries[0][0]
        assert (query == first) if flag else ("looking to buy" not in query)


# --- the AI check (relevance) --------------------------------------------------------------------------------------


def _campaign_obj(role: str | None) -> Campaign:
    plan = plan_campaign(TASK, vertical="investors", location="Estepona")
    spec = {"investor": {"user_role": role, "ticket": {"min": 1200000, "currency": "EUR"}}} if role else None
    return Campaign("c1", plan, "running", 1, 1, TASK, None, None, datetime.now(UTC), spec=spec)


def test_task_data_carries_selling_and_the_buyers_budget_only_with_the_flag() -> None:
    seller = _campaign_obj("selling")
    on = task_data(seller, user_role=True, seller_role=True)
    assert on["user_role"] == "selling" and on["ticket"] == {"min": 1200000, "currency": "EUR"}
    assert task_data(seller) == task_data(seller, user_role=True)  # off: no role, as before
    assert "user_role" not in task_data(seller, user_role=True)
    raising = _campaign_obj("raising")
    assert task_data(raising, user_role=True, seller_role=True) == task_data(raising, user_role=True)


@pytest.mark.asyncio
async def test_the_judge_gets_the_seller_criteria_only_with_the_flag() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content)["messages"][0]["content"])
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(
            {"verdict": "match", "reason": "р", "deviation_ru": ""})}}]})

    def judge(**flags: bool) -> OpenRouterRelevanceJudge:
        return OpenRouterRelevanceJudge(api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                                        **flags)

    task = task_data(_campaign_obj("selling"), user_role=True, seller_role=True)
    finding = {"vertical": "investors", "summary": "Looking to buy a villa in Estepona, budget 1.5M"}
    await judge(investor_leads=True, seller_role=True).judge(task, finding)
    await judge(investor_leads=True).judge(task, finding)
    await judge(seller_role=True).judge({**task, "user_role": "raising"}, finding)
    assert seen == [rel.SYSTEM_SELLER, rel.SYSTEM_INVESTORS, rel.SYSTEM]
    assert "another agent" in rel.SELLER_RULES and "business angel" in rel.SELLER_RULES
    assert "rental" in rel.SELLER_RULES and "budget must reach it" in rel.SELLER_RULES
    assert judge(seller_role=True).prompt(task, finding)[0] == "relevance-v3-sell"
    assert ReviewerJudge(object(), judge(seller_role=True)).seller_role is True  # type: ignore[arg-type]
    assert ReviewerJudge(object(), judge()).seller_role is False  # type: ignore[arg-type]


# --- the analysis --------------------------------------------------------------------------------------------------


BUYER_TEXT = "Hi all, we are looking to buy a 4 bedroom villa in Estepona with sea view, budget up to 1.5M EUR."


def test_the_seller_rule_replaces_the_investor_rule_and_off_is_unchanged() -> None:
    assert instructions() is openrouter.INSTRUCTIONS and openrouter.SELLER_LEAD_RULE not in instructions(True, True, True)
    seller = instructions(seller_rule=True)
    assert openrouter.SELLER_LEAD_RULE in seller and openrouter.INVESTOR_LEAD_RULE not in seller
    assert instructions(False, False, True, seller_rule=True) == seller
    assert prompt_version(seller_rule=True) == "analysis-v6-sell"
    assert prompt_version(investor_rule=True) == "analysis-v6-isrc"
    assert metadata("en", "m", seller_rule=True)["prompt_version"] == "analysis-v6-sell"


def test_the_hint_carries_selling_only_with_the_flag() -> None:
    spec = {"investor": {"user_role": "selling"}}
    assert build_task_hint("g", "Estepona", spec) == {"goal": "g", "place": "Estepona"}
    assert build_task_hint("g", "Estepona", spec, user_role=True) == {"goal": "g", "place": "Estepona"}
    assert build_task_hint("g", "Estepona", spec, seller_role=True)["user_role"] == "selling"
    raising = {"investor": {"user_role": "raising"}}
    assert "user_role" not in build_task_hint("g", "Estepona", raising, seller_role=True)


@pytest.mark.asyncio
async def test_a_buyers_post_is_read_with_the_seller_rule(monkeypatch) -> None:
    sent = mock_model(monkeypatch, lambda prompt, data: openrouter.SELLER_LEAD_RULE in prompt)
    hint = {"goal": "investors · Estepona", "place": "Estepona", "user_role": "selling"}
    on = await AnalysisPipeline(OpenRouterAnalyzer("k", "m", investor_lead_check=True, seller_role=True)).process(
        ev("https://www.reddit.com/r/Spain/comments/1/villa/", BUYER_TEXT, "Looking to buy a villa"), "investors",
        task_hint=hint)
    assert on.accepted and on.seller_rule and openrouter.INVESTOR_LEAD_RULE not in sent[0]["messages"][1]["content"]
    off = await AnalysisPipeline(OpenRouterAnalyzer("k", "m", investor_lead_check=True)).process(
        ev("https://www.reddit.com/r/Spain/comments/1/villa/", BUYER_TEXT), "investors", task_hint=hint)
    assert (off.accepted, off.reason, off.seller_rule, len(sent)) == (False, "irrelevant_keywords", False, 1)  # as before
    # a seller's post without a buy word, or without a property word, is still dropped before the model
    seller = AnalysisPipeline(OpenRouterAnalyzer("k", "m", seller_role=True))
    for text in ("Estepona sunset photos from our terrace, amazing evening with friends",
                 "We are looking for a babysitter in Estepona for the summer, please write"):
        outcome = await seller.process(ev("https://example.com/p", text), "investors", task_hint=hint)
        assert outcome.reason == "irrelevant_keywords"
    assert len(sent) == 1
    assert answer(True)["category"] == "investors"


# --- the settings --------------------------------------------------------------------------------------------------


def test_the_flag_is_off_by_default_and_reaches_every_service(monkeypatch) -> None:
    from bot.analysis_pipeline.settings import AnalysisSettings
    from bot.campaign.settings import CampaignRunnerSettings
    from bot.control_plane.settings import ControlPlaneSettings
    from bot.web_search.settings import WebSearchSettings

    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.delenv("INVESTOR_SELLER_ROLE", raising=False)
    assert AnalysisSettings().investor_seller_role is False
    assert CampaignRunnerSettings().investor_seller_role is False
    assert CampaignRunnerSettings().reach_config().seller_role is False
    assert WebSearchSettings(_env_file=None).config().investor_seller_role is False
    assert ControlPlaneSettings.from_env().investor_seller_role is False
    monkeypatch.setenv("INVESTOR_SELLER_ROLE", "true")
    assert AnalysisSettings().investor_seller_role is True
    assert CampaignRunnerSettings().reach_config().seller_role is True
    assert CampaignRunnerSettings().relevance_judge().seller_role is True
    assert WebSearchSettings(_env_file=None).config().investor_seller_role is True
    assert ControlPlaneSettings.from_env().investor_seller_role is True
