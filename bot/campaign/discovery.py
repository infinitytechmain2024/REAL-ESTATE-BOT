"""Facebook group discovery for a campaign: search Facebook itself, keep what fits.

The only browsing is through the Browser Session Manager on one lease:
``https://www.facebook.com/search/groups/?q=<seed>`` for each plan seed, then
at most a few group pages to confirm activity. Everything is bounded (seeds,
results per seed, candidates, visits, wall time) and paced. Each group found
is stored once per campaign with a decision -- queued, rejected and why, or
kept but not queued (inaccessible) -- so nothing is re-evaluated blindly.

A Facebook challenge stops the run at once: the lease is released as
``VERIFICATION_REQUIRED``, the profile becomes ``human_verification_required``
(the /login watcher then asks an operator to clear it by hand), and the
campaign pauses. Calling :meth:`FacebookDiscovery.run` again later resumes it:
seeds and visits already done are skipped. No bypass, no retry.

Discovery never stores posts; activity evidence is counts and ages only.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from itertools import zip_longest
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import quote, urlsplit

from bot.facebook_collector.challenges import detect_challenge
from bot.facebook_collector.models import ChallengeDetected, GroupRead, GroupState
from bot.services.facebook.activity import age_days

from .architect import _INV_STEMS, _INV_WORDS, _RE_STEMS, _RE_WORDS, GAZETTEER, _norm
from .models import Campaign, CampaignPlan, Language
from .store import CampaignStore

if TYPE_CHECKING:
    import asyncpg

    from bot.facebook_collector.browser import BrowserLease

log = logging.getLogger(__name__)

ACTOR = "campaign:discovery"
SEARCH_URL = "https://www.facebook.com/search/groups/?q={query}"
RESUMABLE_STATES = frozenset({"planned", "discovering", "paused_verification"})
RELEVANCE_THRESHOLD = 0.6
ACTIVE_DAYS = 14
DEAD_DAYS = 90

Activity = Literal["ACTIVE", "INACTIVE", "DEAD", "INACCESSIBLE", "UNKNOWN"]
GroupStateName = Literal["discovered", "queued", "rejected", "collected", "skipped"]


class DiscoveryRefused(RuntimeError):
    """Discovery did not start; ``str(exc)`` is a short machine-readable reason."""


class DiscoveryError(RuntimeError):
    """Discovery could not continue safely (e.g. repeated search failures)."""


# --- records -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    id: str
    name: str
    state: str


@dataclass(frozen=True, slots=True)
class CampaignGroup:
    group_key: str
    canonical_url: str
    name: str | None
    language: str | None
    seed: str | None
    relevance_score: float
    activity: Activity = "UNKNOWN"
    activity_evidence: dict[str, Any] = field(default_factory=dict)
    state: GroupStateName = "discovered"
    reject_reason: str | None = None
    window_no: int | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    campaign_id: str
    state: str  # the campaign's state when the run ended
    stop_reason: str | None
    seeds_total: int
    seeds_done: int
    seeds_run: int  # searched in this run
    candidates: int  # new groups stored in this run
    visits: int  # group pages opened in this run
    queued: int
    rejected: int
    challenge: str | None = None
    budget_exhausted: bool = False


# --- pure helpers --------------------------------------------------------------

_FACEBOOK_HOSTS = frozenset({"facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com"})
_GROUP_PATH = re.compile(r"/groups/([A-Za-z0-9_.-]{1,100})/?")
_RESERVED_GROUP_PATHS = frozenset({"feed", "discover", "joins", "create", "search", "category", "you"})


def search_url(seed: str) -> str:
    return SEARCH_URL.format(query=quote(seed, safe=""))


def canonical_group(url: object) -> tuple[str, str] | None:
    """``(group_key, https://www.facebook.com/groups/<key>/)`` for a group's own URL, else ``None``.

    Posts, permalinks, member lists and other sub-pages are not a group URL.
    """
    if not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url.strip())
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in _FACEBOOK_HOSTS
                or parsed.username or parsed.password or parsed.port):
            return None
    except ValueError:
        return None
    match = _GROUP_PATH.fullmatch(parsed.path)
    if not match:
        return None
    key = match[1].lower()
    if key in _RESERVED_GROUP_PATHS or key.strip(".") == "":
        return None
    return key, f"https://www.facebook.com/groups/{key}/"


@dataclass(frozen=True, slots=True)
class Relevance:
    score: float
    reason: str | None  # why it is rejected; None when relevant
    matched: dict[str, list[str]]

    @property
    def relevant(self) -> bool:
        return self.reason is None


_WORD = re.compile(r"\w+")
_EXTRA_RE_WORDS = frozenset({
    "alquileres", "inmobiliario", "compartir", "roommate", "roommates", "flatshare", "expats",
    "arriendo", "realty", "realtor", "realtors",
})
_EXTRA_RE_STEMS = ("жил", "квартир", "житл")
_EXTRA_INV_WORDS = frozenset({"business", "negocios", "emprendimiento", "networking", "empresarios"})
_EXTRA_INV_STEMS = ("бизнес", "бізнес", "предприним", "підприєм")
_RE_PHRASES = ("real estate", "bienes raices")
_INV_PHRASES = ("business angel",)
# Topics that share words with housing/investing but are not it.
_OFF_TOPIC_WORDS = frozenset({
    # jobs
    "trabajo", "trabajos", "empleo", "empleos", "vacante", "vacantes", "job", "jobs", "vacancy",
    "vacancies", "hiring", "careers", "работа", "работу", "робота", "роботу",
    # dating
    "dating", "citas", "singles", "ligar",
    # crypto / trading signals
    "crypto", "cripto", "bitcoin", "btc", "forex", "signals", "senales", "trading", "binance",
    # vehicles
    "car", "cars", "coche", "coches", "auto", "autos", "carro", "carros", "moto", "motos",
    "vehiculos", "авто",
})
_OFF_TOPIC_STEMS = (
    "ваканс", "трудоустр", "працевлашт", "знакомств", "знайомств", "крипт", "трейдинг", "сигнал",
    "автомобил", "автомобіл", "машин",
)


def _location_terms(plan: CampaignPlan) -> tuple[frozenset[str], tuple[str, ...], tuple[str, ...]]:
    """(exact Latin words, Cyrillic stems, multi-word phrases) naming the plan's place."""
    words: set[str] = set()
    stems: set[str] = set()
    phrases: set[str] = set()
    names = {_norm(plan.location), *(_norm(alias) for alias in plan.location_aliases.values())}
    for place in GAZETTEER:
        if _norm(place.canonical) in names or names & {_norm(a) for a in place.aliases.values()}:
            words |= place.words
            stems |= set(place.stems)
    for name in names:
        tokens = _WORD.findall(name)
        if len(tokens) > 1:
            phrases.add(" ".join(tokens))
        # «Ubud, Bali»: each part of a name names the place too (short words like «de» do not).
        for token in tokens:
            if len(tokens) > 1 and len(token) < 4:
                continue
            if token.isascii():
                words.add(token)
            else:  # case endings: Мадрид -> Мадриде, Київ -> Києві
                stems.add(token[: max(4, len(token) - 1)])
    return frozenset(words), tuple(sorted(stems)), tuple(sorted(phrases))


def _hits(words: list[str], joined: str, exact: Iterable[str], stems: tuple[str, ...],
          phrases: tuple[str, ...] = ()) -> list[str]:
    exact_set = frozenset(exact)
    found = {w for w in words if w in exact_set or (stems and w.startswith(stems))}
    found |= {p for p in phrases if p in joined}
    return sorted(found)


def score_relevance(name: str | None, card: str | None, plan: CampaignPlan, *, group_key: str = "") -> Relevance:
    """Does a search result fit the campaign? Pure; score in [0, 1].

    The place is required (in any language, or in the group's URL slug), then
    the vertical's vocabulary (ES/EN/RU/UK); obviously off-topic words (jobs,
    dating, crypto signals, vehicles) pull the score below the threshold.
    """
    name_text = _norm(name or "")
    joined = " ".join(_WORD.findall(f"{name_text} {_norm(card or '')}"))
    words = joined.split()
    loc_words, loc_stems, loc_phrases = _location_terms(plan)
    location = _hits(words, joined, loc_words, loc_stems, loc_phrases)
    key = group_key.lower()
    location += [w for w in loc_words if len(w) >= 4 and w in key and w not in location]

    re_words, re_stems = _RE_WORDS | _EXTRA_RE_WORDS, _RE_STEMS + _EXTRA_RE_STEMS
    inv_words, inv_stems = _INV_WORDS | _EXTRA_INV_WORDS, _INV_STEMS + _EXTRA_INV_STEMS
    vertical: list[str] = []
    if plan.vertical in ("real_estate", "both"):
        vertical += _hits(words, joined, re_words, re_stems, _RE_PHRASES)
    if plan.vertical in ("investors", "both"):
        vertical += _hits(words, joined, inv_words, inv_stems, _INV_PHRASES)
    vertical = sorted(set(vertical))
    name_joined = " ".join(_WORD.findall(name_text))
    name_words = set(name_joined.split())
    in_name = any(hit in name_words or (" " in hit and hit in name_joined) for hit in vertical)
    off_topic = _hits(words, joined, _OFF_TOPIC_WORDS, _OFF_TOPIC_STEMS)
    matched = {"location": location, "vertical": vertical, "off_topic": off_topic}

    if not location:
        return Relevance(0.0, "no_location", matched)
    score = 0.4
    if vertical:
        score += 0.4 + 0.1 * min(len(vertical) - 1, 2) + (0.1 if in_name else 0.0)
    score -= 0.5 * len(off_topic)
    score = round(max(0.0, min(1.0, score)), 3)
    if score >= RELEVANCE_THRESHOLD:
        return Relevance(score, None, matched)
    return Relevance(score, "off_topic" if off_topic else "no_vertical_match", matched)


_NUM = r"(\d+(?:[.,]\d+)?)\s*(k|mil|тыс|тис)?\.?\s*\+?\s*"
_PERIOD = {
    "day": "day", "dia": "day", "день": "day", "сутки": "day", "добу": "day", "today": "day", "hoy": "day",
    "week": "week", "semana": "week", "неделю": "week", "тиждень": "week",
    "month": "month", "mes": "month", "месяц": "month", "місяць": "month",
    "year": "year", "ano": "year", "год": "year", "рік": "year",
}
_RATE = (
    re.compile(_NUM + r"(?:new\s+)?posts?\s+(?:(?:a|per)\s+(day|week|month|year)|(today))\b"),
    re.compile(_NUM + r"(?:nuevas?\s+)?publicacion(?:es)?\s+(?:al|por|a\s+la|cada|(hoy))\s*(dia|semana|mes|ano)?\b"),
    re.compile(_NUM + r"(?:новых\s+|новые\s+)?(?:публикаци\w*|пост\w*|записе\w*|записи)\s+(?:в|за)\s+(день|неделю|месяц|год|сутки)"),
    re.compile(_NUM + r"(?:нових\s+)?(?:допис\w*|публікаці\w*|пост\w*)\s+(?:на|в|за)\s+(день|тиждень|місяць|рік|добу)"),
)
_YEAR_AGO = re.compile(
    r"\b(?:a|an|one|\d+)\s+years?\s+ago\b"
    r"|\bhace\s+(?:mas\s+de\s+)?(?:un|\d+)\s+anos?\b"
    r"|(?:\b\d+\s+(?:года|лет)|\bгод)\s+назад"
    r"|(?:\b\d+\s+(?:роки|років)|\bрік)\s+тому"
)
_MEMBERS = re.compile(r"(\d+(?:[.,]\d+)*)\s*(k|m|mil|тыс|тис|млн)?\.?\s*(?:members|miembros|участник\w*|учасник\w*)")
_MULTIPLIER = {"k": 1_000, "mil": 1_000, "тыс": 1_000, "тис": 1_000, "m": 1_000_000, "млн": 1_000_000}


def _number(raw: str, multiplier: str | None) -> float:
    if multiplier:
        return float(raw.replace(",", ".")) * _MULTIPLIER[multiplier]
    return float(re.sub(r"[.,]", "", raw))


def card_activity(text: str | None) -> tuple[Activity, dict[str, Any]]:
    """Activity from a search result card: "10+ posts a day", "5 publicaciones al día",
    "3 публикации в неделю", "2 дописи на день", "active a year ago". Pure.

    Posts per day/week -> ACTIVE; zero posts or last activity a year or more
    ago -> DEAD; anything else -> UNKNOWN (never a guess).
    """
    normalized = " ".join(_norm(text or "").split())
    evidence: dict[str, Any] = {"source": "card"}
    members = _MEMBERS.search(normalized)
    if members:
        evidence["members"] = int(_number(members[1], members[2]))
    for pattern in _RATE:
        match = pattern.search(normalized)
        if not match:
            continue
        period = next((g for g in match.groups()[2:] if g), "")
        unit = _PERIOD.get(period)
        if unit is None:
            continue
        count = _number(match[1], match[2])
        evidence.update(posts=count, per=unit)
        if count == 0:
            return "DEAD", evidence
        if unit in ("day", "week"):
            return "ACTIVE", evidence
        if unit == "year":
            return "DEAD", evidence
        return "UNKNOWN", evidence
    if _YEAR_AGO.search(normalized):
        evidence["last_activity"] = "year_or_more"
        return "DEAD", evidence
    return "UNKNOWN", evidence


def _iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _post_age(published_at: object, body: str, now: datetime) -> float | None:
    stamp = _iso(published_at)
    if stamp is not None:
        return max(0.0, (now - stamp).total_seconds() / 86400)
    # Facebook's relative stamp ("3 h", "hace 2 días", "5 дн.") is a short line
    # near the top of the post; longer lines are content, not a date.
    for line in body.splitlines()[:8]:
        line = line.strip().rstrip("·").strip()
        if not line or len(line) > 24:
            continue
        age = age_days(line, now=now)
        if age is not None:
            return age
    return None


def visit_activity(read: GroupRead, *, now: datetime) -> tuple[Activity, dict[str, Any]]:
    """Activity from one group read: the newest dated post decides. Pure.

    Newest <= 14 days -> ACTIVE, <= 90 -> INACTIVE, older -> DEAD. An
    inaccessible group stays INACCESSIBLE -- it is not evidence of a dead one.
    """
    evidence: dict[str, Any] = {
        "source": "visit", "reader_state": read.state.value, "posts_seen": len(read.posts),
        "articles": read.diagnostics.get("articles"),
    }
    if read.state is GroupState.INACCESSIBLE:
        return "INACCESSIBLE", evidence
    if read.state is GroupState.UNKNOWN:
        return "UNKNOWN", evidence
    ages = [a for a in (_post_age(p.published_at, p.body_text, now) for p in read.posts) if a is not None]
    evidence["posts_dated"] = len(ages)
    if ages:
        newest = round(min(ages), 2)
        evidence["newest_age_days"] = newest
        if newest <= ACTIVE_DAYS:
            return "ACTIVE", evidence
        return ("INACTIVE" if newest <= DEAD_DAYS else "DEAD"), evidence
    if read.state is GroupState.INACTIVE:
        return "INACTIVE", evidence
    # Posts, but none datable: only an explicit posting rate on the page counts.
    page_activity, page_evidence = card_activity(str(read.evidence.get("text", ""))[:20_000])
    if page_activity == "ACTIVE":
        evidence.update(page_rate=page_evidence.get("posts"), per=page_evidence.get("per"))
        return "ACTIVE", evidence
    return "UNKNOWN", evidence


def decide(relevance_reason: str | None, activity: Activity) -> tuple[GroupStateName, str | None]:
    """Group state for a relevance verdict and activity; only finalize() queues."""
    if relevance_reason:
        return "rejected", relevance_reason
    if activity == "INACTIVE":
        return "rejected", "inactive"
    if activity == "DEAD":
        return "rejected", "dead"
    return "discovered", None  # ACTIVE: queue candidate; UNKNOWN: visit candidate; INACCESSIBLE: kept


def plan_seeds(plan: CampaignPlan, max_seeds: int) -> list[tuple[Language, str]]:
    """Seeds round-robin across the plan's languages (in plan order), capped."""
    per_language = [[(lang, seed) for seed in plan.query_seeds.get(lang, [])] for lang in plan.languages]
    ordered = [pair for rank in zip_longest(*per_language) for pair in rank if pair]
    return ordered[:max_seeds]


# --- persistence -----------------------------------------------------------------


class DiscoveryStore(Protocol):
    async def claim_profile(self, actor: str) -> BrowserProfile | None: ...
    async def release_profile(self, profile_id: str, state: str, actor: str) -> None: ...
    async def load_progress(self, campaign_id: str) -> dict[str, Any]: ...
    async def save_progress(self, campaign_id: str, progress: dict[str, Any], actor: str) -> None: ...
    async def groups(self, campaign_id: str) -> list[CampaignGroup]: ...
    async def save_group(self, campaign_id: str, group: CampaignGroup, actor: str) -> None: ...
    async def finalize(self, campaign_id: str, *, max_groups: int, window_size: int, actor: str) -> int: ...


_OPEN_STATES = frozenset({"discovered", "rejected"})  # rows discovery may still rewrite


def _finalize_groups(groups: list[CampaignGroup], max_groups: int, window_size: int) -> list[CampaignGroup]:
    """Unconfirmed -> rejected; best ACTIVE -> queued in windows; the rest -> skipped."""
    queued = sum(1 for g in groups if g.state == "queued")
    ranked = sorted(
        (g for g in groups if g.state == "discovered" and g.activity == "ACTIVE"),
        key=lambda g: (-g.relevance_score, g.group_key),
    )
    changes: dict[str, CampaignGroup] = {}
    for group in groups:
        if group.state == "discovered" and group.activity == "UNKNOWN":
            changes[group.group_key] = replace(group, state="rejected", reject_reason="activity_unknown")
    for group in ranked:
        if queued < max_groups:
            changes[group.group_key] = replace(group, state="queued", window_no=queued // window_size + 1)
            queued += 1
        else:
            changes[group.group_key] = replace(group, state="skipped", reject_reason="max_groups_reached")
    return [changes.get(g.group_key, g) for g in groups]


class MemoryDiscoveryStore:
    """In-process twin of ``PostgresDiscoveryStore`` for tests and dry runs."""

    def __init__(self, profile_state: str = "ready") -> None:
        self.profile = BrowserProfile("profile-1", "facebook-main", profile_state)
        self.progress: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, dict[str, CampaignGroup]] = {}
        self.writes: list[tuple[str, str, str]] = []  # (actor, group_key, state)

    async def claim_profile(self, actor: str) -> BrowserProfile | None:
        if self.profile.state != "ready":
            return None
        self.profile = replace(self.profile, state="in_use")
        return self.profile

    async def release_profile(self, profile_id: str, state: str, actor: str) -> None:
        if profile_id == self.profile.id and self.profile.state == "in_use":
            self.profile = replace(self.profile, state=state)

    async def load_progress(self, campaign_id: str) -> dict[str, Any]:
        return dict(self.progress.get(campaign_id, {}))

    async def save_progress(self, campaign_id: str, progress: dict[str, Any], actor: str) -> None:
        self.progress[campaign_id] = dict(progress)

    async def groups(self, campaign_id: str) -> list[CampaignGroup]:
        return list(self.rows.get(campaign_id, {}).values())

    async def save_group(self, campaign_id: str, group: CampaignGroup, actor: str) -> None:
        rows = self.rows.setdefault(campaign_id, {})
        current = rows.get(group.group_key)
        if current is None or current.state in _OPEN_STATES:
            rows[group.group_key] = group
            self.writes.append((actor, group.group_key, group.state))

    async def finalize(self, campaign_id: str, *, max_groups: int, window_size: int, actor: str) -> int:
        rows = self.rows.setdefault(campaign_id, {})
        for group in _finalize_groups(list(rows.values()), max_groups, window_size):
            rows[group.group_key] = group
        return sum(1 for g in rows.values() if g.state == "queued")


class PostgresDiscoveryStore:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self.pool = pool

    async def claim_profile(self, actor: str) -> BrowserProfile | None:
        """Move the oldest ready Facebook profile to in_use, or ``None``.

        At most one Facebook profile may be in use (unique index), so this also
        refuses while the batch collector holds one.
        """
        import asyncpg

        try:
            async with self.pool.acquire() as conn, conn.transaction():
                await _set_actor(conn, actor)
                row = await conn.fetchrow(
                    """update browser_profiles set state = 'in_use', last_used_at = now()
                        where id = (select id from browser_profiles
                                     where platform = 'facebook' and state = 'ready' and deleted_at is null
                                     order by created_at limit 1 for update skip locked)
                        returning id::text, profile_name, state""",
                )
        except asyncpg.UniqueViolationError:
            return None
        return BrowserProfile(row["id"], row["profile_name"], row["state"]) if row else None

    async def release_profile(self, profile_id: str, state: str, actor: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn, actor)
            await conn.execute(
                "update browser_profiles set state = $2 where id = $1::uuid and state = 'in_use'", profile_id, state,
            )

    async def load_progress(self, campaign_id: str) -> dict[str, Any]:
        import json

        raw = await self.pool.fetchval("select discovery_progress::text from campaigns where id = $1::uuid", campaign_id)
        return json.loads(raw) if raw else {}

    async def save_progress(self, campaign_id: str, progress: dict[str, Any], actor: str) -> None:
        import json

        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn, actor)
            await conn.execute(
                "update campaigns set discovery_progress = $2::jsonb where id = $1::uuid", campaign_id, json.dumps(progress),
            )

    async def groups(self, campaign_id: str) -> list[CampaignGroup]:
        import json

        rows = await self.pool.fetch(
            """select group_key, canonical_url, name, language, seed, relevance_score::float8 as relevance_score,
                      activity, activity_evidence::text as activity_evidence, state, reject_reason, window_no
                 from campaign_groups where campaign_id = $1::uuid order by created_at, group_key""",
            campaign_id,
        )
        return [CampaignGroup(
            group_key=r["group_key"], canonical_url=r["canonical_url"], name=r["name"], language=r["language"],
            seed=r["seed"], relevance_score=r["relevance_score"], activity=r["activity"],
            activity_evidence=json.loads(r["activity_evidence"]), state=r["state"],
            reject_reason=r["reject_reason"], window_no=r["window_no"],
        ) for r in rows]

    async def save_group(self, campaign_id: str, group: CampaignGroup, actor: str) -> None:
        """Insert, or rewrite a row discovery still owns; queued/collected/skipped rows are never touched."""
        import json

        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn, actor)
            await conn.execute(
                """insert into campaign_groups (campaign_id, group_key, canonical_url, name, language, seed,
                                                relevance_score, activity, activity_evidence, state, reject_reason)
                   values ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
                   on conflict (campaign_id, group_key) do update set
                       name = excluded.name, relevance_score = excluded.relevance_score,
                       activity = excluded.activity, activity_evidence = excluded.activity_evidence,
                       state = excluded.state, reject_reason = excluded.reject_reason
                   where campaign_groups.state in ('discovered', 'rejected')""",
                campaign_id, group.group_key, group.canonical_url, (group.name or None) and group.name[:200],
                group.language, group.seed, group.relevance_score, group.activity,
                json.dumps(group.activity_evidence), group.state, group.reject_reason,
            )

    async def finalize(self, campaign_id: str, *, max_groups: int, window_size: int, actor: str) -> int:
        async with self.pool.acquire() as conn, conn.transaction():
            await _set_actor(conn, actor)
            await conn.execute(
                """update campaign_groups set state = 'rejected', reject_reason = 'activity_unknown'
                    where campaign_id = $1::uuid and state = 'discovered' and activity = 'UNKNOWN'""",
                campaign_id,
            )
            already = await conn.fetchval(
                "select count(*) from campaign_groups where campaign_id = $1::uuid and state = 'queued'", campaign_id,
            )
            await conn.execute(
                """with ranked as (
                       select id, row_number() over (order by relevance_score desc, group_key) as rank
                         from campaign_groups
                        where campaign_id = $1::uuid and state = 'discovered' and activity = 'ACTIVE')
                   update campaign_groups g
                      set state = case when r.rank <= $2 then 'queued' else 'skipped' end,
                          window_no = case when r.rank <= $2 then ($3 + r.rank - 1) / $4 + 1 end,
                          reject_reason = case when r.rank <= $2 then null else 'max_groups_reached' end
                     from ranked r where g.id = r.id""",
                campaign_id, max(0, max_groups - already), already, window_size,
            )
            return await conn.fetchval(
                "select count(*) from campaign_groups where campaign_id = $1::uuid and state = 'queued'", campaign_id,
            )


async def _set_actor(conn: asyncpg.Connection[asyncpg.Record], actor: str) -> None:
    await conn.execute("select set_config('app.actor', $1, true)", actor)


# --- the run ---------------------------------------------------------------------


class DiscoveryBrowser(Protocol):
    async def acquire(self, profile_id: str, profile_name: str, persisted_state: str, *,
                      platform: str = "facebook") -> BrowserLease: ...
    async def snapshot(self, lease: BrowserLease, url: str, timeout_ms: int) -> dict[str, Any]: ...
    async def release(self, lease: BrowserLease, next_state: str = "READY") -> None: ...


class GroupReader(Protocol):
    async def read(self, lease: BrowserLease, group_url: str) -> GroupRead: ...


@dataclass
class _Run:
    seeds_run: int = 0
    candidates: int = 0
    visits: int = 0
    errors: int = 0
    navigated: bool = False
    budget_exhausted: bool = False
    challenge: str | None = None
    stopped_state: str | None = None  # the campaign left 'discovering' under us


class FacebookDiscovery:
    def __init__(
        self,
        campaigns: CampaignStore,
        store: DiscoveryStore,
        browser: DiscoveryBrowser,
        reader: GroupReader,
        *,
        max_seeds: int = 12,
        max_results_per_seed: int = 15,
        max_visits: int = 10,
        pause_min_seconds: float = 4.0,
        pause_max_seconds: float = 9.0,
        time_budget_seconds: float = 900.0,
        search_timeout_ms: int = 30_000,
        visit_timeout_seconds: float = 90.0,
        max_errors: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (not 1 <= max_seeds <= 24 or not 1 <= max_results_per_seed <= 40 or not 0 <= max_visits <= 20
                or pause_min_seconds < 0 or pause_max_seconds < pause_min_seconds or time_budget_seconds <= 0
                or not 1_000 <= search_timeout_ms <= 60_000 or max_errors < 1):
            raise ValueError("unsafe discovery limits")
        self.campaigns, self.store, self.browser, self.reader = campaigns, store, browser, reader
        self.max_seeds, self.max_results_per_seed, self.max_visits = max_seeds, max_results_per_seed, max_visits
        self.pause_min_seconds, self.pause_max_seconds = pause_min_seconds, pause_max_seconds
        self.time_budget_seconds, self.search_timeout_ms = time_budget_seconds, search_timeout_ms
        self.visit_timeout_seconds, self.max_errors = visit_timeout_seconds, max_errors
        self.sleep, self.clock, self.now = sleep, clock, now

    async def run(self, campaign_id: str) -> DiscoveryReport:
        """Discover, (re)start or resume; raises ``DiscoveryRefused`` without side effects."""
        campaign = await self.campaigns.get(campaign_id)
        if campaign is None:
            raise DiscoveryRefused("campaign_not_found")
        if campaign.state not in RESUMABLE_STATES:
            raise DiscoveryRefused(f"campaign_{campaign.state}")
        profile = await self.store.claim_profile(ACTOR)
        if profile is None:
            raise DiscoveryRefused("facebook_profile_not_ready")
        run = _Run()
        lease: BrowserLease | None = None
        lease_state, profile_state = "READY", "ready"
        try:
            try:
                lease = await self.browser.acquire(profile.id, profile.name, "ready")
            except Exception as exc:
                raise DiscoveryRefused("browser_unavailable") from exc
            if campaign.state != "discovering" and not await self.campaigns.set_state(campaign_id, "discovering", ACTOR):
                raise DiscoveryRefused("campaign_state_changed")
            try:
                await self._discover(campaign, lease, run)
            except ChallengeDetected as challenge:
                # Stop everything; a human clears the checkpoint in the live window.
                run.challenge = challenge.reason
                lease_state, profile_state = "VERIFICATION_REQUIRED", "human_verification_required"
                log.warning("campaign.discovery.challenge", extra={"campaign_id": campaign_id, "reason": challenge.reason})
                await self.campaigns.set_state(campaign_id, "paused_verification", ACTOR,
                                               reason=f"facebook_challenge:{challenge.reason}"[:500])
                return await self._report(campaign_id, run)
            except Exception as exc:
                await self.campaigns.set_state(campaign_id, "failed", ACTOR, reason=f"discovery_error:{type(exc).__name__}")
                raise
            if run.stopped_state is None:
                queued = await self.store.finalize(campaign_id, max_groups=campaign.plan.limits.max_groups,
                                                   window_size=campaign.plan.limits.window_size, actor=ACTOR)
                if queued:
                    await self.campaigns.set_state(campaign_id, "running", ACTOR)
                else:
                    await self.campaigns.set_state(campaign_id, "completed", ACTOR, reason="no_active_groups")
            return await self._report(campaign_id, run)
        finally:
            try:
                if lease is not None:
                    with suppress(Exception):
                        await self.browser.release(lease, lease_state)
            finally:
                await self.store.release_profile(profile.id, profile_state, ACTOR)

    # -- phases --

    async def _discover(self, campaign: Campaign, lease: BrowserLease, run: _Run) -> None:
        plan, cid = campaign.plan, campaign.id
        started = self.clock()
        progress = await self.store.load_progress(cid)
        seeds_done = {(lang, seed) for lang, seed in progress.get("seeds_done", [])}
        visited = set(progress.get("visited", []))
        visits_total = int(progress.get("visits", 0))
        known = {g.group_key: g for g in await self.store.groups(cid)}
        cap = 2 * plan.limits.max_groups

        async def save_progress() -> None:
            await self.store.save_progress(cid, {
                "seeds_done": [list(pair) for pair in sorted(seeds_done)],
                "visited": sorted(visited), "visits": visits_total,
            }, ACTOR)

        for language, seed in plan_seeds(plan, self.max_seeds):
            if (language, seed) in seeds_done:
                continue
            if len(known) >= cap:
                break
            await self._pause(run)
            if not await self._may_continue(cid, started, run):
                return
            try:
                snapshot = await self.browser.snapshot(lease, search_url(seed), self.search_timeout_ms)
            except Exception as exc:
                run.errors += 1
                log.warning("campaign.discovery.search_failed", extra={"campaign_id": cid, "error": type(exc).__name__})
                if run.errors >= self.max_errors:
                    raise DiscoveryError("repeated search failures") from exc
                continue
            reason = detect_challenge(snapshot)
            if reason:
                raise ChallengeDetected(reason, {})
            taken = 0
            links = snapshot.get("group_links")
            for link in links if isinstance(links, list) else []:
                if taken >= self.max_results_per_seed or len(known) >= cap:
                    break
                if not isinstance(link, dict):
                    continue
                parsed = canonical_group(link.get("url"))
                if parsed is None:
                    continue
                taken += 1
                key, url = parsed
                if key in known:
                    continue
                group = self._evaluate(plan, language, seed, key, url, str(link.get("name") or ""),
                                       str(link.get("card") or ""))
                await self.store.save_group(cid, group, ACTOR)
                known[key] = group
                run.candidates += 1
            seeds_done.add((language, seed))
            run.seeds_run += 1
            await save_progress()

        pending = sorted(
            (g for g in known.values()
             if g.state == "discovered" and g.activity == "UNKNOWN" and g.group_key not in visited),
            key=lambda g: (-g.relevance_score, g.group_key),
        )
        for group in pending:
            if visits_total >= self.max_visits:
                break
            await self._pause(run)
            if not await self._may_continue(cid, started, run):
                return
            try:
                read = await asyncio.wait_for(self.reader.read(lease, group.canonical_url), timeout=self.visit_timeout_seconds)
            except ChallengeDetected:
                raise
            except Exception as exc:  # noqa: BLE001 - an unreadable group stays unconfirmed
                activity: Activity = "UNKNOWN"
                evidence: dict[str, Any] = {"source": "visit", "error": type(exc).__name__}
            else:
                activity, evidence = visit_activity(read, now=self.now())
            state, reason = decide(None, activity)
            updated = replace(group, activity=activity, state=state, reject_reason=reason,
                              activity_evidence={**group.activity_evidence, "visit": evidence})
            await self.store.save_group(cid, updated, ACTOR)
            known[group.group_key] = updated
            visited.add(group.group_key)
            visits_total += 1
            run.visits += 1
            await save_progress()

    def _evaluate(self, plan: CampaignPlan, language: str, seed: str, key: str, url: str,
                  name: str, card: str) -> CampaignGroup:
        relevance = score_relevance(name, card, plan, group_key=key)
        activity, evidence = card_activity(card)
        evidence["relevance"] = relevance.matched
        state, reason = decide(relevance.reason, activity)
        return CampaignGroup(
            group_key=key, canonical_url=url, name=name[:200] or None, language=language, seed=seed,
            relevance_score=relevance.score, activity=activity, activity_evidence=evidence,
            state=state, reject_reason=reason,
        )

    async def _may_continue(self, campaign_id: str, started: float, run: _Run) -> bool:
        if self.clock() - started >= self.time_budget_seconds:
            run.budget_exhausted = True
            return False
        current = await self.campaigns.get(campaign_id)
        if current is None or current.state != "discovering":  # cancelled by an operator
            run.stopped_state = current.state if current else "missing"
            return False
        return True

    async def _pause(self, run: _Run) -> None:
        if run.navigated:
            await self.sleep(random.uniform(self.pause_min_seconds, self.pause_max_seconds))
        run.navigated = True

    async def _report(self, campaign_id: str, run: _Run) -> DiscoveryReport:
        campaign = await self.campaigns.get(campaign_id)
        groups = await self.store.groups(campaign_id)
        progress = await self.store.load_progress(campaign_id)
        seeds_total = len(plan_seeds(campaign.plan, self.max_seeds)) if campaign else 0
        return DiscoveryReport(
            campaign_id=campaign_id,
            state=campaign.state if campaign else "missing",
            stop_reason=campaign.stop_reason if campaign else None,
            seeds_total=seeds_total,
            seeds_done=len(progress.get("seeds_done", [])),
            seeds_run=run.seeds_run, candidates=run.candidates, visits=run.visits,
            queued=sum(1 for g in groups if g.state == "queued"),
            rejected=sum(1 for g in groups if g.state == "rejected"),
            challenge=run.challenge, budget_exhausted=run.budget_exhausted,
        )
