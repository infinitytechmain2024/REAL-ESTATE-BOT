-- Investor reach (bot/campaign/reach.py): an investor search also looks for
-- investors, agents, agencies, funds and investor networks on every platform
-- through the search engines (SearXNG, site:linkedin.com/in, site:reddit.com,
-- site:x.com, site:instagram.com, site:tiktok.com, site:youtube.com and the
-- open web). Only the search results are read (link, title, snippet): the
-- platforms themselves are never opened and no account is used. A model sorts
-- each result into a kind; the relevant ones are kept once, globally, in
-- reach_contacts with their city, and every investor search of that city sends
-- them once (campaign_lead_deliveries, key 'reach:<url_key>').

create table if not exists public.campaign_reach (
    campaign_id  uuid primary key references public.campaigns(id) on delete cascade,
    state        text not null default 'running' check (state in ('running', 'done')),
    queries      integer not null default 0 check (queries >= 0),
    current      text check (char_length(current) <= 300),
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);
alter table public.campaign_reach enable row level security;

create table if not exists public.reach_queries (
    campaign_id  uuid not null references public.campaigns(id) on delete cascade,
    query        text not null check (char_length(query) <= 300),
    platform     text not null check (char_length(platform) <= 40),
    hits         integer check (hits >= 0),
    kept         integer check (kept >= 0),
    error_code   text check (char_length(error_code) <= 200),
    created_at   timestamptz not null default now(),
    primary key (campaign_id, query)
);
create index if not exists reach_queries_created_idx on public.reach_queries (created_at);
alter table public.reach_queries enable row level security;

create table if not exists public.reach_contacts (
    id           uuid primary key default gen_random_uuid(),
    url_key      text not null unique check (char_length(url_key) = 64),
    url          text not null check (char_length(url) <= 2000),
    platform     text not null check (char_length(platform) <= 40),
    kind         text not null check (kind in ('investor', 'agent', 'agency', 'fund', 'network', 'developer',
                                               'seeking', 'other')),
    relevant     boolean not null,
    name         text check (char_length(name) <= 200),
    title        text check (char_length(title) <= 300),
    snippet      text check (char_length(snippet) <= 600),
    summary_ru   text check (char_length(summary_ru) <= 600),
    location     text check (char_length(location) <= 200),
    confidence   real check (confidence >= 0 and confidence <= 1),
    judged_by    text not null default 'rules' check (char_length(judged_by) <= 120),
    campaign_id  uuid references public.campaigns(id) on delete set null,
    created_at   timestamptz not null default now()
);
create index if not exists reach_contacts_location_idx on public.reach_contacts (location, created_at desc)
    where relevant;
alter table public.reach_contacts enable row level security;

comment on table public.campaign_reach is 'The reach stage of an investor search: running until its queries are used.';
comment on table public.reach_queries is 'Search-engine queries the reach stage ran for a campaign (each once).';
comment on table public.reach_contacts is
    'Search results about investors, agents, agencies, funds and networks: link, title, snippet, kind, city.';
