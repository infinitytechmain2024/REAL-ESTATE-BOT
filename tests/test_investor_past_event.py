"""Q60 (INVESTOR_PAST_EVENT_EXCLUDED): the page of a one-off event whose date is before the runner's today is no lead.
Synthetic pages, in-memory stores and the runner's own clock (sim b5-g7-s6: wegottickets.com meetups of 2025)."""

from __future__ import annotations

from datetime import date

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.final_report import REASONS
from bot.campaign.investor_findings import dates_named, event_page, past_event
from bot.campaign.metrics import REASONS as METRIC_REASONS
from bot.campaign.relevance import Relevance, reason_category
from bot.campaign.runner import CampaignRunner, RunnerConfig
from bot.campaign.runs import MemoryRunStore
from bot.campaign.settings import CampaignRunnerSettings
from tests.test_campaign_runner import Clock
from tests.test_near_match import ButtonMessenger

GOAL = "ищу соинвесторов в проекты в Севилье"
TICKETS = "https://tickets.example.net/event/1001"
CLUB = "https://sevilla-investors.example.org/about"
TODAY = date(2026, 9, 25)  # tests.test_campaign_runner.Clock


class CountingJudge:
    model = "fake/judge"

    def __init__(self) -> None:
        self.calls = 0

    async def judge(self, task: dict, finding: dict) -> Relevance:
        self.calls += 1
        return Relevance("match")


async def setup(vertical: str = "investors", **config):
    campaigns = MemoryCampaignStore()
    store, messenger, judge = MemoryRunStore(campaigns), ButtonMessenger(), CountingJudge()
    runner = CampaignRunner(campaigns, store, messenger, None, relevance=judge, now=Clock(), config=RunnerConfig(
        relevance_fail_closed=False, window_cooldown_seconds=0, **config))
    plan = plan_campaign(GOAL, vertical=vertical, location="Sevilla")
    cid = await campaigns.create(plan, chat_id=-100, requested_by=42, source_text=GOAL, actor="telegram:42")
    await campaigns.set_state(cid, "running", "campaign:test")
    store.add_groups(cid, 5)
    return store, messenger, judge, runner, cid


def page(store: MemoryRunStore, cid: str, fid: str, url: str, summary: str, *, vertical: str = "investors") -> None:
    payload = {"who": f"Club {fid}", "summary": summary, "summary_ru": None, "location": "Sevilla", "country": "ES"}
    store.add_finding(cid, fid, f"📈 {fid}", payload=payload, original=f"page {fid}", vertical=vertical, url=url)


def test_dates_named_in_several_languages() -> None:
    assert dates_named("meetup on 21 August 2025 at 7pm") == [date(2025, 8, 21)]
    assert dates_named("August 21st, 2025") == [date(2025, 8, 21)]
    assert dates_named("el 21 de agosto de 2025") == [date(2025, 8, 21)]
    assert dates_named("Встреча проходила 21 августа 2025 года") == [date(2025, 8, 21)]
    assert dates_named("зустріч 21 серпня 2025") == [date(2025, 8, 21)]
    assert dates_named("2025-08-21 and 20.11.2025") == [date(2025, 8, 21), date(2025, 11, 20)]
    assert dates_named("el 21 de noviembre del 2026") == [date(2026, 11, 21)]
    assert dates_named("on 21-08-2025, 03/04/2026") == [date(2025, 8, 21), date(2026, 4, 3)]  # day first
    assert dates_named("in August 2025, founded 2019, 31 February 2025") == []


def test_event_page_by_host_path_or_summary() -> None:
    assert event_page("https://wegottickets.com/event/1", None)
    assert event_page("https://www.eventbrite.es/e/x-tickets-123", None)
    assert not event_page("https://www.eventbrite.es/o/club-de-inversores-123", None)  # an organiser's page
    assert not event_page("https://www.eventbrite.es/d/spain--malaga/events/", None)
    assert event_page("https://lu.ma/ab12cd34", None) and not event_page("https://lu.ma/discover", None)
    assert not event_page("https://club.example.org/events/", None)  # a list of events, no event id
    assert event_page("https://club.example.org/events/42", None)
    assert event_page("https://club.example.org/x", {"summary": "Ticket page for the club meetup"})
    assert not event_page(CLUB, {"summary": "Monthly investor club in Sevilla"})


def test_past_event_needs_an_event_page_and_only_past_dates() -> None:
    assert past_event(TICKETS, {"summary": "meetup on 21 August 2025"}, TODAY)
    assert not past_event(TICKETS, {"summary": "meetup on 21 October 2026"}, TODAY)
    assert not past_event(TICKETS, {"summary": f"meetup on {TODAY:%Y-%m-%d}"}, TODAY)  # today is not past
    assert not past_event(TICKETS, {"summary": "meetups on 21 August 2025 and 21 October 2026"}, TODAY)
    assert not past_event(TICKETS, {"summary": "monthly meetup, next date to be announced"}, TODAY)
    assert not past_event(CLUB, {"summary": "club founded on 21 August 2025"}, TODAY)  # the club's own page
    # review Z1: a date we cannot read whole keeps the finding (in doubt, never excluded)
    later = date(2026, 11, 14)
    assert not past_event(TICKETS, {"summary": "Evento el 21 de noviembre del 2026 (publicado el 2 de octubre de 2026)"},
                          later)
    assert not past_event(TICKETS, {"summary": "Meetup on 21 August; tickets on sale since 1 June 2025"}, later)
    assert not past_event(TICKETS, {"summary": "Meetup in noviembre del 2026; page of 1 June 2025"}, later)
    assert not past_event(TICKETS, {"summary": "Meetup 21/08 at 7pm, listed 2025-06-01"}, later)
    assert past_event(TICKETS, {"summary": "Meetup on 21-08-2025"}, later)


def test_the_reason_is_named_in_the_report() -> None:
    assert reason_category("past_event") == "past_event"
    assert REASONS["past_event"] == METRIC_REASONS["past_event"] == "прошедшее событие"


def test_flag_is_off_by_default_and_reaches_the_runner(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u@localhost/db")
    monkeypatch.delenv("INVESTOR_PAST_EVENT_EXCLUDED", raising=False)
    assert RunnerConfig().investor_past_event_excluded is False
    assert CampaignRunnerSettings(_env_file=None).runner_config().investor_past_event_excluded is False
    monkeypatch.setenv("INVESTOR_PAST_EVENT_EXCLUDED", "true")
    assert CampaignRunnerSettings(_env_file=None).runner_config().investor_past_event_excluded is True


def _four(store: MemoryRunStore, cid: str) -> None:
    page(store, cid, "past", TICKETS, "Ticket page for a networking meetup on 21 August 2025.")
    page(store, cid, "next", "https://tickets.example.net/event/1002", "Networking meetup on 21 October 2026.")
    page(store, cid, "nodate", "https://tickets.example.net/event/1003", "Networking meetup, date to be confirmed.")
    page(store, cid, "club", CLUB, "Investor club; its first meetup was on 21 August 2025.")


async def test_on_a_past_event_page_is_excluded_without_the_ai_check() -> None:
    store, _messenger, judge, runner, cid = await setup(investor_past_event_excluded=True)
    _four(store, cid)
    await runner.step(cid)
    assert store.buckets["past"][0] == "excluded" and store.whys["past"] == "past_event"
    assert set(store.streamed[cid]) == {"next", "nodate", "club"}
    assert judge.calls == 3  # the past event never reached the model


async def test_off_a_past_event_page_is_sent_as_before() -> None:
    store, _messenger, judge, runner, cid = await setup()
    _four(store, cid)
    await runner.step(cid)
    assert set(store.streamed[cid]) == {"past", "next", "nodate", "club"} and judge.calls == 4


async def test_on_investor_findings_of_a_both_search_are_excluded_too() -> None:
    store, _messenger, judge, runner, cid = await setup(vertical="both", investor_past_event_excluded=True)
    page(store, cid, "past", TICKETS, "Ticket page for a networking meetup on 21 August 2025.")
    await runner.step(cid)
    assert store.buckets["past"][0] == "excluded" and store.whys["past"] == "past_event" and judge.calls == 0
