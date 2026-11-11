"""LinkedIn people and companies through Apify, without cookies (v3 P1.4, decision D3).

Two actors of ``harvestapi`` («No Cookies», facts in ``docs/v3/ACTORS.md``), each one paid run through
:class:`~bot.acquisition.apify_source.ApifySocialRun` (claim, launch once with ``maxTotalChargeUsd`` and a pinned build,
bill once, import page by page):

* :class:`ApifyLinkedInPeople` -- ``harvestapi/linkedin-profile-search``. ``profileScraperMode`` is always sent as
  ``"Short"`` (the actor's own default is ``"Full"``); ``"Full + email search"`` is refused (an e-mail is personal data
  the bot never asks for). One search page is up to 25 short profiles, $0.10 a page;
* :class:`ApifyLinkedInCompanies` -- ``harvestapi/linkedin-company-search`` with ``scraperMode="short"``. One page is
  up to 50 companies, $0.002 each (+ $0.001 a start), so a page costs about the same $0.10.

Only people and companies: LinkedIn is not searched for listings. The actor input is built from a **white list** of
keys (``PEOPLE_INPUT_KEYS``, ``COMPANY_INPUT_KEYS``) and checked again before every launch (:func:`check_input`): the
MongoDB fields (another party's database credentials), the deduplication and the query segmentation (more paid pages)
and any cookie-, session- or token-like key never reach an actor.

The rows go where the investor reach keeps its contacts (``reach_contacts``, migrations 027 and 028) by the reach's
own path: ``ReachStore.known`` -> the reach judge (``OpenRouterReachJudge``, the rules without a key) ->
``ReachStore.save`` -> ``ReachStore.save_enrichment``. Only what is public and asked for is kept: the name, the
headline or position, the company, the city and the link; for a company also its website, industry and the company's
own phone, as the actor gave them. **An e-mail is never kept**, even when a row carries ``emails``. Nothing is guessed:
a missing field stays empty.

The cap of one run is ``pages x APIFY_LINKEDIN_USD_PER_PAGE + 0.05`` (the start and the rounding), never above
``APIFY_LINKEDIN_MAX_CHARGE_USD`` (:func:`charge_cap`). A refused launch (429) is tried again at most
``LINKEDIN_MAX_ATTEMPTS`` times (``SocialRunConfig.max_attempts``): a refusal starts nothing and costs nothing.

Nothing calls it yet: the campaign runner wires it in P1.8.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from bot.acquisition.apify_client import ApifyHttp, actor_path, check_pinned_build
from bot.acquisition.apify_source import ApifySocialRun, RunOutcome, SocialRunConfig, SourceRunStore
from bot.acquisition.people import profile_link

if TYPE_CHECKING:
    from bot.campaign.reach import Judged, ReachCampaign, ReachJudge, ReachStore

log = logging.getLogger(__name__)

PEOPLE_ACTOR = "harvestapi/linkedin-profile-search"
COMPANIES_ACTOR = "harvestapi/linkedin-company-search"
BUILD_SETTING = "APIFY_LINKEDIN_ACTOR_BUILD"

# The profile mode is always sent: the actor's default is «Full» ($0.004 a profile on top of the page).
PROFILE_SCRAPER_MODE = "Short"
# Searches e-mails of the people found ($0.01 a profile): personal data the bot never collects. Refused.
FORBIDDEN_PROFILE_MODE = "Full + email search"
COMPANY_SCRAPER_MODE = "short"

PEOPLE_PER_PAGE = 25       # short profiles on one search page of linkedin-profile-search
COMPANIES_PER_PAGE = 50    # companies on one page of linkedin-company-search
MAX_PAGES = 4
START_USD = 0.05           # the start and the rounding on top of the pages
LINKEDIN_MAX_ATTEMPTS = 5  # launches of one input; a 429 refusal starts nothing and costs nothing

PEOPLE_INPUT_KEYS = frozenset({"profileScraperMode", "searchQuery", "locations", "functionIds", "industryIds",
                               "takePages", "maxItems"})
COMPANY_INPUT_KEYS = frozenset({"scraperMode", "searchQuery", "locations", "industryIds", "takePages", "maxItems"})
# Never sent, whatever the white lists above become: another party's database, deduplication against it, the query
# segmentation (it multiplies the paid pages).
FORBIDDEN_INPUT_KEYS = frozenset({"mongoDbConnectionString", "mongoDbDatabaseName", "profileDeduplicationMode",
                                  "postFilteringMongoDbQuery", "postFilteringMongoDbAggregation",
                                  "autoQuerySegmentation"})
_FORBIDDEN_PREFIXES = ("mongoDb", "postFilteringMongoDb", "autoQuerySegmentation", "profileDeduplication")
_COOKIE_LIKE = re.compile(r"cookie|session|token|password|secret|auth|credential|login|li_at|header|proxy", re.I)
_ID = re.compile(r"^[0-9]{1,6}$")
REAL_ESTATE_FUNCTION_ID = "23"  # functionIds: «Real Estate» (inputSchema of linkedin-profile-search)

Kind = Literal["person", "company"]


def charge_cap(max_pages: int, usd_per_page: float, max_charge_usd: float) -> float:
    """``maxTotalChargeUsd`` of one run: ``pages x price + 0.05``, never above ``max_charge_usd``."""
    if not (1 <= max_pages <= MAX_PAGES and math.isfinite(usd_per_page) and usd_per_page > 0
            and math.isfinite(max_charge_usd) and max_charge_usd > 0):
        raise ValueError("unsafe linkedin charge settings")
    return round(min(max_pages * usd_per_page + START_USD, max_charge_usd), 4)


def check_input(body: Mapping[str, Any], allowed: frozenset[str]) -> None:
    """Refuse an actor input with a key outside ``allowed``, a forbidden or cookie-like key, or the e-mail mode."""
    for key in body:
        if (key not in allowed or key in FORBIDDEN_INPUT_KEYS or key.startswith(_FORBIDDEN_PREFIXES)
                or _COOKIE_LIKE.search(key)):
            raise ValueError(f"linkedin actor input key not allowed: {key}")
    if "profileScraperMode" in allowed and body.get("profileScraperMode") != PROFILE_SCRAPER_MODE:
        # «Full + email search» above all; «Full» is not needed either (the page already has what is kept).
        raise ValueError("linkedin profileScraperMode must be Short")
    if "scraperMode" in allowed and body.get("scraperMode") != COMPANY_SCRAPER_MODE:
        raise ValueError("linkedin scraperMode must be short")


@dataclass(frozen=True, slots=True)
class LinkedInSearch:
    """One search: the words (``searchQuery``), the places (``locations``) and the LinkedIn ids the task names.

    ``function_ids`` (people only; ``"23"`` = Real Estate) and ``industry_ids`` are LinkedIn's own numeric ids; empty:
    not sent.
    """

    query: str
    locations: tuple[str, ...] = ()
    function_ids: tuple[str, ...] = ()
    industry_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        query = " ".join(self.query.split())
        if not 2 <= len(query) <= 200:
            raise ValueError("linkedin search query must be 2-200 characters")
        object.__setattr__(self, "query", query)
        places = tuple(" ".join(p.split()) for p in self.locations if p and p.strip())
        if len(places) > 5 or any(len(p) > 100 for p in places):
            raise ValueError("at most 5 linkedin locations of up to 100 characters")
        object.__setattr__(self, "locations", places)
        for ids in (self.function_ids, self.industry_ids):
            if len(ids) > 10 or not all(_ID.match(i) for i in ids):
                raise ValueError("linkedin ids are LinkedIn's numeric ids")


@dataclass(frozen=True, slots=True)
class LinkedInConfig:
    """The ``APIFY_LINKEDIN_*`` settings of both sources (one pinned build for both actors)."""

    build: str
    people_actor: str = PEOPLE_ACTOR
    companies_actor: str = COMPANIES_ACTOR
    max_pages: int = 1
    usd_per_page: float = 0.10
    max_charge_usd: float = 0.45
    timeout_seconds: float = 300

    def __post_init__(self) -> None:
        actor_path(self.people_actor)
        actor_path(self.companies_actor)
        check_pinned_build(BUILD_SETTING, self.build)
        charge_cap(self.max_pages, self.usd_per_page, self.max_charge_usd)

    @property
    def cap_usd(self) -> float:
        return charge_cap(self.max_pages, self.usd_per_page, self.max_charge_usd)

    def run_config(self, kind: Kind) -> SocialRunConfig:
        people = kind == "person"
        return SocialRunConfig(
            platform="linkedin", purpose="li_people" if people else "li_companies",
            actor_id=self.people_actor if people else self.companies_actor, build=self.build,
            max_charge_usd=self.cap_usd,
            max_items=self.max_pages * (PEOPLE_PER_PAGE if people else COMPANIES_PER_PAGE),
            build_setting=BUILD_SETTING, timeout_seconds=self.timeout_seconds, max_attempts=LINKEDIN_MAX_ATTEMPTS)


# --- the rows -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LinkedInContact:
    """What is kept of one row: public, asked for, as the actor gave it (never an e-mail)."""

    kind: Kind
    key: str                     # li:in:<id> or li:company:<id> (bot.acquisition.people.profile_link)
    url: str                     # https://www.linkedin.com/in/<id> or /company/<id>
    name: str | None = None
    headline: str | None = None  # a person's headline; a company has none
    position: str | None = None  # a person's current position
    company: str | None = None   # a person's current company
    city: str | None = None
    website: str | None = None   # a company's own site
    industry: str | None = None
    phone: str | None = None     # the company's own phone (a business contact, like an agency's in a listing)

    @property
    def title(self) -> str:
        return (self.headline if self.kind == "person" else self.name) or ""

    @property
    def snippet(self) -> str:
        if self.kind == "person":
            role = ", ".join(p for p in (self.position, self.company) if p)
            parts = [role, self.city]
        else:
            parts = [self.industry, self.city, self.website]
        return " · ".join(p for p in parts if p)[:600]


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit] or None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _city(location: object) -> str | None:
    place = _dict(location)
    return _text(_dict(place.get("parsed")).get("city"), 200) or _text(place.get("linkedinText"), 200)


def _website(value: object) -> str | None:
    text = _text(value, 300)
    return text if text and re.match(r"^https?://[^\s/]+\.[^\s]+$", text) else None


def map_person(row: Mapping[str, Any]) -> LinkedInContact | None:
    """One row of ``linkedin-profile-search`` (short profile), or None without a profile link. ``emails`` is ignored."""
    link = profile_link("linkedin", row.get("linkedinUrl"))
    if link is None and isinstance(row.get("publicIdentifier"), str):
        link = profile_link("linkedin", f"https://www.linkedin.com/in/{row['publicIdentifier'].strip()}")
    if link is None or not link[0].startswith("li:in:"):
        return None
    name = " ".join(p for p in (_text(row.get("firstName"), 100), _text(row.get("lastName"), 100)) if p)[:200]
    positions = row.get("currentPosition")
    current = _dict(positions[0]) if isinstance(positions, list) and positions else {}
    return LinkedInContact("person", link[0], link[1], name=name or None, headline=_text(row.get("headline"), 300),
                           position=_text(current.get("position"), 200),
                           company=_text(current.get("companyName"), 200), city=_city(row.get("location")))


def map_company(row: Mapping[str, Any]) -> LinkedInContact | None:
    """One row of ``linkedin-company-search`` (short), or None without a company link."""
    link = profile_link("linkedin", row.get("linkedinUrl"))
    if link is None or not link[0].startswith("li:company:"):
        return None
    industry = _text(row.get("industry"), 200)
    if industry is None and isinstance(row.get("industries"), list) and row["industries"]:
        industry = _text(_dict(row["industries"][0]).get("name"), 200)
    places = [_dict(p) for p in row.get("locations") or [] if isinstance(p, dict)]
    head = next((p for p in places if p.get("headquarter") is True), None)
    city = (_text(_dict(head.get("parsed")).get("city"), 200) or _text(head.get("city"), 200)) if head else None
    return LinkedInContact("company", link[0], link[1], name=_text(row.get("name"), 200),
                           city=city or _city(row.get("location")), website=_website(row.get("website")),
                           industry=industry, phone=_text(_dict(row.get("phone")).get("number"), 40))


# --- into reach_contacts --------------------------------------------------------------------------------------------

# A company page is never an individual: the reach judge's person kinds become «company».
_PERSON_KINDS = frozenset({"investor", "agent"})


def _kind_for(contact: LinkedInContact, judged: Judged) -> Judged:
    if contact.kind == "company" and judged.kind in _PERSON_KINDS:
        return replace(judged, kind="company")
    return judged


class ReachSink:
    """A page of actor rows -> new ``reach_contacts`` rows, by the reach stage's own path. Idempotent: a page seen
    twice adds nothing (``known``, and the insert is ``on conflict do nothing``)."""

    def __init__(self, kind: Kind, campaign: ReachCampaign, store: ReachStore, judge: ReachJudge | None) -> None:
        self.kind, self.campaign, self.store, self.judge = kind, campaign, store, judge

    async def __call__(self, items: list[dict[str, Any]], run_id: str) -> int:
        from bot.campaign import people as campaign_people
        from bot.campaign.reach import Candidate, Contact, Stored

        mapper = map_person if self.kind == "person" else map_company
        page = "profile" if self.kind == "person" else "company"
        found: dict[str, tuple[LinkedInContact, Candidate]] = {}
        for item in items:
            contact = mapper(item)
            if contact is None:
                continue
            candidate = Candidate(contact.url, "linkedin", page, contact.title[:300], contact.snippet)
            found.setdefault(candidate.url_key, (contact, candidate))
        if not found:
            return 0
        known = await self.store.known(list(found))
        fresh = [pair for key, pair in found.items() if key not in known]
        if not fresh:
            return 0
        judged, judged_by = await self._judge([c for _, c in fresh])
        stored = []
        for (contact, candidate), verdict in zip(fresh, judged, strict=True):
            verdict = _kind_for(contact, verdict)
            if contact.name:  # the name as LinkedIn wrote it, not as the judge read it
                verdict = replace(verdict, name=contact.name)
            stored.append((contact, Stored(candidate, verdict, self.campaign.location, self.campaign.id, judged_by)))
        saved = await self.store.save([s for _, s in stored])
        places = [self.campaign.location, *self.campaign.aliases.values()]
        for contact, item in stored:
            if not item.judged.keep:
                continue
            enrichment = campaign_people.Enrichment(
                phones=(contact.phone,) if contact.phone else (), website=contact.website,
                company=contact.company)  # never an e-mail
            probe = Contact(item.candidate.url_key, item.candidate.url, "linkedin", item.judged.kind,
                            item.judged.name, item.candidate.title, item.candidate.snippet, item.judged.summary_ru,
                            contacts=enrichment.contacts())
            points, _ = campaign_people.score(probe, self.campaign.investor, places=places)
            # Nothing beyond the row itself: only the score (no page was opened, ``enriched_at`` stays empty).
            await self.store.save_enrichment(item.candidate.url_key, enrichment if enrichment.contacts() else None,
                                             points)
        return saved

    async def _judge(self, candidates: Sequence[Any]) -> tuple[list[Judged], str]:
        from bot.campaign.reach import rule_judge

        if self.judge is not None:
            try:
                return await self.judge.judge(self.campaign, candidates), self.judge.model[:120]
            except Exception as exc:  # noqa: BLE001 - fail open to the rules, as the reach stage does
                log.warning("apify_linkedin.judge_failed %s", getattr(exc, "code", type(exc).__name__))
        return [rule_judge(c, self.campaign) for c in candidates], "rules"


# --- the sources ----------------------------------------------------------------------------------------------------


class _LinkedInSource:
    kind: Kind
    allowed: frozenset[str]

    def __init__(self, store: SourceRunStore, http: ApifyHttp, config: LinkedInConfig) -> None:
        self.config = config
        self.runner = ApifySocialRun(store, http, config.run_config(self.kind))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(actor={self.runner.config.actor_id})"

    def actor_input(self, search: LinkedInSearch) -> dict[str, Any]:
        raise NotImplementedError

    async def run(self, campaign: ReachCampaign, search: LinkedInSearch, reach_store: ReachStore,
                  judge: ReachJudge | None, *, owner_user_id: int | None = None) -> RunOutcome:
        """One paid run of ``search`` for the campaign (launched once), its rows judged into ``reach_contacts``."""
        body = self.actor_input(search)
        check_input(body, self.allowed)  # again, right before the launch
        return await self.runner.run(campaign.id, body, ReachSink(self.kind, campaign, reach_store, judge),
                                     owner_user_id=owner_user_id)


class ApifyLinkedInPeople(_LinkedInSource):
    kind: Kind = "person"
    allowed = PEOPLE_INPUT_KEYS

    def actor_input(self, search: LinkedInSearch) -> dict[str, Any]:
        pages = self.config.max_pages
        body: dict[str, Any] = {"profileScraperMode": PROFILE_SCRAPER_MODE, "searchQuery": search.query,
                                "takePages": pages, "maxItems": pages * PEOPLE_PER_PAGE}
        if search.locations:
            body["locations"] = list(search.locations)
        if search.function_ids:
            body["functionIds"] = list(search.function_ids)
        if search.industry_ids:
            body["industryIds"] = list(search.industry_ids)
        check_input(body, self.allowed)
        return body


class ApifyLinkedInCompanies(_LinkedInSource):
    kind: Kind = "company"
    allowed = COMPANY_INPUT_KEYS

    def actor_input(self, search: LinkedInSearch) -> dict[str, Any]:
        pages = self.config.max_pages
        body: dict[str, Any] = {"scraperMode": COMPANY_SCRAPER_MODE, "searchQuery": search.query,
                                "takePages": pages, "maxItems": pages * COMPANIES_PER_PAGE}
        if search.locations:
            body["locations"] = list(search.locations)
        if search.industry_ids:
            body["industryIds"] = list(search.industry_ids)
        check_input(body, self.allowed)
        return body


@dataclass(frozen=True, slots=True)
class LinkedInSources:
    people: ApifyLinkedInPeople
    companies: ApifyLinkedInCompanies


def build_sources(config: LinkedInConfig | None, store: SourceRunStore, http: ApifyHttp) -> LinkedInSources | None:
    """Both sources, or None while ``APIFY_LINKEDIN_ENABLED`` is off (``config`` None)."""
    if config is None:
        return None
    return LinkedInSources(ApifyLinkedInPeople(store, http, config), ApifyLinkedInCompanies(store, http, config))


__all__ = ["COMPANIES_ACTOR", "COMPANY_INPUT_KEYS", "FORBIDDEN_INPUT_KEYS", "FORBIDDEN_PROFILE_MODE",
           "LINKEDIN_MAX_ATTEMPTS", "PEOPLE_ACTOR", "PEOPLE_INPUT_KEYS", "PROFILE_SCRAPER_MODE",
           "REAL_ESTATE_FUNCTION_ID", "ApifyLinkedInCompanies", "ApifyLinkedInPeople", "LinkedInConfig",
           "LinkedInContact", "LinkedInSearch", "LinkedInSources", "ReachSink", "build_sources", "charge_cap",
           "check_input", "map_company", "map_person"]
