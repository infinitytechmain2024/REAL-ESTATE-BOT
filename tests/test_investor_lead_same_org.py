"""Q60 (INVESTOR_LEAD_SAME_ORG): investor findings naming one organisation, club or person are one card, whatever the
hosts. Synthetic clubs and in-memory stores only (sim b5-g7-s6: one club on skiddle.com and two wegottickets.com pages).
"""

from __future__ import annotations

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.investor_findings import org_key, same_org
from bot.campaign.relevance import Relevance
from bot.campaign.runner import CampaignRunner, RunnerConfig, _same_lead_org
from bot.campaign.runs import MemoryRunStore, StreamFinding
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_near_match import ButtonMessenger

GOAL = "ищу соинвесторов в проекты в Севилье"
CLUB_PAGE = "https://events.example.org/whats-on/sevilla/investor-club/123/"
TICKETS_1 = "https://tickets.example.net/event/1001"
TICKETS_2 = "https://tickets.example.net/event/1002"
OTHER = "https://fund.example.com/about"


class SeqJudge:
    model = "fake/judge"

    def __init__(self, *verdicts: str) -> None:
        self.verdicts = list(verdicts)

    async def judge(self, task: dict, finding: dict) -> Relevance:
        return Relevance(self.verdicts.pop(0) if len(self.verdicts) > 1 else self.verdicts[0])


async def setup(judge=None, vertical: str = "investors", **config):
    campaigns = MemoryCampaignStore()
    store = MemoryRunStore(campaigns)
    messenger = ButtonMessenger()
    runner = CampaignRunner(campaigns, store, messenger, None, relevance=judge, config=RunnerConfig(
        relevance_fail_closed=False, window_cooldown_seconds=0, **config))
    plan = plan_campaign(GOAL, vertical=vertical, location="Sevilla")
    cid = await campaigns.create(plan, chat_id=-100, requested_by=42, source_text=GOAL, actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)  # the search is still running
    return store, messenger, runner, cid


def lead(store: MemoryRunStore, cid: str, fid: str, url: str, who: str | None, *, vertical: str = "investors") -> None:
    payload = {"who": who, "summary": f"Investor networking club page {fid}", "summary_ru": f"Клуб инвесторов {fid}",
               "location": "Sevilla", "country": "ES", "original_post_link": url}
    store.add_finding(cid, fid, f"📈 {fid}", payload=payload, original=f"page {fid} text", vertical=vertical, url=url)


def cards(messenger: ButtonMessenger) -> list[str]:
    return [t for _, _, t in messenger.sent if t.startswith("📈")]


def test_org_key_normalises_the_name() -> None:
    assert org_key({"who": "Triana Deal Makers Club (TDM)"}) == org_key({"who": "triana deal  makers club"})
    assert org_key({"who": "Inversiones Ñandú Rojo, S.L."}) == "inversiones nandu rojo"
    assert org_key({"who": "Property Deal Network Marbella (PDN)"}) == "property deal network marbella"
    assert org_key({"who": "Private investor"}) is None and org_key({"who": "inversor particular"}) is None
    assert org_key({"who": "Частный инвестор"}) is None
    assert org_key({"who": None}) is None and org_key({}) is None and org_key(None) is None
    assert not same_org({"who": "Triana Deal Makers Club"}, {"who": "Triana Deal Breakers"})
    assert not same_org({"who": "Private investor"}, {"who": "Private investor"})
    # review B1: descriptive «who» are no organisation: one word, generic words and places only
    for who in ("David", "Family office", "Investor from Madrid", "Business Angels", "Club de inversores",
                "Sevilla Investor Club", "Inversiones Ñandú, S.L.", "Real Estate Network Marbella"):
        assert org_key({"who": who}) is None, who
        assert not same_org({"who": who}, {"who": who}), who
    club = {"who": "Triana Deal Makers Club"}
    assert _same_lead_org(StreamFinding("a", "", club, vertical="investors"), StreamFinding("b", "", club, vertical="investors"))
    assert not _same_lead_org(StreamFinding("a", "", club, vertical="real_estate"),
                              StreamFinding("b", "", club, vertical="real_estate"))


def test_flag_is_off_by_default_and_reaches_the_runner(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.delenv("INVESTOR_LEAD_SAME_ORG", raising=False)
    assert RunnerConfig().investor_lead_same_org is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().investor_lead_same_org is False
    monkeypatch.setenv("INVESTOR_LEAD_SAME_ORG", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().investor_lead_same_org is True


async def test_off_one_club_on_three_hosts_is_three_cards() -> None:
    store, messenger, runner, cid = await setup()
    lead(store, cid, "a", CLUB_PAGE, "Triana Deal Makers Club")
    lead(store, cid, "b", TICKETS_1, "Triana Deal Makers Club (TDM)")
    lead(store, cid, "c", TICKETS_2, "triana deal makers club")
    await runner.step(cid)
    assert len(cards(messenger)) == 3 and store.duplicates == {}


async def test_on_one_club_on_three_hosts_is_one_card_with_also_on() -> None:
    store, messenger, runner, cid = await setup(investor_lead_same_org=True)
    lead(store, cid, "a", CLUB_PAGE, "Triana Deal Makers Club")
    lead(store, cid, "b", TICKETS_1, "Triana Deal Makers Club (TDM)")
    lead(store, cid, "c", TICKETS_2, "triana deal makers club")
    lead(store, cid, "d", OTHER, "Guadalquivir Homes Partners")
    await runner.step(cid)
    assert len(cards(messenger)) == 2  # the club once, the other fund apart
    assert store.duplicates == {"b": "a", "c": "a"}
    assert [link["url"] for link in store.cluster_links["a"]] == [TICKETS_1, TICKETS_2]


async def test_on_generic_or_missing_names_are_never_merged() -> None:
    store, messenger, runner, cid = await setup(investor_lead_same_org=True)
    lead(store, cid, "a", CLUB_PAGE, "Private investor")
    lead(store, cid, "b", TICKETS_1, "Private investor")
    lead(store, cid, "c", TICKETS_2, None)
    lead(store, cid, "d", OTHER, None)
    await runner.step(cid)
    assert len(cards(messenger)) == 4 and store.duplicates == {}


async def test_on_a_similar_copy_of_a_sent_card_is_not_held() -> None:
    store, messenger, runner, cid = await setup(SeqJudge("match", "near", "near"), investor_lead_same_org=True)
    lead(store, cid, "a", CLUB_PAGE, "Triana Deal Makers Club")
    lead(store, cid, "b", TICKETS_1, "Triana Deal Makers Club")
    lead(store, cid, "c", TICKETS_2, "Triana Deal Makers Club")
    await runner.step(cid)
    assert len(cards(messenger)) == 1
    assert store.duplicates == {"b": "a", "c": "a"}
    assert await store.held_findings(cid, "similar", 10) == [] and messenger.asks == []


async def test_on_held_copies_of_one_club_are_one_held_finding() -> None:
    store, _messenger, runner, cid = await setup(SeqJudge("near"), investor_lead_same_org=True)
    lead(store, cid, "b", TICKETS_1, "Triana Deal Makers Club")
    lead(store, cid, "c", TICKETS_2, "Triana Deal Makers Club")
    await runner.step(cid)
    assert [f.id for f in await store.held_findings(cid, "similar", 10)] == ["b"]
    assert store.duplicates == {"c": "b"}


async def test_off_held_copies_stay_apart() -> None:
    store, _messenger, runner, cid = await setup(SeqJudge("near"))
    lead(store, cid, "b", TICKETS_1, "Triana Deal Makers Club")
    lead(store, cid, "c", TICKETS_2, "Triana Deal Makers Club")
    await runner.step(cid)
    assert [f.id for f in await store.held_findings(cid, "similar", 10)] == ["b", "c"]
    assert store.duplicates == {}


async def test_on_investor_findings_of_a_both_search_are_merged_too() -> None:
    store, messenger, runner, cid = await setup(vertical="both", investor_lead_same_org=True)
    lead(store, cid, "a", CLUB_PAGE, "Triana Deal Makers Club")
    lead(store, cid, "b", TICKETS_1, "Triana Deal Makers Club")
    await runner.step(cid)
    assert len(cards(messenger)) == 1 and store.duplicates == {"b": "a"}
