-- The paid Apify runs of the v3 social sources (Instagram, TikTok, LinkedIn, Facebook reactions; bot/acquisition/
-- apify_source.py ApifySocialRun). apify_runs (046) cannot hold them: its batch_id is a Facebook acquisition batch.
-- Additive only, nothing reads it until a source is wired (P1.2-P1.8); the undo for an operator is
-- scripts/rollback/051_apify_source_runs_down.sql.
--
-- One row per (campaign, purpose, input): the same input for the same campaign and purpose is launched at most once.
-- Before the paid launch the row gets run_id 'pending:<uuid>' (docs/v2/HANDOFF.md section 4 item 2); a successful
-- launch replaces it with the real run id, an explicit refusal (4xx/429) removes it. A row whose run_id is set is
-- never launched again: a placeholder left behind closes as failed launch_unconfirmed, booked at max_charge_usd.
-- billed_usd is the run's cost (also in campaign_costs, key apify:<run_id>); import_checkpoint the dataset offset
-- already read, items_imported what the source kept.

create table if not exists public.apify_source_runs (
    id                bigint generated always as identity primary key,
    campaign_id       uuid not null references public.campaigns(id) on delete cascade,
    owner_user_id     bigint,
    platform          text not null check (platform in ('facebook', 'instagram', 'tiktok', 'linkedin')),
    purpose           text not null check (purpose ~ '^[a-z][a-z0-9_:-]{0,39}$'),
    actor_id          text not null check (char_length(actor_id) between 3 and 120),
    actor_build       text not null check (char_length(actor_build) between 1 and 40 and lower(actor_build) <> 'latest'),
    input_hash        text not null check (input_hash ~ '^[0-9a-f]{64}$'),
    run_id            text unique check (run_id is null or run_id ~ '^[A-Za-z0-9]{1,80}$'
                                         or run_id ~ '^pending:[0-9a-f]{32}$'),
    dataset_id        text check (dataset_id is null or dataset_id ~ '^[A-Za-z0-9]{1,80}$'),
    status            text not null default 'claimed'
                      check (status in ('claimed', 'launching', 'running', 'succeeded', 'failed')),
    max_charge_usd    numeric(10, 4) not null check (max_charge_usd > 0),
    billed_usd        numeric(10, 4) check (billed_usd is null or billed_usd >= 0),
    import_checkpoint integer not null default 0 check (import_checkpoint >= 0),
    items_imported    integer not null default 0 check (items_imported >= 0),
    error_code        text check (error_code is null or char_length(error_code) <= 200),
    created_at        timestamptz not null default now(),
    launched_at       timestamptz,
    finished_at       timestamptz,
    constraint apify_source_runs_input_unique unique (campaign_id, purpose, input_hash),
    constraint apify_source_runs_claimed_check check (status <> 'claimed' or run_id is null),
    constraint apify_source_runs_launching_check
        check (status <> 'launching' or (run_id is not null and run_id like 'pending:%')),
    constraint apify_source_runs_running_check
        check (status not in ('running', 'succeeded') or (run_id is not null and run_id not like 'pending:%'))
);
create index if not exists apify_source_runs_campaign_idx on public.apify_source_runs (campaign_id, platform);
create index if not exists apify_source_runs_status_idx on public.apify_source_runs (status);
alter table public.apify_source_runs enable row level security;

comment on table public.apify_source_runs is
    'Paid Apify runs of the v3 social sources: one per (campaign, purpose, input), pending:<uuid> before launch, run id before import, billed once.';
