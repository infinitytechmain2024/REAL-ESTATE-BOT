"""Campaign Facebook discovery: pure scoring/activity and the bounded run, with fakes."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from bot.campaign import MemoryCampaignStore, plan_campaign
from bot.campaign.discovery import (
    DiscoveryRefused,
    FacebookDiscovery,
    MemoryDiscoveryStore,
    canonical_group,
    card_activity,
    plan_seeds,
    score_relevance,
    search_url,
    visit_activity,
)
from bot.campaign.models import CampaignLimits
from bot.facebook_collector.browser import BrowserLease
from bot.facebook_collector.models import ChallengeDetected, CollectedPost, GroupRead, GroupState

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
RENT = "Найди квартиры в аренду в Мадриде до 1200 евро"


def plan(text: str = RENT, max_groups: int | None = None, window_size: int = 20):
    base = plan_campaign(text)
    if max_groups is None:
        return base
    return base.model_copy(update={"limits": CampaignLimits(max_groups=max_groups, window_size=window_size)})


# --- pure: URLs ------------------------------------------------------------------


def test_only_a_groups_own_url_is_a_group() -> None:
    assert canonical_group("https://m.facebook.com/groups/Pisos.Madrid/?ref=search") == (
        "pisos.madrid", "https://www.facebook.com/groups/pisos.madrid/")
    assert canonical_group("https://www.facebook.com/groups/123456") == ("123456", "https://www.facebook.com/groups/123456/")
    for bad in (
        "https://www.facebook.com/groups/123/posts/456/", "https://www.facebook.com/groups/123/permalink/9/",
        "https://www.facebook.com/groups/123/members/", "https://www.facebook.com/groups/feed/",
        "https://www.facebook.com/groups/discover/", "https://example.com/groups/pisos/",
        "https://user:pw@www.facebook.com/groups/pisos/", "https://www.facebook.com/pisosmadrid/",
        "javascript:alert(1)", None, 42,
    ):
        assert canonical_group(bad) is None, bad


def test_search_url_is_a_facebook_group_search_with_the_seed_encoded() -> None:
    url = search_url("аренда квартир Мадрид & co")
    parts = urlsplit(url)
    assert (parts.scheme, parts.hostname, parts.path) == ("https", "www.facebook.com", "/search/groups/")
    assert parse_qs(parts.query) == {"q": ["аренда квартир Мадрид & co"]}


def test_seeds_are_round_robin_across_languages_and_capped() -> None:
    seeds = plan_seeds(plan(), 12)
    assert len(seeds) == 12
    assert [lang for lang, _ in seeds[:4]] == ["es", "en", "ru", "uk"]
    assert {lang for lang, _ in seeds} == {"es", "en", "ru", "uk"}
    assert len(plan_seeds(plan(), 3)) == 3


# --- pure: relevance -----------------------------------------------------------------


@pytest.mark.parametrize(("name", "card"), [
    ("Pisos en alquiler Madrid", "Público · 12 mil miembros"),
    ("Madrid apartments for rent", "Public group · 8K members"),
    ("Аренда квартир в Мадриде", "Публичная группа · 3 тыс. участников"),
    ("Оренда житла у Мадриді", "Публічна група · 2 тис. учасників"),
])
def test_relevant_groups_in_all_four_languages(name: str, card: str) -> None:
    result = score_relevance(name, card, plan())
    assert result.relevant and result.score >= 0.6, result


@pytest.mark.parametrize(("name", "card", "reason"), [
    ("Pisos en alquiler Barcelona", "Público", "no_location"),
    ("Apartments for rent", "Public group", "no_location"),
    ("Madrid friends", "Public · 5K members", "no_vertical_match"),
    ("Trabajo y empleo Madrid", "ofertas de alquiler y vacantes", "off_topic"),
    ("Madrid dating", "rent a date", "off_topic"),
    ("Crypto signals Madrid", "investment", "off_topic"),
    ("Venta de coches Madrid", "sale", "off_topic"),
    ("Работа в Мадриде", "вакансии, жильё", "off_topic"),
    ("Робота Мадрид", "вакансії житло", "off_topic"),
])
def test_irrelevant_groups_are_rejected_with_a_reason(name: str, card: str, reason: str) -> None:
    result = score_relevance(name, card, plan())
    assert not result.relevant and result.reason == reason, result
    assert result.score < 0.6


def test_location_may_come_from_the_group_slug_and_vertical_follows_the_plan() -> None:
    assert score_relevance("Pisos alquiler", "", plan(), group_key="pisosmadrid").relevant
    investors = plan("Найди инвесторов для стартапа в Мадриде")
    assert investors.vertical == "investors"
    assert score_relevance("Madrid Startups & Investors", "", investors).relevant
    assert score_relevance("Pisos alquiler Madrid", "", investors).reason == "no_vertical_match"
    both = plan("Инвесторы в недвижимость и аренда квартир в Мадриде")
    assert both.vertical == "both"
    assert score_relevance("Inversores Madrid", "", both).relevant
    assert score_relevance("Pisos Madrid", "", both).relevant


def test_more_vertical_evidence_scores_higher() -> None:
    weak = score_relevance("Madrid", "pisos", plan())
    strong = score_relevance("Alquiler pisos habitaciones Madrid", "", plan())
    assert strong.score > weak.score


# --- pure: activity -----------------------------------------------------------------


@pytest.mark.parametrize(("card", "expected", "per"), [
    ("Public · 12K members · 10+ posts a day", "ACTIVE", "day"),
    ("Public · 3 posts a week", "ACTIVE", "week"),
    ("Público · 12 mil miembros · 5 publicaciones al día", "ACTIVE", "day"),
    ("Público · 2 publicaciones a la semana", "ACTIVE", "week"),
    ("Публичная группа · 3,4 тыс. участников · 10 публикаций в день", "ACTIVE", "day"),
    ("Публичная группа · 2 публикации в неделю", "ACTIVE", "week"),
    ("Публічна група · 5 дописів на день", "ACTIVE", "day"),
    ("Публічна група · 1 допис на тиждень", "ACTIVE", "week"),
    ("Public · 1 post a month", "UNKNOWN", "month"),
    ("Public · 0 posts a day", "DEAD", "day"),
    ("Public · 2 posts a year", "DEAD", "year"),
])
def test_card_posting_rates(card: str, expected: str, per: str) -> None:
    activity, evidence = card_activity(card)
    assert activity == expected and evidence["per"] == per, (activity, evidence)


@pytest.mark.parametrize("card", [
    "Public · Last active a year ago", "Público · última actividad hace un año",
    "Последняя активность: год назад", "Остання активність: 2 роки тому",
])
def test_cards_last_active_a_year_ago_are_dead(card: str) -> None:
    assert card_activity(card)[0] == "DEAD"


def test_a_card_without_rate_is_unknown_and_members_are_counted() -> None:
    activity, evidence = card_activity("Public · 12K members")
    assert activity == "UNKNOWN" and evidence == {"source": "card", "members": 12_000}
    assert card_activity("")[0] == "UNKNOWN" and card_activity(None)[0] == "UNKNOWN"


def _read(state: GroupState, *posts: tuple[str | None, str], text: str = "") -> GroupRead:
    return GroupRead(state, tuple(CollectedPost(f"id{i}", f"https://www.facebook.com/groups/g/posts/{i}/", body, at)
                                  for i, (at, body) in enumerate(posts)),
                     {"text": text}, {"articles": len(posts)})


def test_visit_activity_follows_the_newest_dated_post() -> None:
    iso = lambda days: (NOW - timedelta(days=days)).isoformat()  # noqa: E731
    assert visit_activity(_read(GroupState.ACTIVE, (iso(40), "old"), (iso(2), "new")), now=NOW)[0] == "ACTIVE"
    assert visit_activity(_read(GroupState.ACTIVE, (iso(30), "x")), now=NOW)[0] == "INACTIVE"
    assert visit_activity(_read(GroupState.ACTIVE, (iso(200), "x")), now=NOW)[0] == "DEAD"
    activity, evidence = visit_activity(_read(GroupState.ACTIVE, (None, "Ana\n3 h ·\nPiso en Madrid")), now=NOW)
    assert activity == "ACTIVE" and evidence["newest_age_days"] < 1
    assert visit_activity(_read(GroupState.ACTIVE, (None, "Анна\n5 дн.\nКвартира")), now=NOW)[0] == "ACTIVE"
    assert visit_activity(_read(GroupState.ACTIVE, (None, "no date here at all, just text")), now=NOW)[0] == "UNKNOWN"
    assert visit_activity(_read(GroupState.ACTIVE, (None, "undated"), text="10 posts a day"), now=NOW)[0] == "ACTIVE"
    assert visit_activity(_read(GroupState.INACTIVE), now=NOW)[0] == "INACTIVE"
    assert visit_activity(_read(GroupState.UNKNOWN), now=NOW)[0] == "UNKNOWN"


def test_inaccessible_is_never_dead() -> None:
    activity, evidence = visit_activity(_read(GroupState.INACCESSIBLE, text="Private group · a year ago"), now=NOW)
    assert activity == "INACCESSIBLE" and "text" not in evidence


# --- the run, with fakes --------------------------------------------------------------


def link(key: str, name: str, card: str = "") -> dict[str, str]:
    return {"url": f"https://www.facebook.com/groups/{key}/?__cft__=x", "name": name, "card": card}


class FakeBrowser:
    def __init__(self, results: dict[str, list[dict[str, str]]] | None = None) -> None:
        self.results = results or {}
        self.navigations: list[str] = []
        self.acquired: list[tuple[str, str, str]] = []
        self.released: list[str] = []
        self.challenge_on: str | None = None
        self.fail_on: set[str] = set()

    async def acquire(self, profile_id: str, profile_name: str, persisted_state: str, *, platform: str = "facebook") -> BrowserLease:
        self.acquired.append((profile_id, profile_name, persisted_state))
        return BrowserLease(profile_id, "token")

    async def snapshot(self, lease: BrowserLease, url: str, timeout_ms: int) -> dict[str, Any]:
        self.navigations.append(url)
        seed = parse_qs(urlsplit(url).query)["q"][0]
        if seed in self.fail_on:
            raise OSError("browser service unreachable")
        if seed == self.challenge_on:
            return {"url": "https://www.facebook.com/checkpoint/123/", "title": "Security check", "text": "", "posts": []}
        return {"url": url, "title": "Facebook", "text": "", "posts": [], "group_links": self.results.get(seed, [])}

    async def release(self, lease: BrowserLease, next_state: str = "READY") -> None:
        self.released.append(next_state)


class FakeReader:
    def __init__(self, reads: dict[str, GroupRead] | None = None, challenge_on: str | None = None) -> None:
        self.reads, self.challenge_on = reads or {}, challenge_on
        self.visited: list[str] = []

    async def read(self, lease: BrowserLease, group_url: str) -> GroupRead:
        self.visited.append(group_url)
        if group_url == self.challenge_on:
            raise ChallengeDetected("facebook_url:/checkpoint", {"text": "secret page"})
        return self.reads.get(group_url, _read(GroupState.UNKNOWN))


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


async def setup(the_plan=None, **kwargs: Any):
    campaigns = MemoryCampaignStore()
    cid = await campaigns.create(the_plan or plan(), chat_id=1, requested_by=2, source_text=RENT, actor="telegram:2")
    store = MemoryDiscoveryStore()
    browser = kwargs.pop("browser", FakeBrowser())
    reader = kwargs.pop("reader", FakeReader())
    sleeps = Sleeps()
    discovery = FacebookDiscovery(campaigns, store, browser, reader, sleep=sleeps, now=lambda: NOW, **kwargs)
    return SimpleNamespace(campaigns=campaigns, cid=cid, store=store, browser=browser, reader=reader,
                           sleeps=sleeps, discovery=discovery)


def url_of(key: str) -> str:
    return f"https://www.facebook.com/groups/{key}/"


async def test_only_active_relevant_groups_are_queued_best_first_and_capped() -> None:
    the_plan = plan(max_groups=6, window_size=4)
    seeds = plan_seeds(the_plan, 12)
    browser = FakeBrowser({
        seeds[0][1]: [
            link("pisosmadrid", "Pisos alquiler habitaciones Madrid", "Público · 10+ publicaciones al día"),
            link("rentmadrid", "Madrid rent", "Public · 3 posts a week"),
            link("madridflats", "Madrid flats for rent", "Public · 5 posts a day"),
            *(link(f"low{i}", "Madrid", "Public · 5 posts a day · pisos") for i in range(1, 5)),
            link("123", "Pisos Barcelona", "10 posts a day"),  # no location
            link("jobs", "Trabajo Madrid alquiler", "10 posts a day"),  # off topic
            link("dead", "Alquiler Madrid", "Last active a year ago"),
            {"url": "https://www.facebook.com/groups/x/posts/1/", "name": "a post", "card": ""},
        ],
        # the same group again from another seed is not a second candidate
        seeds[1][1]: [link("pisosmadrid", "Pisos Madrid", "10 posts a day"),
                      link("privado", "Alquiler Madrid privado", "Grupo privado"),
                      link("maybe", "Alquiler Madrid", "Público")],
    })
    reader = FakeReader({url_of("privado"): _read(GroupState.INACCESSIBLE),
                         url_of("maybe"): _read(GroupState.ACTIVE, ((NOW - timedelta(days=40)).isoformat(), "x"))})
    h = await setup(the_plan, browser=browser, reader=reader, max_results_per_seed=20)

    report = await h.discovery.run(h.cid)

    groups = {g.group_key: g for g in await h.store.groups(h.cid)}
    assert set(groups) == {"pisosmadrid", "rentmadrid", "madridflats", "low1", "low2", "low3", "low4",
                           "123", "jobs", "dead", "privado", "maybe"}  # 12 = the 2 x max_groups candidate cap
    queued = sorted((g for g in groups.values() if g.state == "queued"), key=lambda g: (g.window_no, -g.relevance_score, g.group_key))
    # best six of seven ACTIVE, in windows of four
    assert [(g.group_key, g.window_no) for g in queued] == [
        ("madridflats", 1), ("pisosmadrid", 1), ("rentmadrid", 1), ("low1", 1), ("low2", 2), ("low3", 2)]
    assert all(g.activity == "ACTIVE" for g in queued)
    assert (groups["low4"].state, groups["low4"].reject_reason) == ("skipped", "max_groups_reached")
    assert (groups["123"].state, groups["123"].reject_reason) == ("rejected", "no_location")
    assert (groups["jobs"].state, groups["jobs"].reject_reason) == ("rejected", "off_topic")
    assert (groups["dead"].state, groups["dead"].activity, groups["dead"].reject_reason) == ("rejected", "DEAD", "dead")
    assert (groups["privado"].state, groups["privado"].activity) == ("discovered", "INACCESSIBLE")
    assert (groups["maybe"].state, groups["maybe"].reject_reason) == ("rejected", "inactive")
    assert groups["maybe"].activity_evidence["visit"]["newest_age_days"] == 40
    assert "text" not in str(groups["privado"].activity_evidence)  # counts only, no content
    # only relevant UNKNOWN groups are visited
    assert h.reader.visited == [url_of("maybe"), url_of("privado")]

    assert report.state == "running" and report.queued == 6 and report.challenge is None
    assert (await h.campaigns.get(h.cid)).state == "running"
    assert h.browser.released == ["READY"] and h.store.profile.state == "ready"
    assert h.browser.acquired == [("profile-1", "facebook-main", "ready")]
    assert all(actor == "campaign:discovery" for actor, _, _ in h.store.writes)


async def test_bounds_on_seeds_results_candidates_visits_and_pacing() -> None:
    the_plan = plan(max_groups=3)
    seeds = plan_seeds(the_plan, 24)
    many = {seed: [link(f"g{i}x{j}", f"Alquiler Madrid {i} {j}", "Público") for j in range(30)]
            for i, (_, seed) in enumerate(seeds)}
    browser = FakeBrowser(many)
    h = await setup(
        the_plan, browser=browser, max_seeds=5, max_results_per_seed=4, max_visits=2)

    report = await h.discovery.run(h.cid)

    searches = [u for u in h.browser.navigations if "/search/groups/" in u]
    assert len(searches) == 2  # 4 + 2 candidates reach the cap of 2 x max_groups = 6
    groups = await h.store.groups(h.cid)
    assert len(groups) == 6
    assert len(h.reader.visited) == 2
    navigations = len(h.browser.navigations) + len(h.reader.visited)
    assert len(h.sleeps.calls) == navigations - 1 and all(4.0 <= s <= 9.0 for s in h.sleeps.calls)
    assert report.state == "completed" and report.stop_reason == "no_active_groups"
    assert {g.reject_reason for g in groups} == {"activity_unknown"}



async def test_the_seed_cap_bounds_searches() -> None:
    h = await setup(max_seeds=3)
    report = await h.discovery.run(h.cid)
    assert len(h.browser.navigations) == 3 and report.seeds_total == 3 and report.seeds_done == 3


async def test_time_budget_stops_the_search_and_still_finalizes() -> None:
    ticks = iter([0.0, 0.0, 5.0, 50.0, 500.0, 5000.0] + [9999.0] * 50)
    h = await setup(
        clock=lambda: next(ticks), time_budget_seconds=100)
    report = await h.discovery.run(h.cid)
    assert report.budget_exhausted and len(h.browser.navigations) == 3
    assert report.state == "completed"


async def test_a_search_challenge_stops_everything_and_resume_continues() -> None:
    seeds = plan_seeds(plan(), 12)
    browser = FakeBrowser({
        seeds[0][1]: [link("pisosmadrid", "Pisos alquiler Madrid", "10 posts a day")],
        seeds[2][1]: [link("arenda", "Аренда квартир Мадрид", "5 публикаций в день")],
    })
    browser.challenge_on = seeds[1][1]
    h = await setup(browser=browser)

    report = await h.discovery.run(h.cid)

    assert len(h.browser.navigations) == 2  # stopped at once: no further search, no visit
    assert h.reader.visited == []
    assert h.browser.released == ["VERIFICATION_REQUIRED"]
    assert h.store.profile.state == "human_verification_required"
    campaign = await h.campaigns.get(h.cid)
    assert campaign.state == "paused_verification" and campaign.stop_reason.startswith("facebook_challenge:")
    assert report.challenge and report.state == "paused_verification"
    assert [g.group_key for g in await h.store.groups(h.cid)] == ["pisosmadrid"]  # partial results kept
    assert not any(g.state == "queued" for g in await h.store.groups(h.cid))

    # Refused while the profile waits for a human; nothing is touched.
    with pytest.raises(DiscoveryRefused, match="facebook_profile_not_ready"):
        await h.discovery.run(h.cid)
    assert len(h.browser.navigations) == 2

    # A human cleared the checkpoint; the run resumes after the done seed.
    h.store.profile = replace(h.store.profile, state="ready")
    h.browser.challenge_on = None
    before = len(h.browser.navigations)
    report = await h.discovery.run(h.cid)
    resumed = h.browser.navigations[before:]
    assert search_url(seeds[0][1]) not in resumed and resumed[0] == search_url(seeds[1][1])
    keys = [g.group_key for g in await h.store.groups(h.cid)]
    assert sorted(keys) == ["arenda", "pisosmadrid"] and len(keys) == len(set(keys))
    assert report.state == "running" and report.queued == 2
    assert h.browser.released[-1] == "READY" and h.store.profile.state == "ready"


async def test_a_challenge_on_a_group_visit_pauses_and_the_visit_is_redone_on_resume() -> None:
    seeds = plan_seeds(plan(), 12)
    browser = FakeBrowser({seeds[0][1]: [link("maybe", "Alquiler Madrid", "Público"),
                                         link("ok", "Pisos Madrid", "10 posts a day")]})
    reader = FakeReader(challenge_on=url_of("maybe"))
    h = await setup(browser=browser, reader=reader)
    report = await h.discovery.run(h.cid)
    assert report.state == "paused_verification" and h.browser.released == ["VERIFICATION_REQUIRED"]
    assert h.store.profile.state == "human_verification_required"
    assert "secret page" not in str(await h.store.groups(h.cid))

    h.store.profile = replace(h.store.profile, state="ready")
    h.reader.challenge_on = None
    h.reader.reads[url_of("maybe")] = _read(GroupState.ACTIVE, (NOW.isoformat(), "x"))
    searches_before = len(h.browser.navigations)
    report = await h.discovery.run(h.cid)
    assert len(h.browser.navigations) == searches_before  # every seed was done; only the visit is redone
    assert h.reader.visited == [url_of("maybe"), url_of("maybe")]
    assert report.state == "running" and report.queued == 2


async def test_rerunning_a_running_campaign_is_refused_and_groups_are_not_duplicated() -> None:
    seeds = plan_seeds(plan(), 12)
    browser = FakeBrowser({s: [link("pisosmadrid", "Pisos Madrid", "10 posts a day")] for _, s in seeds})
    h = await setup(browser=browser)
    await h.discovery.run(h.cid)
    assert len(await h.store.groups(h.cid)) == 1
    with pytest.raises(DiscoveryRefused, match="campaign_running"):
        await h.discovery.run(h.cid)


async def test_refusals_leave_everything_untouched() -> None:
    h = await setup()
    h.store.profile = replace(h.store.profile, state="human_verification_required")
    with pytest.raises(DiscoveryRefused, match="facebook_profile_not_ready"):
        await h.discovery.run(h.cid)
    assert h.browser.acquired == [] and (await h.campaigns.get(h.cid)).state == "planned"
    with pytest.raises(DiscoveryRefused, match="campaign_not_found"):
        await h.discovery.run("nope")
    await h.campaigns.set_state(h.cid, "cancelled", "test")
    h.store.profile = replace(h.store.profile, state="ready")
    with pytest.raises(DiscoveryRefused, match="campaign_cancelled"):
        await h.discovery.run(h.cid)
    assert h.store.profile.state == "ready"


async def test_an_operator_cancel_stops_discovery_without_queueing() -> None:
    h = await setup()

    async def cancel_after_first(seconds: float) -> None:
        await h.campaigns.set_state(h.cid, "cancelled", "telegram:2")

    h.discovery.sleep = cancel_after_first
    report = await h.discovery.run(h.cid)
    assert len(h.browser.navigations) == 1 and report.state == "cancelled"
    assert h.browser.released == ["READY"] and h.store.profile.state == "ready"


async def test_repeated_search_failures_fail_the_campaign_and_free_the_profile() -> None:
    browser = FakeBrowser()
    browser.fail_on = {seed for _, seed in plan_seeds(plan(), 12)}
    h = await setup(browser=browser, max_errors=2)
    with pytest.raises(Exception, match="repeated search failures"):
        await h.discovery.run(h.cid)
    campaign = await h.campaigns.get(h.cid)
    assert campaign.state == "failed" and campaign.stop_reason == "discovery_error:DiscoveryError"
    assert h.browser.released == ["READY"] and h.store.profile.state == "ready"


async def test_unsafe_limits_are_rejected() -> None:
    campaigns, store = MemoryCampaignStore(), MemoryDiscoveryStore()
    for bad in ({"max_seeds": 0}, {"max_seeds": 25}, {"max_results_per_seed": 41}, {"max_visits": 21},
                {"pause_min_seconds": 5, "pause_max_seconds": 1}, {"search_timeout_ms": 120_000}):
        with pytest.raises(ValueError):
            FacebookDiscovery(campaigns, store, FakeBrowser(), FakeReader(), **bad)


async def test_after_every_group_search_people_and_pages_become_judged_contacts() -> None:
    from bot.campaign.reach import Contact, Judged, MemoryReachStore, ReachWorker, contact_card
    from tests.test_investor_reach import FakeSearcher

    class Judge:
        model = "judge"

        async def judge(self, campaign, candidates):
            return [Judged("agency" if "Inmobiliaria" in c.title else "agent", True, 0.9, c.title, "Агентство")
                    for c in candidates]

    class PeopleBrowser(FakeBrowser):
        async def snapshot(self, lease: BrowserLease, url: str, timeout_ms: int) -> dict[str, Any]:
            if "/search/people/" in url or "/search/pages/" in url:
                self.navigations.append(url)
                return {"url": url, "title": "Facebook", "text": "", "posts": [], "group_links": [], "profile_links": [
                    {"url": "https://www.facebook.com/inmomadrid", "name": "Inmobiliaria Madrid",
                     "card": "Inmobiliaria Madrid · Agencia inmobiliaria · Madrid"},
                    {"url": "https://www.facebook.com/profile.php?id=100012345678", "name": "Ana López",
                     "card": "Agente inmobiliario en Madrid"},
                    {"url": "https://www.facebook.com/groups/x/", "name": "a group", "card": ""}]}
            return await super().snapshot(lease, url, timeout_ms)

    the_plan = plan()
    seeds = plan_seeds(the_plan, 12)
    browser = PeopleBrowser({seeds[0][1]: [link("pisosmadrid", "Pisos alquiler Madrid", "Público · 10 posts a day")]})
    reach_store = MemoryReachStore()
    h = await setup(the_plan, browser=browser, reach=ReachWorker(reach_store, FakeSearcher(), Judge()),
                    people_searches=2)
    await h.discovery.run(h.cid)
    kinds = ["groups" if "/search/groups/" in u else "people" for u in browser.navigations]
    assert kinds[-2:] == ["people", "people"] and set(kinds[:-2]) == {"groups"}, "every group search comes first"
    assert "/search/pages/" in browser.navigations[-2], "a property search asks for pages (agencies) first"
    urls = {s.candidate.url for s in reach_store.contacts.values()}
    assert urls == {"https://www.facebook.com/inmomadrid", "https://www.facebook.com/profile.php?id=100012345678"}
    stored = next(s for s in reach_store.contacts.values() if "inmomadrid" in s.candidate.url)
    card = contact_card(Contact(stored.candidate.url_key, stored.candidate.url, "facebook", "agency",
                                stored.judged.name))
    assert card.startswith("🏢 Агентство недвижимости · Facebook")
    assert reach_store.used == {}, "people searches are not the reach's own queries"
