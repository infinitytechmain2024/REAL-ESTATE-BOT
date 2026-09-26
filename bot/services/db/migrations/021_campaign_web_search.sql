-- Website search stage of campaigns (bot/web_search), beside the Facebook stage.
--
-- AI-generated queries go to the internal SearXNG; the public pages they find
-- are read once and stored as ordinary collected_posts, so the existing
-- analysis worker analyses them and the campaign runner streams the findings.
--
-- Linkage (no change to existing tables): each site (normalised host) is one
-- monitoring_source (platform 'website', method 'scrapling',
-- canonical_url 'https://<host>/'); a campaign's web reads are ordinary
-- acquisition_batches (platform 'website', campaign_id set, at most 20 sites
-- each), one acquisition_batch_item per site, and one acquisition_run per page
-- (created 'running', never 'queued', so scrapling-worker never picks it up).
-- collected_posts.acquisition_run_id -> batch item -> batch.campaign_id is the
-- same path PostgresRunStore.unstreamed_findings/pending_analysis already use.
--
-- De-duplication:
--   web_seen_urls      one row per page ever claimed, keyed by url_key (SHA-256
--                      of the normalised URL): global across campaigns, so a
--                      page is fetched at most once, ever.
--   web_campaign_urls  one row per (campaign, url_key): a campaign's queue and
--                      the audit of what happened to each URL it met.
--   web_hosts          one row per site: its source, counters and a temporary
--                      block after repeated refusals (403/429).
--   web_search_queries one row per (campaign, query_key): queries are never
--                      repeated within a campaign, and a query another campaign
--                      searched recently is skipped.

create extension if not exists pgcrypto;

-- The web stage of one campaign: state, rounds, the site being read, and a
-- lease so one worker at a time drives it (a crashed worker's lease expires).
create table if not exists public.web_search_runs (
    campaign_id         uuid primary key references public.campaigns(id) on delete cascade,
    state               text not null default 'searching' check (state in ('searching', 'done', 'stopped')),
    stop_reason         text check (stop_reason is null or length(stop_reason) <= 200),
    rounds              integer not null default 0 check (rounds >= 0),
    current_host        text check (current_host is null or length(current_host) <= 253),
    progress            text check (progress is null or length(progress) <= 500),
    lease_token         uuid,
    lease_until         timestamptz,
    started_at          timestamptz not null default now(),
    finished_at         timestamptz,
    updated_at          timestamptz not null default now()
);

create table if not exists public.web_search_queries (
    id                  uuid primary key default gen_random_uuid(),
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    round_no            integer not null check (round_no >= 1),
    query_text          text not null check (length(query_text) between 1 and 200),
    -- sorted meaning tokens (bot/web_search/queries.py query_key)
    query_key           text not null check (length(query_key) between 1 and 400),
    language            text check (language is null or language in ('es', 'en', 'ru', 'uk')),
    state               text not null default 'pending' check (state in ('pending', 'searched', 'failed', 'skipped')),
    result_count        integer check (result_count is null or result_count >= 0),
    new_urls            integer check (new_urls is null or new_urls >= 0),
    error_code          text check (error_code is null or length(error_code) <= 80),
    created_at          timestamptz not null default now(),
    searched_at         timestamptz,
    constraint web_search_queries_campaign_key unique (campaign_id, query_key)
);
create index if not exists web_search_queries_pending_idx
    on public.web_search_queries (campaign_id, created_at) where state = 'pending';
create index if not exists web_search_queries_recent_idx
    on public.web_search_queries (query_key, searched_at) where state = 'searched';
create index if not exists web_search_queries_daily_idx
    on public.web_search_queries (searched_at) where searched_at is not null;

create table if not exists public.web_hosts (
    host                text primary key check (host ~ '^[a-z0-9][a-z0-9.-]{0,252}$'),
    source_id           uuid references public.monitoring_sources(id) on delete set null,
    pages_fetched       integer not null default 0 check (pages_fetched >= 0),
    pages_failed        integer not null default 0 check (pages_failed >= 0),
    consecutive_refusals integer not null default 0 check (consecutive_refusals >= 0),
    blocked_until       timestamptz,
    first_seen_at       timestamptz not null default now(),
    last_fetch_at       timestamptz
);

create table if not exists public.web_seen_urls (
    url_key             text primary key check (url_key ~ '^[0-9a-f]{64}$'),
    url                 text not null check (length(url) between 1 and 2048),
    host                text not null check (length(host) between 1 and 253),
    kind                text check (kind is null or kind in ('listing', 'index', 'unknown')),
    state               text not null default 'fetching' check (state in ('fetching', 'fetched', 'failed')),
    -- the campaign that claimed (and fetched) it first
    campaign_id         uuid references public.campaigns(id) on delete set null,
    post_id             uuid references public.collected_posts(id) on delete set null,
    error_code          text check (error_code is null or length(error_code) <= 80),
    claimed_at          timestamptz not null default now(),
    finished_at         timestamptz,
    created_at          timestamptz not null default now()
);
create index if not exists web_seen_urls_daily_idx
    on public.web_seen_urls (finished_at) where finished_at is not null;
create index if not exists web_seen_urls_host_idx on public.web_seen_urls (host);

create table if not exists public.web_campaign_urls (
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    url_key             text not null check (url_key ~ '^[0-9a-f]{64}$'),
    url                 text not null check (length(url) between 1 and 2048),
    host                text not null check (length(host) between 1 and 253),
    -- 0: a search result; 1: a listing found on an index page (never expanded further)
    depth               smallint not null default 0 check (depth between 0 and 1),
    kind                text not null default 'unknown' check (kind in ('listing', 'index', 'unknown')),
    query_id            uuid references public.web_search_queries(id) on delete set null,
    state               text not null default 'queued' check (state in
                            ('queued', 'fetched', 'failed', 'duplicate', 'capped', 'robots', 'skipped')),
    detail              text check (detail is null or length(detail) <= 80),
    created_at          timestamptz not null default now(),
    finished_at         timestamptz,
    primary key (campaign_id, url_key)
);
create index if not exists web_campaign_urls_queue_idx
    on public.web_campaign_urls (campaign_id, depth, created_at) where state = 'queued';
create index if not exists web_campaign_urls_host_idx
    on public.web_campaign_urls (campaign_id, host, state);

alter table public.web_search_runs enable row level security;
alter table public.web_search_queries enable row level security;
alter table public.web_hosts enable row level security;
alter table public.web_seen_urls enable row level security;
alter table public.web_campaign_urls enable row level security;

comment on table public.web_search_runs is
    'Website search stage of a campaign: state, rounds, current site and the worker lease.';
comment on table public.web_search_queries is
    'Search queries of a campaign, unique by meaning (query_key); a query searched recently by any campaign is skipped.';
comment on table public.web_hosts is
    'Every site the web stage met: its monitoring source, counters and a temporary block after repeated refusals.';
comment on table public.web_seen_urls is
    'Global page registry: a url_key is claimed once and never fetched again, by any campaign.';
comment on table public.web_campaign_urls is
    'Per-campaign URL queue and audit: queued, fetched, failed, duplicate (seen elsewhere), capped, robots, skipped.';
