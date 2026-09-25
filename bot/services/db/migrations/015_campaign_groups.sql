-- Campaign discovery: Facebook groups found for a campaign by searching
-- Facebook itself through the Browser Session Manager. One row per group per
-- campaign, whatever the outcome, so every decision (queued, rejected and why)
-- stays auditable and a group is never re-evaluated blindly. Activity evidence
-- is counts and ages only; discovery never stores post content.

create extension if not exists pgcrypto;

create table if not exists public.campaign_groups (
    id                  uuid primary key default gen_random_uuid(),
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    group_key           text not null check (group_key ~ '^[a-z0-9_.-]{1,100}$'),
    canonical_url       text not null check (canonical_url = 'https://www.facebook.com/groups/' || group_key || '/'),
    name                text check (name is null or length(name) <= 200),
    language            text check (language is null or language in ('es', 'en', 'ru', 'uk')),
    seed                text check (seed is null or length(seed) <= 80),
    relevance_score     numeric(5, 3) not null default 0 check (relevance_score between 0 and 1),
    activity            text not null default 'UNKNOWN' check (activity in
                            ('ACTIVE', 'INACTIVE', 'DEAD', 'INACCESSIBLE', 'UNKNOWN')),
    activity_evidence   jsonb not null default '{}'::jsonb check (jsonb_typeof(activity_evidence) = 'object'),
    state               text not null default 'discovered' check (state in
                            ('discovered', 'queued', 'rejected', 'collected', 'skipped')),
    reject_reason       text check (reject_reason is null or length(reject_reason) <= 200),
    window_no           integer check (window_no is null or window_no >= 1),
    batch_id            uuid references public.acquisition_batches(id) on delete set null,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint campaign_groups_campaign_group_key unique (campaign_id, group_key)
);

create index if not exists campaign_groups_campaign_state_idx
    on public.campaign_groups (campaign_id, state);

-- Which search seeds and group visits a (possibly interrupted) discovery has
-- already done, so a resumed run continues instead of starting over.
alter table public.campaigns
    add column if not exists discovery_progress jsonb not null default '{}'::jsonb;

drop trigger if exists campaign_groups_touch_updated_at on public.campaign_groups;
create trigger campaign_groups_touch_updated_at
    before update on public.campaign_groups
    for each row execute function public.touch_updated_at();
drop trigger if exists campaign_groups_audit on public.campaign_groups;
create trigger campaign_groups_audit
    after insert or update on public.campaign_groups
    for each row execute function public.audit_orchestration_row();
alter table public.campaign_groups enable row level security;

comment on table public.campaign_groups is
    'Facebook groups discovered for a campaign, with relevance, activity evidence (counts/ages only) and queue state; audited in orchestration_audit_log.';
