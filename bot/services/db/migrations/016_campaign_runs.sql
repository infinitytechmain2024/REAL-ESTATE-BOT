-- Campaign runner: a campaign's queued groups are collected in windows of at
-- most 20 groups, one ordinary Facebook batch per window (same quotas, breakers
-- and facebook-runner launch as /run). Findings from those batches are streamed
-- to the campaign chat exactly once, and one Telegram message per campaign
-- shows its live status. Adds the `campaign` Orchestra command.

create extension if not exists pgcrypto;

-- Which campaign a Facebook batch belongs to, so posts and findings trace back.
alter table public.acquisition_batches
    add column if not exists campaign_id uuid references public.campaigns(id) on delete set null;
create index if not exists acquisition_batches_campaign_idx
    on public.acquisition_batches (campaign_id) where campaign_id is not null;

-- Runner bookkeeping, one row per campaign (kept off `campaigns` so status
-- edits do not flood the campaign audit trail).
create table if not exists public.campaign_runs (
    campaign_id         uuid primary key references public.campaigns(id) on delete cascade,
    -- The status text last shown in Telegram: the message is edited only on change.
    status_text         text check (status_text is null or length(status_text) <= 1000),
    -- No new window before this time (cooldown, or a refused window's retry).
    next_window_at      timestamptz,
    -- When the last window finished and the runner began waiting for analysis.
    drain_started_at    timestamptz,
    updated_at          timestamptz not null default now()
);

create table if not exists public.campaign_windows (
    id                  uuid primary key default gen_random_uuid(),
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    window_no           integer not null check (window_no between 1 and 10),
    batch_id            uuid not null unique references public.acquisition_batches(id) on delete restrict,
    group_count         smallint not null check (group_count between 1 and 20),
    state               text not null default 'active' check (state in ('active', 'finished')),
    -- The batch's final state when the window closed.
    outcome             text check (outcome is null or outcome in ('succeeded', 'failed', 'cancelled')),
    created_at          timestamptz not null default now(),
    finished_at         timestamptz,
    constraint campaign_windows_number_unique unique (campaign_id, window_no)
);
-- One window at a time per campaign.
create unique index if not exists campaign_windows_one_active_idx
    on public.campaign_windows (campaign_id) where state = 'active';

-- A finding streamed to the campaign chat; the primary key makes a send
-- happen at most once, across restarts. 'sending' is the claim taken before
-- the Telegram call.
create table if not exists public.campaign_findings (
    finding_id          uuid primary key references public.findings(id) on delete cascade,
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    state               text not null default 'sending' check (state in ('sending', 'sent')),
    telegram_message_id bigint,
    created_at          timestamptz not null default now(),
    sent_at             timestamptz,
    constraint campaign_findings_campaign_finding_unique unique (campaign_id, finding_id)
);
create index if not exists campaign_findings_campaign_idx on public.campaign_findings (campaign_id, state);

drop trigger if exists campaign_windows_audit on public.campaign_windows;
create trigger campaign_windows_audit
    after insert or update on public.campaign_windows
    for each row execute function public.audit_orchestration_row();
alter table public.campaign_runs enable row level security;
alter table public.campaign_windows enable row level security;
alter table public.campaign_findings enable row level security;

-- The `campaign` command goes through the same confirmation and inbox.
alter table public.telegram_command_confirmations
    drop constraint if exists telegram_command_confirmations_command_check;
alter table public.telegram_command_confirmations
    add constraint telegram_command_confirmations_command_check
    check (command in ('run', 'pause', 'resume', 'cancel', 'campaign'));
alter table public.orchestration_commands
    drop constraint if exists orchestration_commands_command_check;
alter table public.orchestration_commands
    add constraint orchestration_commands_command_check
    check (command in ('run', 'pause', 'resume', 'cancel', 'campaign'));

comment on table public.campaign_runs is
    'Campaign runner bookkeeping: live status text, window cooldown and the final analysis wait.';
comment on table public.campaign_windows is
    'A campaign window: at most 20 queued groups collected as one Facebook batch; audited in orchestration_audit_log.';
comment on table public.campaign_findings is
    'Findings streamed to a campaign chat; one row per finding so a restart never sends it twice.';
