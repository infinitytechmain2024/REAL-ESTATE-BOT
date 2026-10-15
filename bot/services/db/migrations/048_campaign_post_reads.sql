-- One Facebook post a user sent by link (v2 B1, bot/campaign/post_leads.py): its comments are read through Apify
-- (APIFY_FACEBOOK_COMMENTS_ENABLED) and the commenters who show interest become investor_leads (026) of the campaign.
-- campaign_comment_reads (026) cannot hold it: its key is a finding, and a post sent by link has none.
-- run_id is stored before the run is awaited, so a restart or a second tick resumes the same paid run and never
-- launches another. Additive: no existing table changes.

create table if not exists public.campaign_post_reads (
    campaign_id     uuid not null references public.campaigns(id) on delete cascade,
    post_url        text not null check (char_length(post_url) <= 2000),
    state           text not null default 'queued' check (state in ('queued', 'reading', 'done', 'failed')),
    provider        text not null default 'apify' check (char_length(provider) <= 40),
    run_id          text check (char_length(run_id) <= 100),
    attempts        integer not null default 0 check (attempts >= 0),
    comments_found  integer check (comments_found >= 0),
    leads_found     integer check (leads_found >= 0),
    cost_usd        numeric(12, 6) check (cost_usd >= 0),
    error_code      text check (char_length(error_code) <= 200),
    created_at      timestamptz not null default now(),
    started_at      timestamptz,
    read_at         timestamptz,
    primary key (campaign_id, post_url)
);
create index if not exists campaign_post_reads_queue_idx
    on public.campaign_post_reads (state, created_at);
alter table public.campaign_post_reads enable row level security;

comment on table public.campaign_post_reads is
    'Facebook posts sent by link whose comments are read once through Apify for investor leads (v2 B1).';
