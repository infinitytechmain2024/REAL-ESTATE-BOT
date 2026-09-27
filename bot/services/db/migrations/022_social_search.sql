-- Social network search (bot/social_search): the campaign runner goes INTO
-- TikTok, Instagram and LinkedIn with the owner's logged-in browser profile
-- (/login <platform>) and uses each network's own search with AI-generated
-- queries. Adds LinkedIn as a platform, the `search` source kind and the
-- `social_search` acquisition method, and the bookkeeping that keeps every
-- query and every post/profile from being used twice.

create extension if not exists pgcrypto;

-- LinkedIn joins the platform checks of migration 003 (constraint names are
-- PostgreSQL's defaults for the inline checks there).
alter table public.monitoring_sources drop constraint if exists monitoring_sources_platform_check;
alter table public.monitoring_sources add constraint monitoring_sources_platform_check
    check (platform in ('facebook', 'tiktok', 'instagram', 'telegram', 'website', 'linkedin'));
alter table public.browser_profiles drop constraint if exists browser_profiles_platform_check;
alter table public.browser_profiles add constraint browser_profiles_platform_check
    check (platform in ('facebook', 'tiktok', 'instagram', 'telegram', 'website', 'linkedin'));
alter table public.acquisition_batches drop constraint if exists acquisition_batches_platform_check;
alter table public.acquisition_batches add constraint acquisition_batches_platform_check
    check (platform in ('facebook', 'tiktok', 'instagram', 'telegram', 'website', 'linkedin'));

-- One `search` source per platform and vertical holds the posts found by that
-- network's search, so the analysis pipeline reads them like any other source.
alter table public.monitoring_sources drop constraint if exists monitoring_sources_source_kind_check;
alter table public.monitoring_sources add constraint monitoring_sources_source_kind_check
    check (source_kind in ('group', 'account', 'channel', 'hashtag', 'website', 'feed', 'search'));
alter table public.monitoring_sources drop constraint if exists monitoring_sources_acquisition_method_check;
alter table public.monitoring_sources add constraint monitoring_sources_acquisition_method_check
    check (acquisition_method in ('facebook_connector', 'agent_ridge', 'scrapling', 'social_search'));

-- Every query ever run in a network's search. Unique per campaign/platform/kind
-- (a campaign never repeats a query); the (platform, query_key) index lets a
-- new campaign skip queries another campaign ran recently, and started_at
-- counts the per-platform daily cap.
create table if not exists public.social_search_queries (
    id                  uuid primary key default gen_random_uuid(),
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    platform            text not null check (platform in ('tiktok', 'instagram', 'linkedin')),
    kind                text not null check (kind in ('keyword', 'hashtag', 'posts', 'people', 'companies')),
    query               text not null check (length(query) between 1 and 120),
    query_key           text not null check (length(query_key) between 1 and 120),
    language            text check (language is null or language in ('es', 'en', 'ru', 'uk')),
    round_no            smallint not null default 1 check (round_no between 1 and 50),
    state               text not null default 'planned' check (state in ('planned', 'running', 'done', 'failed', 'skipped')),
    items_found         integer not null default 0 check (items_found >= 0),
    items_new           integer not null default 0 check (items_new >= 0),
    error_code          text check (error_code is null or length(error_code) <= 200),
    browser_profile_id  uuid references public.browser_profiles(id) on delete set null,
    created_at          timestamptz not null default now(),
    started_at          timestamptz,
    finished_at         timestamptz,
    constraint social_search_queries_unique unique (campaign_id, platform, kind, query_key)
);
create index if not exists social_search_queries_global_idx
    on public.social_search_queries (platform, kind, query_key, started_at desc);
create index if not exists social_search_queries_daily_idx
    on public.social_search_queries (platform, started_at) where started_at is not null;
create index if not exists social_search_queries_work_idx
    on public.social_search_queries (campaign_id, platform, state);

-- Every post or profile a search showed, by its normalised id, across all
-- campaigns: an item seen once is never collected or analysed again.
create table if not exists public.social_seen_items (
    platform            text not null check (platform in ('tiktok', 'instagram', 'linkedin')),
    item_key            text not null check (length(item_key) between 1 and 200),
    canonical_url       text not null,
    first_campaign_id   uuid references public.campaigns(id) on delete set null,
    first_query_id      uuid references public.social_search_queries(id) on delete set null,
    post_id             uuid references public.collected_posts(id) on delete set null,
    seen_count          integer not null default 1 check (seen_count >= 1),
    first_seen_at       timestamptz not null default now(),
    last_seen_at        timestamptz not null default now(),
    primary key (platform, item_key)
);
create index if not exists social_seen_items_url_idx on public.social_seen_items (canonical_url);

-- Which campaign a social post belongs to: the equivalent of
-- acquisition_runs -> batch items -> acquisition_batches.campaign_id for
-- Facebook, so the campaign streams its findings the same way.
create table if not exists public.campaign_social_posts (
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    post_id             uuid not null references public.collected_posts(id) on delete cascade,
    platform            text not null check (platform in ('tiktok', 'instagram', 'linkedin')),
    query_id            uuid references public.social_search_queries(id) on delete set null,
    created_at          timestamptz not null default now(),
    primary key (campaign_id, post_id)
);
create index if not exists campaign_social_posts_post_idx on public.campaign_social_posts (post_id);

-- Per campaign and platform: pending (more rounds to run), running (a query is
-- in the browser now), waiting (no ready profile: owner note only), done.
create table if not exists public.campaign_social_state (
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    platform            text not null check (platform in ('tiktok', 'instagram', 'linkedin')),
    state               text not null default 'pending' check (state in ('pending', 'running', 'waiting', 'done')),
    rounds              smallint not null default 0 check (rounds between 0 and 50),
    current_query       text check (current_query is null or length(current_query) <= 120),
    note                text check (note is null or length(note) <= 300),
    updated_at          timestamptz not null default now(),
    primary key (campaign_id, platform)
);

-- Per platform: when the next browser action may happen (pause with jitter)
-- and a cooldown after a rate limit.
create table if not exists public.social_platform_state (
    platform            text primary key check (platform in ('tiktok', 'instagram', 'linkedin')),
    next_action_at      timestamptz,
    paused_until        timestamptz,
    reason              text check (reason is null or length(reason) <= 200),
    updated_at          timestamptz not null default now()
);

alter table public.social_search_queries enable row level security;
alter table public.social_seen_items enable row level security;
alter table public.campaign_social_posts enable row level security;
alter table public.campaign_social_state enable row level security;
alter table public.social_platform_state enable row level security;

comment on table public.social_search_queries is
    'Queries run in a social network''s own search for a campaign; never repeated within a campaign.';
comment on table public.social_seen_items is
    'Every post/profile a social search returned, by normalised id, across campaigns: collected at most once.';
comment on table public.campaign_social_posts is
    'Links a collected social post to the campaign whose query found it (streams like a Facebook batch post).';
comment on table public.campaign_social_state is
    'Social search progress per campaign and platform; the note is technical and shown to owners only.';
comment on table public.social_platform_state is
    'Per-platform pacing (next allowed action) and rate-limit cooldown for social search.';
