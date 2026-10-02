-- Search sites the person approved or rejected (bot/campaign/sites.py).
--
-- Before the web stage reads any page of a site it has not seen for this
-- person, the bot sends the list of new sites of the task, numbered, with the
-- query each came from, and asks «Все сайты подтверждены?». The answer is
-- kept per person and mode (search_sites): an approved site is read in later
-- searches without asking again and is searched first (site:<host>); a
-- rejected one is never offered or read again for that person. Only the
-- sites themselves are kept, never the agencies or listings found on them.
-- Facebook groups have their own stage and are never part of this list.

create table if not exists public.search_sites (
    user_id        bigint not null,
    host           text not null check (host ~ '^[a-z0-9][a-z0-9.-]{0,252}$'),
    vertical       text not null check (vertical in ('real_estate', 'investors', 'both')),
    status         text not null check (status in ('approved', 'rejected')),
    location       text check (char_length(location) <= 80),
    country        text check (country ~ '^[A-Z]{2}$'),
    query_text     text check (char_length(query_text) <= 200),
    language       text check (char_length(language) <= 10),
    campaign_id    uuid references public.campaigns(id) on delete set null,
    created_at     timestamptz not null default now(),
    decided_at     timestamptz not null default now(),
    last_used_at   timestamptz,
    primary key (user_id, host, vertical)
);
create index if not exists search_sites_approved_idx
    on public.search_sites (user_id, vertical, last_used_at desc nulls last) where status = 'approved';
alter table public.search_sites enable row level security;

-- The sites of one campaign: their number in the list, the query they came
-- from and the decision (pending until the person answers; 'saved' when an
-- earlier decision of the person applied).
create table if not exists public.campaign_sites (
    campaign_id    uuid not null references public.campaigns(id) on delete cascade,
    host           text not null check (host ~ '^[a-z0-9][a-z0-9.-]{0,252}$'),
    number         smallint check (number between 1 and 999),
    batch          smallint not null default 0 check (batch between 0 and 20),
    query_text     text check (char_length(query_text) <= 200),
    language       text check (char_length(language) <= 10),
    state          text not null default 'pending' check (state in ('pending', 'approved', 'rejected')),
    source         text not null default 'asked' check (source in ('asked', 'saved', 'unasked')),
    created_at     timestamptz not null default now(),
    decided_at     timestamptz,
    primary key (campaign_id, host)
);
create unique index if not exists campaign_sites_number_idx
    on public.campaign_sites (campaign_id, number) where number is not null;
alter table public.campaign_sites enable row level security;

-- One question per batch of new sites: asked (message ids kept to answer under
-- it), reminded once, answered.
create table if not exists public.campaign_site_questions (
    campaign_id    uuid not null references public.campaigns(id) on delete cascade,
    batch          smallint not null check (batch between 1 and 20),
    state          text not null default 'asked' check (state in ('asked', 'answered')),
    chat_id        bigint not null,
    requested_by   bigint not null,
    message_ids    bigint[] not null default '{}',
    asked_at       timestamptz not null default now(),
    reminded_at    timestamptz,
    answered_at    timestamptz,
    answered_by    bigint,
    primary key (campaign_id, batch)
);
create index if not exists campaign_site_questions_open_idx
    on public.campaign_site_questions (requested_by, asked_at desc) where state = 'asked';
alter table public.campaign_site_questions enable row level security;

comment on table public.search_sites is
    'Search sites a person approved or rejected, per mode; approved ones are reused, rejected ones never offered again.';
comment on table public.campaign_sites is
    'The sites of a campaign''s web stage, numbered as the person saw them, and their decision.';
comment on table public.campaign_site_questions is
    'The «are all sites approved?» questions of a campaign, one per batch of new sites.';
