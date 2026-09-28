-- Investor leads from the comments under the objects campaigns sent
-- (bot/campaign/leads.py). When a Facebook post's card goes out, the post is
-- queued in campaign_comment_reads (at most CAMPAIGN_COMMENT_MAX_POSTS per
-- campaign). The comment worker opens the post between Facebook windows, reads
-- its comments and keeps the people who show interest as an investor or buyer
-- in investor_leads: one row per person per post, with the object's city and
-- facts. Nothing is sent then. An investor search (mode «Инвесторы») later
-- sends each stored person of its city once (campaign_lead_deliveries), with
-- their profile link, what they asked under which objects and a short preview.

create table if not exists public.campaign_comment_reads (
    campaign_id     uuid not null references public.campaigns(id) on delete cascade,
    finding_id      uuid not null references public.findings(id) on delete cascade,
    post_url        text not null check (char_length(post_url) <= 2000),
    state           text not null default 'queued' check (state in ('queued', 'reading', 'done', 'failed')),
    attempts        integer not null default 0 check (attempts >= 0),
    comments_found  integer check (comments_found >= 0),
    leads_found     integer check (leads_found >= 0),
    error_code      text check (char_length(error_code) <= 200),
    created_at      timestamptz not null default now(),
    started_at      timestamptz,
    read_at         timestamptz,
    primary key (campaign_id, finding_id)
);
create index if not exists campaign_comment_reads_queue_idx
    on public.campaign_comment_reads (state, created_at);
create index if not exists campaign_comment_reads_read_at_idx
    on public.campaign_comment_reads (read_at) where read_at is not null;
alter table public.campaign_comment_reads enable row level security;

create table if not exists public.investor_leads (
    id              uuid primary key default gen_random_uuid(),
    profile_key     text not null check (char_length(profile_key) <= 200),
    profile_url     text not null check (char_length(profile_url) <= 500),
    author_name     text check (char_length(author_name) <= 200),
    role            text not null check (role in ('investor', 'buyer')),
    confidence      real check (confidence >= 0 and confidence <= 1),
    comment_text    text not null check (char_length(comment_text) <= 2000),
    comment_url     text check (char_length(comment_url) <= 2000),
    summary_ru      text check (char_length(summary_ru) <= 600),
    judged_by       text not null default 'rules' check (char_length(judged_by) <= 120),
    post_url        text not null check (char_length(post_url) <= 2000),
    location        text check (char_length(location) <= 200),
    object_facts    jsonb not null default '{}'::jsonb check (jsonb_typeof(object_facts) = 'object'),
    finding_id      uuid references public.findings(id) on delete set null,
    campaign_id     uuid references public.campaigns(id) on delete set null,
    created_at      timestamptz not null default now(),
    unique (profile_key, post_url)
);
create index if not exists investor_leads_location_idx on public.investor_leads (location, created_at desc);
create index if not exists investor_leads_profile_idx on public.investor_leads (profile_key);
alter table public.investor_leads enable row level security;

create table if not exists public.campaign_lead_deliveries (
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    profile_key         text not null check (char_length(profile_key) <= 200),
    state               text not null default 'sending' check (state in ('sending', 'sent')),
    telegram_message_id bigint,
    created_at          timestamptz not null default now(),
    sent_at             timestamptz,
    primary key (campaign_id, profile_key),
    check (state <> 'sent' or sent_at is not null)
);
alter table public.campaign_lead_deliveries enable row level security;

comment on table public.campaign_comment_reads is
    'Facebook posts (sent campaign findings) whose comments are read for investor leads, once each.';
comment on table public.investor_leads is
    'People who commented with investor or buyer interest under an object: public profile link, comment, object.';
comment on table public.campaign_lead_deliveries is
    'Which stored people an investor search already sent (once per person per campaign).';
