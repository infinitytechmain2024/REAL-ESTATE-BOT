"""X through twitter-cli (Agent Reach's backend) for the reach, and LinkedIn people as reach contacts."""

from __future__ import annotations

import json
from typing import Any

import pytest

from bot.campaign.reach import MemoryReachStore, ReachCampaign, ReachConfig, ReachWorker
from bot.campaign.xsearch import (
    BrowserSession,
    EnvSession,
    FirstSession,
    TwitterCli,
    XUnavailable,
    parse_output,
    x_query,
)
from bot.web_search.searxng import SearchError, SearchHit
from tests.test_investor_reach import FakeSearcher
from tests.test_near_match import ButtonMessenger

TWEETS = {"ok": True, "schema_version": "1", "data": [
    {"id": "1840000000000000001", "text": "Busco inversores para un edificio en Madrid centro, rentabilidad 6%",
     "author": {"name": "Ana Inversiones", "screenName": "ana_inv"}, "isRetweet": False},
    {"id": "1840000000000000002", "text": "RT something", "author": {"name": "B", "screenName": "bbb"},
     "isRetweet": True},
    {"id": "not-a-number", "text": "x", "author": {"name": "C", "screenName": "ccc"}},
    {"id": "1840000000000000003", "text": "x", "author": {"name": "D", "screenName": "bad handle!"}},
]}
MADRID = ReachCampaign("c1", "Madrid", {"es": "Madrid", "en": "Madrid"}, ("es", "en"), "Инвесторы в Мадриде",
                       chat_id=42, requested_by=42)


def test_the_query_loses_its_site_part_and_the_output_becomes_one_hit_per_original_post() -> None:
    assert x_query("site:x.com inversor inmobiliario Madrid") == "inversor inmobiliario Madrid"
    assert x_query("site:twitter.com/foo real estate agent Madrid") == "real estate agent Madrid"
    [hit] = parse_output(json.dumps(TWEETS).encode())
    assert hit == SearchHit("https://x.com/ana_inv/status/1840000000000000001", "Ana Inversiones (@ana_inv) on X",
                            "Busco inversores para un edificio en Madrid centro, rentabilidad 6%")


@pytest.mark.parametrize(("payload", "error"), [
    ({"ok": False, "error": {"code": "not_authenticated", "message": "No Twitter cookies found."}}, XUnavailable),
    ({"ok": False, "error": {"code": "rate_limited"}}, SearchError),
    ("not json", SearchError),
])
def test_a_refused_session_asks_for_a_login_and_other_errors_are_search_errors(payload: Any, error: type) -> None:
    raw = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
    with pytest.raises(error):
        parse_output(raw)


class Session:
    def __init__(self, value: tuple[str, str] | None) -> None:
        self.value, self.dropped = value, 0

    async def get(self) -> tuple[str, str] | None:
        return self.value

    def invalidate(self) -> None:
        self.dropped += 1


async def test_the_cli_gets_a_fixed_argument_list_and_only_its_session_in_the_environment() -> None:
    calls: list[tuple[list[str], dict[str, str], float]] = []

    async def runner(args, env, timeout):
        calls.append((list(args), dict(env), timeout))
        return 0, json.dumps(TWEETS).encode()

    cli = TwitterCli(Session(("tok", "csrf")), binary="/opt/twitter-cli/bin/twitter", max_results=7,
                     proxy_url="http://proxy:1", runner=runner)
    hits = await cli.search("site:x.com busco piso Madrid; rm -rf /")
    [(args, env, timeout)] = calls
    assert args == ["/opt/twitter-cli/bin/twitter", "search", "busco piso Madrid; rm -rf /", "-t", "latest", "-n", "7",
                    "--json"], "one argv entry for the whole query: no shell ever reads it"
    assert env["TWITTER_AUTH_TOKEN"] == "tok" and env["TWITTER_CT0"] == "csrf" and env["TWITTER_PROXY"] == "http://proxy:1"
    assert "DATABASE_URL" not in env and "OPENROUTER_API_KEY" not in env and timeout == 60
    assert len(hits) == 1


async def test_no_session_and_a_refused_session_are_reported_and_the_refused_one_is_dropped() -> None:
    async def refused(args, env, timeout):
        return 1, json.dumps({"ok": False, "error": {"code": "not_authenticated"}}).encode()

    with pytest.raises(XUnavailable):
        await TwitterCli(Session(None), runner=refused).search("piso Madrid")
    session = Session(("old", "old"))
    with pytest.raises(XUnavailable):
        await TwitterCli(session, runner=refused).search("piso Madrid")
    assert session.dropped == 1


async def test_a_missing_cli_or_a_slow_one_is_a_search_error() -> None:
    async def missing(args, env, timeout):
        raise FileNotFoundError(args[0])

    async def slow(args, env, timeout):
        raise TimeoutError

    for runner, code in ((missing, "x_cli_missing"), (slow, "x_timeout")):
        with pytest.raises(SearchError) as caught:
            await TwitterCli(Session(("t", "c")), runner=runner).search("piso Madrid")
        assert caught.value.code == code


async def test_sessions_come_from_the_environment_first_then_the_live_window_profile() -> None:
    assert await EnvSession({"TWITTER_AUTH_TOKEN": "a", "TWITTER_CT0": "b"}).get() == ("a", "b")
    assert await EnvSession({"TWITTER_AUTH_TOKEN": "a"}).get() is None

    class Pool:
        async def fetchrow(self, sql: str, *args: Any) -> dict[str, str] | None:
            assert "platform = 'x' and state = 'ready'" in sql
            return {"id": "p-x", "profile_name": "x-main"}

    class Browser:
        def __init__(self) -> None:
            self.events: list[str] = []

        async def acquire(self, profile_id, name, state, *, platform="facebook"):
            self.events.append(f"acquire:{profile_id}:{platform}")
            return "lease"

        async def x_credentials(self, lease):
            self.events.append("credentials")
            return {"auth_token": "live-tok", "ct0": "live-csrf"}

        async def release(self, lease, next_state="READY"):
            self.events.append(f"release:{next_state}")

    browser = Browser()
    clock = [0.0]
    live = BrowserSession(Pool(), browser, cache_seconds=60, clock=lambda: clock[0])  # type: ignore[arg-type]
    first = FirstSession(EnvSession({}), live)
    assert await first.get() == ("live-tok", "live-csrf")
    assert await first.get() == ("live-tok", "live-csrf")
    assert browser.events == ["acquire:p-x:x", "credentials", "release:READY"], "kept in memory, the lease is freed"
    first.invalidate()
    await first.get()
    assert browser.events.count("credentials") == 2


async def test_the_reach_sends_x_queries_to_x_and_falls_back_to_the_search_engines_with_a_login_prompt() -> None:
    from bot.campaign.logins import LoginPrompts
    from bot.campaign.reach import ReachQuery

    store = MemoryReachStore([MADRID])
    searcher = FakeSearcher({"site:x.com inversor Madrid": [SearchHit("https://x.com/eng/status/5", "Eng", "")]})
    seen: list[str] = []

    class X:
        session: tuple[str, str] | None = ("t", "c")

        async def search(self, query: str, *, language: str | None = None) -> list[SearchHit]:
            seen.append(query)
            if X.session is None:
                raise XUnavailable("no_session")
            return parse_output(json.dumps(TWEETS).encode())

    messenger = ButtonMessenger()
    worker = ReachWorker(store, searcher, x=X(), logins=LoginPrompts(messenger, {1}), config=ReachConfig())
    query = ReachQuery("x", "es", "site:x.com inversor Madrid")
    await worker._run(MADRID, query)
    assert seen == ["site:x.com inversor Madrid"] and searcher.queries == [], "X answered itself"
    assert any(s.candidate.url == "https://x.com/ana_inv/status/1840000000000000001" for s in store.contacts.values())

    X.session = None
    await worker._run(MADRID, ReachQuery("x", "es", "site:x.com inversor Madrid"))
    assert searcher.queries == [("site:x.com inversor Madrid", "es")], "no X session: the search engines answer"
    assert messenger.asks and messenger.asks[0][2] == (("🔐 Войти в X (Twitter)", "login:go:x"),)
    assert any("X (Twitter) пока недоступен" in t for _, _, t in messenger.sent)


# --- LinkedIn people from the logged-in search become contacts ---------------------------------------


async def test_people_and_companies_of_a_linkedin_search_become_judged_contacts() -> None:
    from bot.campaign.reach import Judged
    from tests.test_social_search import LINKEDIN_PEOPLE, campaign, world

    class Judge:
        model = "judge"

        async def judge(self, campaign, candidates):
            return [Judged("investor", True, 0.9, "Juan", "Инвестор в недвижимость") for _ in candidates]

    clock, campaigns, store, _browser, _, worker = world({}, platforms=("linkedin",), default=LINKEDIN_PEOPLE)
    reach_store = MemoryReachStore()
    worker.reach = ReachWorker(reach_store, FakeSearcher(), Judge())
    cid = await campaign(campaigns, store, "инвесторы в недвижимость в Мадриде", vertical="investors")
    store.add_profile("linkedin")
    for _ in range(4):
        await worker.tick()
        clock.advance(3600)
    urls = {s.candidate.url for s in reach_store.contacts.values()}
    assert urls == {"https://www.linkedin.com/in/juan-perez-inversor/", "https://www.linkedin.com/company/fondo-madrid/"}
    assert all(s.campaign_id == cid for s in reach_store.contacts.values())
    assert reach_store.used.get(cid) is None, "a LinkedIn page is not one of the reach's own queries"


async def test_with_the_store_the_need_is_recorded_and_the_owners_get_no_button_from_the_runner() -> None:
    from bot.campaign.logins import LoginPrompts

    class Requests:
        def __init__(self) -> None:
            self.needed: list[tuple[str, int]] = []

        async def need(self, platform: str, searches: int) -> None:
            self.needed.append((platform, searches))

    requests, messenger = Requests(), ButtonMessenger()
    prompts = LoginPrompts(messenger, {1}, requests=requests)
    await prompts.need("linkedin", [MADRID])
    assert requests.needed == [("linkedin", 1)] and messenger.asks == [], "the control plane sends the link"
    assert [chat for chat, _, _ in messenger.sent] == [42], "the requester is still told once"


async def test_no_facebook_profile_records_a_facebook_login_need() -> None:
    from bot.campaign import MemoryCampaignStore, plan_campaign
    from bot.campaign.discovery import DiscoveryRefused
    from bot.campaign.logins import LoginPrompts
    from bot.campaign.runner import CampaignRunner
    from bot.campaign.runs import MemoryRunStore

    class Refusing:
        async def run(self, campaign_id: str) -> None:
            raise DiscoveryRefused("facebook_profile_not_ready")

    class Requests:
        needed: list[str]

        def __init__(self) -> None:
            self.needed = []

        async def need(self, platform: str, searches: int) -> None:
            self.needed.append(platform)

    campaigns = MemoryCampaignStore()
    requests = Requests()
    runner = CampaignRunner(campaigns, MemoryRunStore(campaigns), ButtonMessenger(), Refusing(),  # type: ignore[arg-type]
                            logins=LoginPrompts(ButtonMessenger(), {7}, requests=requests))
    cid = await campaigns.create(plan_campaign("квартиры в аренду в Мадриде"), chat_id=7, requested_by=7,
                                 source_text="x", actor="t")
    await runner.step(cid)
    assert requests.needed == ["facebook"]
