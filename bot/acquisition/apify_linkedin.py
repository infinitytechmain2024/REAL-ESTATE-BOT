"""LinkedIn people and companies through Apify, without cookies (v3 P1.4, decision D3).

Two actors of ``harvestapi`` («No Cookies», facts in ``docs/v3/ACTORS.md``), each one paid run through
:class:`~bot.acquisition.apify_source.ApifySocialRun` (claim, launch once with ``maxTotalChargeUsd`` and a pinned build,
bill once, import page by page):

* :class:`ApifyLinkedInPeople` -- ``harvestapi/linkedin-profile-search``. ``profileScraperMode`` is always sent as
  ``"Short"`` (the actor's own default is ``"Full"``); ``"Full + email search"`` is refused (an e-mail is personal data
  the bot never asks for). One search page is up to 25 short profiles, $0.10 a page;
* :class:`ApifyLinkedInCompanies` -- ``harvestapi/linkedin-company-search`` with ``scraperMode="short"``. One page is
  up to 50 companies, $0.002 each (+ $0.001 a start): $0.101 a page.

Only people and companies: LinkedIn is not searched for listings. The actor input is built from a **white list** of
keys (``PEOPLE_INPUT_KEYS``, ``COMPANY_INPUT_KEYS``) and checked again before every launch (:func:`check_input`): the
MongoDB fields (another party's database credentials), the deduplication and the query segmentation (more paid pages)
and any cookie-, session- or token-like key never reach an actor.

The rows go where the investor reach keeps its contacts (``reach_contacts``, migrations 027 and 028) by the reach's
own path: ``ReachStore.known`` -> the reach judge (``OpenRouterReachJudge``, the rules without a key) ->
``ReachStore.save`` -> ``ReachStore.save_enrichment``. Only what is public and asked for is kept: the name, the
headline or position, the company, the city and the link; for a company also its website, industry and the company's
own phone, as the actor gave them, and only in ``contacts.phones``. **An e-mail is never kept**: ``emails`` is never
read, and an e-mail or a phone number written into a name, headline, position or company is cut out of the text
before it is stored or sent to the judge (:func:`clean_text`). Nothing is guessed: a missing field stays empty. The
contacts come with the row itself (no page is opened), so ``enriched_at`` is not set and the reach stage's page cap
is not used (``mark_enriched=False``).

The cap of one run is ``pages x price + 0.05`` (the start and the rounding; the price is
``APIFY_LINKEDIN_USD_PER_PAGE`` for people, ``APIFY_LINKEDIN_COMPANIES_USD_PER_PAGE`` for companies), never above
``APIFY_LINKEDIN_MAX_CHARGE_USD`` (:func:`charge_cap`). At $0.15 (one people page) the live sample of P1.7
(2026-11-11) took no search page: ``SUCCEEDED``, 0 rows, «max charge reached»; such a run ends ``failed
apify_cap_too_low`` (``ApifySocialRun``), and the real minimum is still unknown, so the formula stays. Each actor has its own pinned build
(``APIFY_LINKEDIN_PEOPLE_ACTOR_BUILD``, ``APIFY_LINKEDIN_COMPANIES_ACTOR_BUILD``). A 429 on the launch is handled as in P1.2:
retried within one call for ``RATE_LIMIT_SECONDS`` without spending an attempt, then at most ``LINKEDIN_MAX_ATTEMPTS``
launches (``SocialRunConfig``); a refusal starts nothing and costs nothing.

The reach stage (``bot.campaign.reach``) runs it once per campaign with ``ACQUISITION_MODE=apify`` (P1.8).
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from bot.acquisition.apify_client import ApifyHttp, actor_path, check_pinned_build
from bot.acquisition.apify_social import RATE_LIMIT_PAUSE_SECONDS, RATE_LIMIT_SECONDS
from bot.acquisition.apify_source import ApifySocialRun, RunOutcome, SocialRunConfig, SourceRunStore
from bot.acquisition.people import profile_link

if TYPE_CHECKING:
    from bot.campaign.reach import Judged, ReachCampaign, ReachJudge, ReachStore

log = logging.getLogger(__name__)

PEOPLE_ACTOR = "harvestapi/linkedin-profile-search"
COMPANIES_ACTOR = "harvestapi/linkedin-company-search"
PEOPLE_BUILD_SETTING = "APIFY_LINKEDIN_PEOPLE_ACTOR_BUILD"
COMPANIES_BUILD_SETTING = "APIFY_LINKEDIN_COMPANIES_ACTOR_BUILD"

# The profile mode is always sent: the actor's default is «Full» ($0.004 a profile on top of the page).
PROFILE_SCRAPER_MODE = "Short"
# Searches e-mails of the people found ($0.01 a profile): personal data the bot never collects. Refused.
FORBIDDEN_PROFILE_MODE = "Full + email search"
COMPANY_SCRAPER_MODE = "short"

PEOPLE_PER_PAGE = 25       # short profiles on one search page of linkedin-profile-search
COMPANIES_PER_PAGE = 50    # companies on one page of linkedin-company-search
MAX_PAGES = 4
START_USD = 0.05           # the start and the rounding on top of the pages
# A 429 on the launch as in P1.2 (bot.acquisition.apify_social): retried within one call for at most
# RATE_LIMIT_SECONDS without spending an attempt; a 429 that outlasts it spends one of LINKEDIN_MAX_ATTEMPTS.
LINKEDIN_MAX_ATTEMPTS = 3

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
    """The ``APIFY_LINKEDIN_*`` settings of both sources (each actor with its own pinned build)."""

    people_build: str
    companies_build: str
    people_actor: str = PEOPLE_ACTOR
    companies_actor: str = COMPANIES_ACTOR
    max_pages: int = 1
    usd_per_page: float = 0.10             # people: one search page of up to 25 short profiles
    companies_usd_per_page: float = 0.101  # companies: 50 x $0.002 + the $0.001 start
    max_charge_usd: float = 0.45
    timeout_seconds: float = 300

    def __post_init__(self) -> None:
        actor_path(self.people_actor)
        actor_path(self.companies_actor)
        check_pinned_build(PEOPLE_BUILD_SETTING, self.people_build)
        check_pinned_build(COMPANIES_BUILD_SETTING, self.companies_build)
        self.cap_usd("person")
        self.cap_usd("company")

    def cap_usd(self, kind: Kind) -> float:
        price = self.usd_per_page if kind == "person" else self.companies_usd_per_page
        return charge_cap(self.max_pages, price, self.max_charge_usd)

    def run_config(self, kind: Kind) -> SocialRunConfig:
        people = kind == "person"
        return SocialRunConfig(
            platform="linkedin", purpose="li_people" if people else "li_companies",
            actor_id=self.people_actor if people else self.companies_actor,
            build=self.people_build if people else self.companies_build, max_charge_usd=self.cap_usd(kind),
            max_items=self.max_pages * (PEOPLE_PER_PAGE if people else COMPANIES_PER_PAGE),
            build_setting=PEOPLE_BUILD_SETTING if people else COMPANIES_BUILD_SETTING,
            timeout_seconds=self.timeout_seconds, max_attempts=LINKEDIN_MAX_ATTEMPTS,
            rate_limit_seconds=RATE_LIMIT_SECONDS, rate_limit_pause_seconds=RATE_LIMIT_PAUSE_SECONDS)


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


# Phone numbers written into a text: a narrow rule (owner decision 2026-11-11, a sum or a year cut out is worse than
# a phone left in). Only these are cut:
# 1. Spanish international: «+34» or «0034», optionally in brackets, then a Spanish number (below);
# 2. Spanish national: exactly 9 digits, the first 6-9, in one run or grouped 3-3-3 / 3-2-2-2 by one space or hyphen;
#    no letter or digit, dot, comma, «€» or «$» next to it, no currency or multiplier after it;
# 3. any number after a keyword (tel, tlf, telf, teléfono, móvil, mov, whatsapp, wa, phone, call, ☎, 📞): the keyword
#    and the number (a «+» or a digit first, 8-15 digits with spaces, hyphens and brackets) are cut together.
_ES9 = r"[6-9](?:\d{8}|\d{2}[ -]\d{3}[ -]\d{3}|\d{2}[ -]\d{2}[ -]\d{2}[ -]\d{2})"
_NOT_AFTER = r"(?![\w])(?![.,]\d)(?!\s?(?:€|\$|EUR\b|USD\b|euros?\b|[kKM]\b|mill|млн|m2|m²))"
_PHONE_ES = re.compile(
    r"(?:(?<![\w+])(?:\((?:\+|00)34\)|(?:\+|00)34)[ -]?|(?<![\w.,€$+]))" + _ES9 + _NOT_AFTER)
_PHONE_KEYWORD = re.compile(
    r"(?:(?<!\w)(?i:tel[ée]fono|telf|tlf|tel|m[óo]vil|mov|whatsapp|wa|phone|call)(?!\w)|☎\ufe0f?|📞)"
    r"[.:]{0,2}\s{0,3}(?P<number>(?:\(?\+|\(?\d)[\d \-()]{6,30}\d)(?!\w)")
_SEPARATORS = re.compile(r"\s*([|·•,;])(?:\s*[|·•,;])+\s*")
_EDGE = re.compile(r"^[\s|·•,;/:-]+|[\s|·•,;/:-]+$")


_YEAR_RANGE = re.compile(r"\d{4}\s?-\s?\d{4}")


def _keyword_phone(match: re.Match[str]) -> str:
    number = match.group("number")
    if _YEAR_RANGE.fullmatch(number.strip()):  # «Cold call 2015-2020»: years, not a phone
        return match.group()
    digits = sum(ch.isdigit() for ch in number)
    return " " if 8 <= digits <= 15 else match.group()


def clean_text(text: str) -> str:
    """``text`` without the e-mail addresses and phone numbers written into it, and without the separators left.

    Phones by the narrow rule above: Spanish numbers (with or without +34 / 0034) and any number after a keyword
    (tel, móvil, whatsapp, 📞 …). A foreign number without a keyword may stay (owner decision 2026-11-11).
    """
    from bot.campaign.people import _EMAIL

    out = _EMAIL.sub(" ", text)
    out = _PHONE_KEYWORD.sub(_keyword_phone, out)
    out = _PHONE_ES.sub(" ", out)
    out = _SEPARATORS.sub(r" \1 ", " ".join(out.split()))
    return _EDGE.sub("", out)


def _text(value: object, limit: int) -> str | None:
    """A text field as the actor gave it, without e-mails and phone numbers (``clean_text``), or None."""
    if not isinstance(value, str):
        return None
    return clean_text(" ".join(value.split()))[:limit] or None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _city(location: object) -> str | None:
    place = _dict(location)
    return _text(_dict(place.get("parsed")).get("city"), 200) or _text(place.get("linkedinText"), 200)


def _website(value: object) -> str | None:
    """The company's own site as the actor gave it, only an http(s) address (never ``mailto:``)."""
    text = value.strip()[:300] if isinstance(value, str) else ""
    return text if re.match(r"^https?://[^\s/@]+\.[^\s@]+$", text) else None


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
                           industry=industry, phone=_phone(_dict(row.get("phone")).get("number")))


def _phone(value: object) -> str | None:
    """The company's own phone as the actor gave it (only whitespace folded): kept in ``contacts.phones`` only."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:40] if len(re.sub(r"\D", "", text)) >= 6 else None


# --- into reach_contacts --------------------------------------------------------------------------------------------

# A company page is never an individual: the reach judge's person kinds become «company».
_PERSON_KINDS = frozenset({"investor", "agent"})


def _kind_for(contact: LinkedInContact, judged: Judged) -> Judged:
    if contact.kind == "company" and judged.kind in _PERSON_KINDS:
        return replace(judged, kind="company")
    return judged


class ReachSink:
    """A page of actor rows -> new ``reach_contacts`` rows, by the reach stage's own path. Idempotent: a page seen
    twice adds no row (``known``, and the insert is ``on conflict do nothing``); a kept row of it whose score was never
    saved (a crash or a failed write between ``save`` and ``save_enrichment``) gets its score and contacts then."""

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
        unscored = await self.store.unscored([k for k in found if k in known]) if known else {}
        fresh = [pair for key, pair in found.items() if key not in known]
        saved = 0
        verdicts: dict[str, Stored] = {}
        if fresh:
            judged, judged_by = await self._judge([c for _, c in fresh])
            for (contact, candidate), verdict in zip(fresh, judged, strict=True):
                verdict = _kind_for(contact, verdict)
                if contact.name:  # the name as LinkedIn wrote it, not as the judge read it
                    verdict = replace(verdict, name=contact.name)
                verdicts[candidate.url_key] = Stored(candidate, verdict, self.campaign.location, self.campaign.id,
                                                     judged_by)
            saved = await self.store.save(list(verdicts.values()))
        for key, verdict in unscored.items():
            verdicts[key] = Stored(found[key][1], verdict, self.campaign.location, self.campaign.id, "")
        places = [self.campaign.location, *self.campaign.aliases.values()]
        for key, (contact, _) in found.items():  # in the page's order
            item = verdicts.get(key)
            if item is None or not item.judged.keep:
                continue
            enrichment = campaign_people.Enrichment(
                phones=(contact.phone,) if contact.phone else (), website=contact.website,
                company=contact.company)  # never an e-mail
            probe = Contact(key, item.candidate.url, "linkedin", item.judged.kind, item.judged.name,
                            item.candidate.title, item.candidate.snippet, item.judged.summary_ru,
                            contacts=enrichment.contacts())
            points, _ = campaign_people.score(probe, self.campaign.investor, places=places)
            try:  # the contacts came with the row (no page was opened): enriched_at and the reach's page cap stay
                await self.store.save_enrichment(key, enrichment if enrichment.contacts() else None, points,
                                                 mark_enriched=False)
            except Exception:  # noqa: BLE001 - the contact is stored; its score is saved when the page comes again
                log.warning("apify_linkedin.enrichment_save_failed", extra={"campaign_id": self.campaign.id})
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

    def __init__(self, store: SourceRunStore, http: ApifyHttp, config: LinkedInConfig, **run_kw: Any) -> None:
        self.config = config  # run_kw: ApifySocialRun's clock, sleep, poll_seconds (tests)
        self.runner = ApifySocialRun(store, http, config.run_config(self.kind), **run_kw)

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


__all__ = [
    "COMPANIES_ACTOR",
    "COMPANY_INPUT_KEYS",
    "FORBIDDEN_INPUT_KEYS",
    "FORBIDDEN_PROFILE_MODE",
    "LINKEDIN_MAX_ATTEMPTS",
    "PEOPLE_ACTOR",
    "PEOPLE_INPUT_KEYS",
    "PROFILE_SCRAPER_MODE",
    "REAL_ESTATE_FUNCTION_ID",
    "ApifyLinkedInCompanies",
    "ApifyLinkedInPeople",
    "LinkedInConfig",
    "LinkedInContact",
    "LinkedInSearch",
    "LinkedInSources",
    "ReachSink",
    "build_sources",
    "charge_cap",
    "check_input",
    "clean_text",
    "map_company",
    "map_person",
]
