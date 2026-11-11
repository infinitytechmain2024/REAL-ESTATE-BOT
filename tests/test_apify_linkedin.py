"""P1.4: LinkedIn people and companies through Apify without cookies, against a mocked Apify API.

No live calls: every answer comes from ``httpx.MockTransport``. The dataset rows are synthetic, shaped by the
actors' schemas as recorded in ``docs/v3/ACTORS.md`` (``harvestapi/linkedin-profile-search``,
``harvestapi/linkedin-company-search``); no real person, phone or address. A live sample is P1.7.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from bot.acquisition import apify_linkedin as li
from bot.acquisition.apify_client import ApifyHttp
from bot.acquisition.apify_source import MemorySourceRunStore
from bot.campaign.reach import Judged, MemoryReachStore, ReachCampaign
from bot.campaign.settings import CampaignRunnerSettings
from bot.utils import costs

TOKEN = "apify_api_SECRETtoken123"
CAMPAIGN = ReachCampaign("00000000-0000-0000-0000-000000000001", "Madrid", {}, ("es", "en"))
CONFIG = li.LinkedInConfig(build="1.2.3")
SEARCH = li.LinkedInSearch("real estate investor", ("Madrid",), function_ids=(li.REAL_ESTATE_FUNCTION_ID,))


def person(n: int = 1, **kw: Any) -> dict[str, Any]:
    return {"id": f"ACoAA{n}", "publicIdentifier": f"persona-ejemplo-{n}",
            "linkedinUrl": f"https://www.linkedin.com/in/persona-ejemplo-{n}",
            "firstName": "Persona", "lastName": f"Ejemplo {n}", "headline": "Real estate investor",
            "location": {"linkedinText": "Madrid, Comunidad de Madrid, España",
                         "parsed": {"city": "Madrid", "country": "Spain", "countryCode": "ES"}},
            "currentPosition": [{"companyName": "Ejemplo Capital", "position": "Socio",
                                 "companyLinkedinUrl": "https://www.linkedin.com/company/ejemplo-capital"}],
            "about": "Texto largo del perfil", "emails": [], "followerCount": 10,
            "_meta": {"pagination": {"totalElements": 1, "totalPages": 1}}, **kw}


def company(n: int = 1, **kw: Any) -> dict[str, Any]:
    return {"id": f"10{n}", "name": f"Ejemplo Inmuebles {n}", "universalName": f"ejemplo-inmuebles-{n}",
            "linkedinUrl": f"https://www.linkedin.com/company/ejemplo-inmuebles-{n}/",
            "website": "https://www.ejemplo-inmuebles.example", "description": "Descripción larga",
            "tagline": "Lema", "industry": "Real Estate", "industries": [{"name": "Real Estate"}],
            "employeeCount": 12, "phone": {"number": "+34 900 000 000"},
            "location": {"linkedinText": "Madrid, España"},
            "locations": [{"headquarter": False, "city": "Sevilla"},
                          {"headquarter": True, "city": "Madrid", "country": "ES", "line1": "Calle Ejemplo 1",
                           "postalCode": "28000", "parsed": {"city": "Madrid", "country": "Spain"}}],
            "_meta": {"pagination": {"totalElements": 1, "totalPages": 1}}, **kw}


class Api:
    def __init__(self, dataset: list[Any] | None = None, *, launch_status: int = 201) -> None:
        self.dataset, self.launch_status = dataset or [], launch_status
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/runs"):
            if self.launch_status != 201:
                return httpx.Response(self.launch_status, json={"error": {"type": "rate-limit-exceeded"}})
            return httpx.Response(201, json={"data": {"id": "RUN1", "defaultDatasetId": "DS1", "status": "READY"}})
        if "/actor-runs/RUN1" in path:
            return httpx.Response(200, json={"data": {"id": "RUN1", "status": "SUCCEEDED", "defaultDatasetId": "DS1",
                                                      "usageTotalUsd": 0.1}})
        if "/datasets/DS1/items" in path:
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            return httpx.Response(200, json=self.dataset[offset:offset + limit])
        return httpx.Response(404)

    def launches(self) -> list[httpx.Request]:
        return [r for r in self.calls if r.method == "POST" and r.url.path.endswith("/runs")]


def http(api: Api) -> ApifyHttp:
    async def no_sleep(_: float) -> None:
        return None

    return ApifyHttp(TOKEN, client=httpx.AsyncClient(transport=httpx.MockTransport(api)), sleep=no_sleep,
                     jitter=lambda: 0, retries=1)


class FakeJudge:
    model = "test/judge"

    def __init__(self, kind: str = "investor") -> None:
        self.kind, self.seen = kind, []

    async def judge(self, campaign, candidates):
        self.seen.append(list(candidates))
        return [Judged(self.kind, True, 0.9, "nombre del juez", "Кратко") for _ in candidates]  # type: ignore[arg-type]

    async def queries(self, campaign, count):
        return []


@pytest.fixture
def ledger():
    sink = costs.MemoryLedger()
    costs.install(sink, budget_usd=5)
    try:
        yield sink
    finally:
        costs.install(None)


def settings(**env: str) -> CampaignRunnerSettings:
    return CampaignRunnerSettings(_env_file=None, DATABASE_URL="postgresql://x", TELEGRAM_TOKEN="t", **env)


_COOKIE_LIKE = re.compile(r"cookie|session|token|password|secret|auth|credential|login|li_at|header|proxy|mongo"
                          r"|dedup|segmentation|email", re.I)


def keys_of(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [k for k, v in value.items() for k in (k, *keys_of(v))]
    if isinstance(value, list):
        return [k for v in value for k in keys_of(v)]
    return []


# --- the launch body --------------------------------------------------------------------------------------------


async def test_people_launch_short_white_list_cap_and_build(ledger) -> None:
    api, store = Api([person()]), MemoryReachStore()
    source = li.ApifyLinkedInPeople(MemorySourceRunStore(), http(api), CONFIG)
    outcome = await source.run(CAMPAIGN, SEARCH, store, FakeJudge())
    assert outcome.status == "succeeded"
    (launch,) = api.launches()
    assert launch.url.path.endswith("/acts/harvestapi~linkedin-profile-search/runs")
    body = json.loads(launch.content)
    assert body == {"profileScraperMode": "Short", "searchQuery": "real estate investor", "locations": ["Madrid"],
                    "functionIds": ["23"], "takePages": 1, "maxItems": 25}
    assert set(body) <= li.PEOPLE_INPUT_KEYS
    params = dict(launch.url.params)
    assert params["maxTotalChargeUsd"] == "0.15" and params["build"] == "1.2.3" and params["maxItems"] == "25"
    # The whole request: no forbidden, cookie-, session- or e-mail-like key anywhere, the token only in the header.
    scanned = [*keys_of(body), *params]
    assert not [k for k in scanned if _COOKIE_LIKE.search(k) or k in li.FORBIDDEN_INPUT_KEYS]
    assert TOKEN not in str(launch.url) and TOKEN not in launch.content.decode()


async def test_companies_launch_short_white_list(ledger) -> None:
    api = Api([company()])
    source = li.ApifyLinkedInCompanies(MemorySourceRunStore(), http(api), CONFIG)
    await source.run(CAMPAIGN, li.LinkedInSearch("inmobiliaria", ("Madrid",), industry_ids=("44",),
                                                 function_ids=("23",)), MemoryReachStore(), FakeJudge("agency"))
    (launch,) = api.launches()
    assert launch.url.path.endswith("/acts/harvestapi~linkedin-company-search/runs")
    body = json.loads(launch.content)
    # functionIds is a people filter: never sent to the company search.
    assert body == {"scraperMode": "short", "searchQuery": "inmobiliaria", "locations": ["Madrid"],
                    "industryIds": ["44"], "takePages": 1, "maxItems": 50}
    assert set(body) <= li.COMPANY_INPUT_KEYS
    assert launch.url.params["maxTotalChargeUsd"] == "0.15" and launch.url.params["build"] == "1.2.3"
    assert not [k for k in keys_of(body) if _COOKIE_LIKE.search(k)]


def test_full_with_email_search_is_refused() -> None:
    assert li.FORBIDDEN_PROFILE_MODE == "Full + email search"
    good = {"profileScraperMode": "Short", "searchQuery": "x"}
    li.check_input(good, li.PEOPLE_INPUT_KEYS)
    for mode in (li.FORBIDDEN_PROFILE_MODE, "Full", None):
        with pytest.raises(ValueError, match="Short"):
            li.check_input({**good, "profileScraperMode": mode}, li.PEOPLE_INPUT_KEYS)
    with pytest.raises(ValueError, match="short"):
        li.check_input({"scraperMode": "full", "searchQuery": "x"}, li.COMPANY_INPUT_KEYS)
    with pytest.raises(ValueError, match="short"):  # the mode is always sent
        li.check_input({"searchQuery": "x"}, li.COMPANY_INPUT_KEYS)


@pytest.mark.parametrize("key", ["mongoDbConnectionString", "mongoDbDatabaseName", "profileDeduplicationMode",
                                 "postFilteringMongoDbQuery", "postFilteringMongoDbAggregation",
                                 "autoQuerySegmentation", "autoQuerySegmentationLevel", "cookie", "li_at",
                                 "sessionCookie", "customHttpHeaders", "startPage"])
def test_forbidden_and_unknown_keys_are_refused(key) -> None:
    for allowed in (li.PEOPLE_INPUT_KEYS, li.COMPANY_INPUT_KEYS):
        mode = {"profileScraperMode": "Short"} if allowed is li.PEOPLE_INPUT_KEYS else {"scraperMode": "short"}
        with pytest.raises(ValueError, match="not allowed"):
            li.check_input({**mode, "searchQuery": "x", key: "v"}, allowed)


def test_white_lists_hold_no_forbidden_key() -> None:
    for allowed in (li.PEOPLE_INPUT_KEYS, li.COMPANY_INPUT_KEYS):
        assert not allowed & li.FORBIDDEN_INPUT_KEYS
        assert not [k for k in allowed if _COOKIE_LIKE.search(k)]


# --- mapping ------------------------------------------------------------------------------------------------------


async def test_person_is_mapped_to_a_reach_contact(ledger) -> None:
    api, store, judge = Api([person()]), MemoryReachStore(), FakeJudge("investor")
    outcome = await li.ApifyLinkedInPeople(MemorySourceRunStore(), http(api), CONFIG).run(
        CAMPAIGN, SEARCH, store, judge)
    assert outcome.items_imported == 1
    (stored,) = store.contacts.values()
    assert stored.candidate.url == "https://www.linkedin.com/in/persona-ejemplo-1"
    assert (stored.candidate.platform, stored.candidate.page) == ("linkedin", "profile")
    assert stored.candidate.title == "Real estate investor"
    assert stored.candidate.snippet == "Socio, Ejemplo Capital · Madrid"
    assert stored.judged.kind == "investor" and stored.judged.keep
    assert stored.judged.name == "Persona Ejemplo 1"  # as LinkedIn wrote it, not the judge's reading
    assert stored.judged_by == "test/judge" and stored.location == "Madrid" and stored.campaign_id == CAMPAIGN.id
    assert store.enrichments[stored.candidate.url_key].contacts() == {"company": "Ejemplo Capital"}
    assert stored.candidate.url_key in store.scores
    # The long «about» is not kept.
    assert "Texto largo" not in repr(store.contacts) + repr(store.enrichments)


async def test_company_is_mapped_with_site_industry_and_phone(ledger) -> None:
    api, store = Api([company()]), MemoryReachStore()
    await li.ApifyLinkedInCompanies(MemorySourceRunStore(), http(api), CONFIG).run(
        CAMPAIGN, li.LinkedInSearch("inmobiliaria", ("Madrid",)), store, FakeJudge("investor"))
    (stored,) = store.contacts.values()
    assert stored.candidate.url == "https://www.linkedin.com/company/ejemplo-inmuebles-1"
    assert stored.candidate.page == "company"
    assert stored.candidate.title == "Ejemplo Inmuebles 1"
    assert stored.candidate.snippet == "Real Estate · Madrid · https://www.ejemplo-inmuebles.example"
    assert stored.judged.kind == "company"  # a company page is never an individual investor
    assert store.enrichments[stored.candidate.url_key].contacts() == {
        "phones": ["+34 900 000 000"], "website": "https://www.ejemplo-inmuebles.example"}
    kept = repr(store.contacts) + repr(store.enrichments)
    assert "Calle Ejemplo" not in kept and "Descripción" not in kept and "Lema" not in kept


def test_company_without_headquarter_uses_location_and_nothing_is_guessed() -> None:
    row = company(locations=[], industry=None, industries=[], website="not a site", phone=None)
    contact = li.map_company(row)
    assert contact is not None
    assert (contact.city, contact.industry, contact.website, contact.phone) == ("Madrid, España", None, None, None)
    bare = li.map_person({"linkedinUrl": "https://www.linkedin.com/in/solo-enlace"})
    assert bare is not None
    assert (bare.name, bare.headline, bare.position, bare.company, bare.city) == (None,) * 5


async def test_rules_judge_without_a_model(ledger) -> None:
    store = MemoryReachStore()
    await li.ApifyLinkedInPeople(MemorySourceRunStore(), http(Api([person()])), CONFIG).run(
        CAMPAIGN, SEARCH, store, None)
    (stored,) = store.contacts.values()
    assert stored.judged_by == "rules" and stored.judged.kind == "investor" and stored.judged.relevant


# --- the profile link -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("href", "url"), [
    ("https://es.linkedin.com/in/Persona-Ejemplo-1/?trk=public_profile", "https://www.linkedin.com/in/persona-ejemplo-1"),
    ("http://linkedin.com/in/persona-ejemplo-1", "https://www.linkedin.com/in/persona-ejemplo-1"),
    ("https://www.linkedin.com/in/persona-ejemplo-1/", "https://www.linkedin.com/in/persona-ejemplo-1"),
])
def test_profile_link_is_normalised(href, url) -> None:
    contact = li.map_person(person(linkedinUrl=href))
    assert contact is not None and contact.url == url and contact.key == "li:in:persona-ejemplo-1"


def test_profile_from_public_identifier_and_bad_links_dropped() -> None:
    contact = li.map_person(person(linkedinUrl=None, publicIdentifier="persona-ejemplo-7"))
    assert contact is not None and contact.url == "https://www.linkedin.com/in/persona-ejemplo-7"
    for href in ("https://www.linkedin.com/company/ejemplo", "https://www.linkedin.com/posts/x-123",
                 "https://evil.example/in/persona", "javascript:alert(1)"):
        assert li.map_person(person(linkedinUrl=href, publicIdentifier=None)) is None
    assert li.map_company(company(linkedinUrl="https://www.linkedin.com/in/persona")) is None


# --- e-mail ---------------------------------------------------------------------------------------------------------


async def test_email_is_never_kept(ledger) -> None:
    rows = [person(1, emails=["persona@ejemplo.example"]),
            person(2, emails=[{"email": "otra@ejemplo.example", "status": "valid"}], email="x@ejemplo.example")]
    store, judge = MemoryReachStore(), FakeJudge()
    await li.ApifyLinkedInPeople(MemorySourceRunStore(), http(Api(rows)), CONFIG).run(CAMPAIGN, SEARCH, store, judge)
    assert len(store.contacts) == 2
    kept = repr(store.contacts) + repr(store.enrichments) + repr(judge.seen)
    assert "@" not in kept and "ejemplo.example" not in kept
    assert all(not e.emails for e in store.enrichments.values())
    for row in rows:
        contact = li.map_person(row)
        assert contact is not None and "@" not in repr(contact)


# --- empty, duplicates, flag ----------------------------------------------------------------------------------------


async def test_empty_dataset(ledger) -> None:
    store, judge = MemoryReachStore(), FakeJudge()
    outcome = await li.ApifyLinkedInPeople(MemorySourceRunStore(), http(Api([])), CONFIG).run(
        CAMPAIGN, SEARCH, store, judge)
    assert (outcome.status, outcome.items_imported) == ("succeeded", 0)
    assert store.contacts == {} and judge.seen == []


async def test_rows_without_links_and_known_contacts_add_nothing(ledger) -> None:
    store, judge = MemoryReachStore(), FakeJudge()
    sink = li.ReachSink("person", CAMPAIGN, store, judge)
    assert await sink([person(1), person(1), {"firstName": "Sin enlace"}], "RUN1") == 1
    assert await sink([person(1)], "RUN1") == 0  # a page seen twice
    assert len(judge.seen) == 1 and len(judge.seen[0]) == 1


def test_flag_off_builds_no_source() -> None:
    off = settings()
    assert off.apify_linkedin_enabled is False and off.linkedin_config() is None
    assert li.build_sources(off.linkedin_config(), MemorySourceRunStore(), http(Api())) is None


def test_flag_on_needs_token_and_pinned_build() -> None:
    for env in ({"APIFY_TOKEN": "x"}, {"APIFY_TOKEN": "x", "APIFY_LINKEDIN_ACTOR_BUILD": "latest"},
                {"APIFY_LINKEDIN_ACTOR_BUILD": "1.2.3"}):
        with pytest.raises(ValidationError):
            settings(APIFY_LINKEDIN_ENABLED="true", **env)
    on = settings(APIFY_LINKEDIN_ENABLED="true", APIFY_TOKEN="x", APIFY_LINKEDIN_ACTOR_BUILD="1.2.3")
    config = on.linkedin_config()
    assert config is not None and config.people_actor == li.PEOPLE_ACTOR and config.companies_actor == li.COMPANIES_ACTOR
    sources = li.build_sources(config, MemorySourceRunStore(), http(Api()))
    assert sources is not None
    assert sources.people.runner.config.build == "1.2.3"
    assert sources.people.runner.config.build_setting == "APIFY_LINKEDIN_ACTOR_BUILD"


# --- money ----------------------------------------------------------------------------------------------------------


def test_charge_cap_formula() -> None:
    assert li.charge_cap(1, 0.10, 0.45) == 0.15
    assert li.charge_cap(2, 0.10, 0.45) == 0.25
    assert li.charge_cap(4, 0.10, 0.45) == 0.45  # 0.45 = the ceiling, not 0.45 by chance
    assert li.charge_cap(4, 0.10, 0.30) == 0.30
    config = settings(APIFY_LINKEDIN_ENABLED="true", APIFY_TOKEN="x", APIFY_LINKEDIN_ACTOR_BUILD="1.2.3",
                      APIFY_LINKEDIN_MAX_PAGES="2").linkedin_config()
    assert config is not None and config.cap_usd == 0.25
    people, companies = config.run_config("person"), config.run_config("company")
    assert people.max_charge_usd == companies.max_charge_usd == 0.25
    assert (people.max_items, companies.max_items) == (50, 100)
    for pages in ("0", "5"):
        with pytest.raises(ValidationError):
            settings(APIFY_LINKEDIN_MAX_PAGES=pages)


async def test_two_pages_reach_the_launch(ledger) -> None:
    api = Api([person()])
    config = li.LinkedInConfig(build="1.2.3", max_pages=2)
    await li.ApifyLinkedInPeople(MemorySourceRunStore(), http(api), config).run(
        CAMPAIGN, SEARCH, MemoryReachStore(), FakeJudge())
    (launch,) = api.launches()
    assert json.loads(launch.content)["takePages"] == 2 and json.loads(launch.content)["maxItems"] == 50
    assert launch.url.params["maxTotalChargeUsd"] == "0.25"


# --- 429 ------------------------------------------------------------------------------------------------------------


async def test_rate_limited_launch_is_retried_up_to_its_own_attempts(ledger) -> None:
    api, runs = Api(launch_status=429), MemorySourceRunStore()
    source = li.ApifyLinkedInPeople(runs, http(api), CONFIG)
    assert source.runner.config.max_attempts == li.LINKEDIN_MAX_ATTEMPTS == 5
    outcomes = [await source.run(CAMPAIGN, SEARCH, MemoryReachStore(), FakeJudge()) for _ in range(7)]
    assert [o.status for o in outcomes[:5]] == ["retry"] * 5
    assert outcomes[5].status == "failed" and outcomes[5].error == "max_attempts"
    assert outcomes[6].status == "failed"
    assert len(api.launches()) == 5  # a refusal starts nothing: no estimate booked
    assert not [e for e in ledger.entries if e.cost_usd]
