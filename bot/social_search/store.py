"""Durable state of social search (migration 022), PostgreSQL and an in-memory twin for tests.

* ``social_search_queries``: every query a campaign ran on a platform; the
  unique key stops a campaign from repeating one, ``used_queries`` also
  returns queries other campaigns ran recently, and ``started_at`` counts the
  per-platform daily cap.
* ``social_seen_items``: every post/profile a search showed, globally. Only
  the first sighting is collected; later ones (this or any other campaign)
  just count. A URL Facebook or another source already collected is skipped too.
* ``campaign_social_posts``: the campaign a collected post belongs to (the
  equivalent of a Facebook batch's ``campaign_id``), read by the runner's
  findings stream.
* ``campaign_social_state`` / ``social_platform_state``: progress per
  campaign and platform, and pacing/cooldown per platform.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from .adapters import PLATFORM_NAMES, Block, SocialItem
from .queries import SocialQuery

if TYPE_CHECKING:
    import asyncpg

ACTOR = "campaign:social"
TERMINAL_CAMPAIGN_STATES = ("completed", "cancelled", "failed")


@dataclass(frozen=True, slots=True)
class PlannedQuery:
    id: str
    platform: str
    kind: str
    text: str
    round_no: int


@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class CampaignSocial:
    state: str = "pending"
    rounds: int = 0


@dataclass(frozen=True, slots=True)
class Pacing:
    next_action_at: datetime | None = None
    paused_until: datetime | None = None


def content_hash(item: SocialItem) -> str:
    return hashlib.sha256(f"{item.url}\n{item.text}".encode()).hexdigest()


def source_url(platform: str, vertical: str) -> str:
    """The one `search` source per platform and vertical that holds the posts its searches found.

    It is the network's signed-in home page (the verification flow opens a
    source's URL in the live browser); the fragment keeps one source per vertical.
    """
    home = {"tiktok": "www.tiktok.com/", "instagram": "www.instagram.com/", "linkedin": "www.linkedin.com/feed/"}[platform]
    return f"https://{home}#bot-social-search-{vertical}"


def item_title(item: SocialItem) -> str:
    who = f" · {item.author}" if item.author else ""
    kind = {"post": "", "person": " · профиль", "company": " · компания"}[item.kind]
    return f"{PLATFORM_NAMES.get(item.platform, item.platform)}{kind}{who}"[:300]


class SocialStore(Protocol):
    async def recover(self) -> int: ...
    async def open_campaigns(self) -> list[str]: ...
    async def campaign_social(self, campaign_id: str, platform: str) -> CampaignSocial: ...
    async def set_campaign_social(self, campaign_id: str, platform: str, state: str, *, rounds: int | None = None,
                                  current_query: str | None = None, note: str | None = None,
                                  current_url: str | None = None) -> None: ...
    async def pacing(self, platform: str) -> Pacing: ...
    async def set_pacing(self, platform: str, *, next_action_at: datetime | None = None,
                         paused_until: datetime | None = None, reason: str | None = None) -> None: ...
    async def queries_today(self, platform: str) -> int: ...
    async def used_queries(self, campaign_id: str, platform: str, reuse_days: int) -> list[tuple[str, str, str]]: ...
    async def add_queries(self, campaign_id: str, platform: str, round_no: int, queries: list[SocialQuery]) -> int: ...
    async def next_query(self, campaign_id: str, platform: str) -> PlannedQuery | None: ...
    async def ready_profile(self, platform: str) -> Profile | None: ...
    async def claim_profile(self, profile_id: str) -> bool: ...
    async def release_profile(self, profile_id: str) -> None: ...
    async def start_query(self, query_id: str, profile_id: str) -> None: ...
    async def finish_query(self, query_id: str, state: str, *, found: int = 0, new: int = 0, error: str | None = None) -> None: ...
    async def seen(self, platform: str, items: list[SocialItem]) -> set[str]: ...
    async def save_item(self, campaign_id: str, vertical: str, query_id: str, item: SocialItem) -> bool: ...
    async def challenge(self, platform: str, profile_id: str, query_id: str, block: Block, vertical: str) -> None: ...


# --- PostgreSQL -----------------------------------------------------------------------------------


async def _actor(conn: asyncpg.Connection[asyncpg.Record]) -> None:
    await conn.execute("select set_config('app.actor', $1, true)", ACTOR)


async def _source(conn: asyncpg.Connection[asyncpg.Record], platform: str, vertical: str) -> str:
    url = source_url(platform, vertical)
    await conn.execute(
        """insert into monitoring_sources (platform, source_kind, vertical, canonical_url, display_name,
                                           acquisition_method, state)
           values ($1, 'search', $2, $3, $4, 'social_search', 'active')
           on conflict (platform, canonical_url) do nothing""",
        platform, vertical, url, f"{PLATFORM_NAMES[platform]} search ({vertical})",
    )
    return str(await conn.fetchval(
        "select id from monitoring_sources where platform = $1 and canonical_url = $2", platform, url))


class PostgresSocialStore:
    def __init__(self, pool: asyncpg.Pool[asyncpg.Record]) -> None:
        self.pool = pool

    async def recover(self) -> int:
        """Startup: a query left running by a crash failed; its profile (still in_use) is ready again."""
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            rows = await conn.fetch(
                """update social_search_queries set state = 'failed', finished_at = now(), error_code = 'worker_restarted'
                    where state = 'running' returning browser_profile_id""")
            profiles = [r["browser_profile_id"] for r in rows if r["browser_profile_id"] is not None]
            if profiles:
                await conn.execute(
                    "update browser_profiles set state = 'ready' where id = any($1::uuid[]) and state = 'in_use'", profiles)
            await conn.execute("update campaign_social_state set state = 'pending', current_query = null where state = 'running'")
        return len(rows)

    async def open_campaigns(self) -> list[str]:
        rows = await self.pool.fetch(
            "select id::text from campaigns where state <> all($1::text[]) order by created_at",
            list(TERMINAL_CAMPAIGN_STATES))
        return [r["id"] for r in rows]

    async def campaign_social(self, campaign_id: str, platform: str) -> CampaignSocial:
        row = await self.pool.fetchrow(
            "select state, rounds from campaign_social_state where campaign_id = $1::uuid and platform = $2",
            campaign_id, platform)
        return CampaignSocial(row["state"], row["rounds"]) if row else CampaignSocial()

    async def set_campaign_social(self, campaign_id: str, platform: str, state: str, *, rounds: int | None = None,
                                  current_query: str | None = None, note: str | None = None,
                                  current_url: str | None = None) -> None:
        await self.pool.execute(
            """insert into campaign_social_state (campaign_id, platform, state, rounds, current_query, note, current_url)
               values ($1::uuid, $2, $3, coalesce($4, 0), $5, $6, $7)
               on conflict (campaign_id, platform) do update set state = excluded.state,
                   rounds = coalesce($4, campaign_social_state.rounds), current_query = excluded.current_query,
                   note = excluded.note, current_url = excluded.current_url, updated_at = now()""",
            campaign_id, platform, state, rounds, current_query and current_query[:120], note and note[:300],
            current_url and current_url[:2000],
        )

    async def pacing(self, platform: str) -> Pacing:
        row = await self.pool.fetchrow(
            "select next_action_at, paused_until from social_platform_state where platform = $1", platform)
        return Pacing(row["next_action_at"], row["paused_until"]) if row else Pacing()

    async def set_pacing(self, platform: str, *, next_action_at: datetime | None = None,
                         paused_until: datetime | None = None, reason: str | None = None) -> None:
        await self.pool.execute(
            """insert into social_platform_state (platform, next_action_at, paused_until, reason)
               values ($1, $2, $3, $4)
               on conflict (platform) do update set
                   next_action_at = coalesce(excluded.next_action_at, social_platform_state.next_action_at),
                   paused_until = coalesce(excluded.paused_until, social_platform_state.paused_until),
                   reason = coalesce(excluded.reason, social_platform_state.reason), updated_at = now()""",
            platform, next_action_at, paused_until, reason and reason[:200],
        )

    async def queries_today(self, platform: str) -> int:
        return int(await self.pool.fetchval(
            """select count(*) from social_search_queries
                where platform = $1 and started_at > now() - interval '24 hours'""", platform))

    async def used_queries(self, campaign_id: str, platform: str, reuse_days: int) -> list[tuple[str, str, str]]:
        rows = await self.pool.fetch(
            """select distinct on (kind, query_key) kind, query_key, query from social_search_queries
                where platform = $2 and (campaign_id = $1::uuid
                      or (started_at is not null and started_at > now() - make_interval(days => $3)))
                order by kind, query_key, created_at""",
            campaign_id, platform, reuse_days)
        return [(r["kind"], r["query_key"], r["query"]) for r in rows]

    async def add_queries(self, campaign_id: str, platform: str, round_no: int, queries: list[SocialQuery]) -> int:
        added = 0
        async with self.pool.acquire() as conn, conn.transaction():
            for query in queries:
                inserted = await conn.fetchval(
                    """insert into social_search_queries (campaign_id, platform, kind, query, query_key, language, round_no, created_at)
                       values ($1::uuid, $2, $3, $4, $5, $6, $7, clock_timestamp()) on conflict do nothing returning 1""",
                    campaign_id, platform, query.kind, query.text, query.key, query.language, round_no)
                added += bool(inserted)
        return added

    async def next_query(self, campaign_id: str, platform: str) -> PlannedQuery | None:
        row = await self.pool.fetchrow(
            """select id::text, kind, query, round_no from social_search_queries
                where campaign_id = $1::uuid and platform = $2 and state = 'planned'
                order by round_no, created_at, id limit 1""", campaign_id, platform)
        return PlannedQuery(row["id"], platform, row["kind"], row["query"], row["round_no"]) if row else None

    async def ready_profile(self, platform: str) -> Profile | None:
        row = await self.pool.fetchrow(
            """select id::text, profile_name from browser_profiles
                where platform = $1 and state = 'ready' and deleted_at is null
                order by last_used_at nulls first, created_at limit 1""", platform)
        return Profile(row["id"], row["profile_name"]) if row else None

    async def claim_profile(self, profile_id: str) -> bool:
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            status = await conn.execute(
                "update browser_profiles set state = 'in_use', last_used_at = now() where id = $1::uuid and state = 'ready'",
                profile_id)
        return status.endswith(" 1")

    async def release_profile(self, profile_id: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            await conn.execute("update browser_profiles set state = 'ready' where id = $1::uuid and state = 'in_use'", profile_id)

    async def start_query(self, query_id: str, profile_id: str) -> None:
        await self.pool.execute(
            """update social_search_queries set state = 'running', started_at = now(), browser_profile_id = $2::uuid
                where id = $1::uuid""", query_id, profile_id)

    async def finish_query(self, query_id: str, state: str, *, found: int = 0, new: int = 0, error: str | None = None) -> None:
        """``planned`` gives a query that never reached the network back (it does not count toward the daily cap)."""
        await self.pool.execute(
            """update social_search_queries set state = $2, items_found = $3, items_new = $4, error_code = $5,
                   finished_at = case when $2 = 'planned' then null else now() end,
                   started_at = case when $2 = 'planned' then null else started_at end,
                   browser_profile_id = case when $2 = 'planned' then null else browser_profile_id end
                where id = $1::uuid""",
            query_id, state, found, new, error and error[:200])

    async def seen(self, platform: str, items: list[SocialItem]) -> set[str]:
        if not items:
            return set()
        rows = await self.pool.fetch(
            """select item_key from social_seen_items where platform = $1 and item_key = any($2::text[])
               union
               select s.item_key from unnest($2::text[], $3::text[]) as s(item_key, url)
                where exists (select 1 from collected_posts p where p.canonical_url = s.url)""",
            platform, [i.key for i in items], [i.url for i in items])
        return {r["item_key"] for r in rows}

    async def save_item(self, campaign_id: str, vertical: str, query_id: str, item: SocialItem) -> bool:
        """Collect a first sighting (True); a repeat, here or in any campaign, only counts (False)."""
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            first = await conn.fetchval(
                """insert into social_seen_items (platform, item_key, canonical_url, first_campaign_id, first_query_id)
                   values ($1, $2, $3, $4::uuid, $5::uuid)
                   on conflict (platform, item_key) do update
                       set seen_count = social_seen_items.seen_count + 1, last_seen_at = now()
                   returning (xmax = 0)""",
                item.platform, item.key, item.url, campaign_id, query_id)
            if not first or await conn.fetchval("select 1 from collected_posts where canonical_url = $1 limit 1", item.url):
                return False
            source_id = await _source(conn, item.platform, vertical)
            payload = {"title": item_title(item), "platform": item.platform, "kind": item.kind, "query_id": query_id}
            post_id = await conn.fetchval(
                """insert into collected_posts (source_id, platform_post_id, canonical_url, author_handle, published_at,
                                                body_text, raw_payload, content_hash, state)
                   values ($1::uuid, $2, $3, $4, $5, $6, $7::jsonb, $8, 'normalised')
                   on conflict do nothing returning id""",
                source_id, item.key, item.url, item.author, item.published_at, item.text, json.dumps(payload),
                content_hash(item))
            if post_id is None:
                return False
            await conn.execute("update social_seen_items set post_id = $3 where platform = $1 and item_key = $2",
                               item.platform, item.key, post_id)
            await conn.execute(
                """insert into campaign_social_posts (campaign_id, post_id, platform, query_id)
                   values ($1::uuid, $2, $3, $4::uuid) on conflict do nothing""",
                campaign_id, post_id, item.platform, query_id)
            return True

    async def challenge(self, platform: str, profile_id: str, query_id: str, block: Block, vertical: str) -> None:
        """The profile needs a human: the existing verification flow (live view / verification service) asks the owner."""
        async with self.pool.acquire() as conn, conn.transaction():
            await _actor(conn)
            await conn.execute(
                """update social_search_queries set state = 'failed', finished_at = now(), error_code = $2
                    where id = $1::uuid""", query_id, f"blocked:{block.reason}"[:200])
            await conn.execute(
                "update browser_profiles set state = 'human_verification_required' where id = $1::uuid and state in ('in_use', 'ready')",
                profile_id)
            source_id = await _source(conn, platform, vertical)
            await conn.execute(
                """insert into verification_jobs (source_id, job_type, state, requested_by, resolution_note,
                                                  browser_profile_id, challenge_kind)
                   values ($1::uuid, 'login', 'requested', $2, $3, $4::uuid, $5)
                   on conflict (source_id, job_type) where state in ('requested', 'active') do nothing""",
                source_id, ACTOR, block.reason[:500], profile_id, block.kind if block.kind != "rate_limit" else "unknown")


# --- in memory ------------------------------------------------------------------------------------


@dataclass
class _MemQuery:
    id: str
    campaign_id: str
    platform: str
    query: SocialQuery
    round_no: int
    state: str = "planned"
    started_at: datetime | None = None
    found: int = 0
    new: int = 0
    error: str | None = None
    profile_id: str | None = None


@dataclass
class MemorySocialStore:
    """Test double with the rules of the PostgreSQL store."""

    now: Any = field(default=lambda: datetime.now(UTC))
    campaigns: dict[str, str] = field(default_factory=dict)  # campaign id -> campaign state
    profiles: dict[str, dict[str, str]] = field(default_factory=dict)  # id -> {platform, name, state}
    queries: list[_MemQuery] = field(default_factory=list)
    seen_items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    posts: dict[str, dict[str, Any]] = field(default_factory=dict)  # post id -> row
    links: set[tuple[str, str]] = field(default_factory=set)  # (campaign, post)
    social: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    platforms: dict[str, Pacing] = field(default_factory=dict)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    external_urls: set[str] = field(default_factory=set)  # URLs collected by another source (e.g. Facebook)

    def add_profile(self, platform: str, state: str = "ready", name: str | None = None) -> str:
        profile_id = f"profile-{platform}-{len(self.profiles) + 1}"
        self.profiles[profile_id] = {"platform": platform, "name": name or f"{platform}-main", "state": state}
        return profile_id

    async def recover(self) -> int:
        count = 0
        for query in self.queries:
            if query.state == "running":
                query.state, query.error, count = "failed", "worker_restarted", count + 1
                if query.profile_id and self.profiles[query.profile_id]["state"] == "in_use":
                    self.profiles[query.profile_id]["state"] = "ready"
        return count

    async def open_campaigns(self) -> list[str]:
        return [cid for cid, state in self.campaigns.items() if state not in TERMINAL_CAMPAIGN_STATES]

    async def campaign_social(self, campaign_id: str, platform: str) -> CampaignSocial:
        row = self.social.get((campaign_id, platform))
        return CampaignSocial(row["state"], row["rounds"]) if row else CampaignSocial()

    async def set_campaign_social(self, campaign_id: str, platform: str, state: str, *, rounds: int | None = None,
                                  current_query: str | None = None, note: str | None = None,
                                  current_url: str | None = None) -> None:
        row = self.social.setdefault((campaign_id, platform), {"rounds": 0})
        row.update(state=state, current_query=current_query, note=note, current_url=current_url)
        if rounds is not None:
            row["rounds"] = rounds

    async def pacing(self, platform: str) -> Pacing:
        return self.platforms.get(platform, Pacing())

    async def set_pacing(self, platform: str, *, next_action_at: datetime | None = None,
                         paused_until: datetime | None = None, reason: str | None = None) -> None:
        current = self.platforms.get(platform, Pacing())
        self.platforms[platform] = replace(current, next_action_at=next_action_at or current.next_action_at,
                                           paused_until=paused_until or current.paused_until)

    async def queries_today(self, platform: str) -> int:
        since = self.now() - timedelta(hours=24)
        return sum(1 for q in self.queries if q.platform == platform and q.started_at and q.started_at > since)

    async def used_queries(self, campaign_id: str, platform: str, reuse_days: int) -> list[tuple[str, str, str]]:
        since = self.now() - timedelta(days=reuse_days)
        out: dict[tuple[str, str], str] = {}
        for q in self.queries:
            if q.platform == platform and (q.campaign_id == campaign_id or (q.started_at and q.started_at > since)):
                out.setdefault((q.query.kind, q.query.key), q.query.text)
        return [(kind, key, text) for (kind, key), text in out.items()]

    async def add_queries(self, campaign_id: str, platform: str, round_no: int, queries: list[SocialQuery]) -> int:
        added = 0
        for query in queries:
            if any(q.campaign_id == campaign_id and q.platform == platform and (q.query.kind, q.query.key) == (query.kind, query.key)
                   for q in self.queries):
                continue
            self.queries.append(_MemQuery(f"q{len(self.queries) + 1}", campaign_id, platform, query, round_no))
            added += 1
        return added

    async def next_query(self, campaign_id: str, platform: str) -> PlannedQuery | None:
        for q in self.queries:
            if q.campaign_id == campaign_id and q.platform == platform and q.state == "planned":
                return PlannedQuery(q.id, platform, q.query.kind, q.query.text, q.round_no)
        return None

    async def ready_profile(self, platform: str) -> Profile | None:
        for pid, row in self.profiles.items():
            if row["platform"] == platform and row["state"] == "ready":
                return Profile(pid, row["name"])
        return None

    async def claim_profile(self, profile_id: str) -> bool:
        row = self.profiles[profile_id]
        if row["state"] != "ready" or any(
                r["platform"] == row["platform"] and r["state"] == "in_use" for r in self.profiles.values()):
            return False
        row["state"] = "in_use"
        return True

    async def release_profile(self, profile_id: str) -> None:
        if self.profiles[profile_id]["state"] == "in_use":
            self.profiles[profile_id]["state"] = "ready"

    def _query(self, query_id: str) -> _MemQuery:
        return next(q for q in self.queries if q.id == query_id)

    async def start_query(self, query_id: str, profile_id: str) -> None:
        query = self._query(query_id)
        query.state, query.started_at, query.profile_id = "running", self.now(), profile_id

    async def finish_query(self, query_id: str, state: str, *, found: int = 0, new: int = 0, error: str | None = None) -> None:
        query = self._query(query_id)
        query.state, query.found, query.new, query.error = state, found, new, error
        if state == "planned":
            query.started_at = query.profile_id = None

    async def seen(self, platform: str, items: list[SocialItem]) -> set[str]:
        urls = {p["canonical_url"] for p in self.posts.values()} | self.external_urls
        return {i.key for i in items if (platform, i.key) in self.seen_items or i.url in urls}

    async def save_item(self, campaign_id: str, vertical: str, query_id: str, item: SocialItem) -> bool:
        known = self.seen_items.get((item.platform, item.key))
        if known is not None:
            known["seen_count"] += 1
            return False
        self.seen_items[(item.platform, item.key)] = {"url": item.url, "campaign_id": campaign_id, "seen_count": 1}
        if item.url in self.external_urls or any(p["canonical_url"] == item.url for p in self.posts.values()):
            return False
        post_id = f"post{len(self.posts) + 1}"
        self.posts[post_id] = {"canonical_url": item.url, "body_text": item.text, "vertical": vertical,
                               "platform": item.platform, "title": item_title(item), "author": item.author,
                               "published_at": item.published_at}
        self.links.add((campaign_id, post_id))
        return True

    async def challenge(self, platform: str, profile_id: str, query_id: str, block: Block, vertical: str) -> None:
        query = self._query(query_id)
        query.state, query.error = "failed", f"blocked:{block.reason}"
        self.profiles[profile_id]["state"] = "human_verification_required"
        if not any(j["platform"] == platform and j["state"] == "requested" for j in self.jobs):
            self.jobs.append({"platform": platform, "profile_id": profile_id, "kind": block.kind, "state": "requested"})
