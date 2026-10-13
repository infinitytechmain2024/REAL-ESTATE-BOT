-- The Apify provider of Facebook batches (bot/facebook_collector/apify, FEATURE_APIFY_FACEBOOK, off by default).
-- Additive only; the undo for an operator is scripts/rollback/046_facebook_apify_runs_down.sql.
--
-- apify_runs: one actor run per Facebook batch (the batch is the queue item; the provider never launches a second run
--   for it). run_id is stored before the run is awaited or imported; a launch whose answer was lost is kept as
--   status LAUNCH_UNKNOWN (run_id 'unknown-<batch>'), so it is never launched again. import_checkpoint is the
--   dataset offset already imported; billed_usd the run's cost (also booked in campaign_costs, key apify:<run_id>).
-- apify_raw_items: the dataset items as the actor returned them, for audit and re-mapping; removed after
--   APIFY_FACEBOOK_RAW_RETENTION_DAYS by scripts/cleanup_apify_raw.py (apify_runs stays as the audit trail).
-- acquisition_runs.acquisition_method gains 'apify' (the per-group run row of an Apify batch).
-- campaigns.acquisition_provider: who reads the campaign's Facebook groups (local browser by default).

create table if not exists public.apify_runs (
    id                bigint generated always as identity primary key,
    batch_id          uuid not null unique references public.acquisition_batches(id) on delete restrict,
    campaign_id       uuid references public.campaigns(id) on delete set null,
    provider          text not null default 'apify' check (provider = 'apify'),
    actor_id          text not null check (char_length(actor_id) between 3 and 120),
    actor_build       text not null check (char_length(actor_build) between 1 and 40 and lower(actor_build) <> 'latest'),
    run_id            text not null unique check (run_id ~ '^[A-Za-z0-9-]{1,80}$'),
    dataset_id        text check (dataset_id is null or dataset_id ~ '^[A-Za-z0-9]{1,80}$'),
    status            text not null check (status in
                      ('RUNNING', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'ABORTED', 'LAUNCH_UNKNOWN')),
    input_hash        text not null check (char_length(input_hash) = 64),
    launched_at       timestamptz not null default now(),
    finished_at       timestamptz,
    import_checkpoint integer not null default 0 check (import_checkpoint >= 0),
    items_imported    integer not null default 0 check (items_imported >= 0),
    billed_usd        numeric(10, 4) check (billed_usd is null or billed_usd >= 0),
    error_message     text check (error_message is null or char_length(error_message) <= 300)
);
create index if not exists apify_runs_campaign_idx on public.apify_runs (campaign_id);
create index if not exists apify_runs_status_idx on public.apify_runs (status);
alter table public.apify_runs enable row level security;

create table if not exists public.apify_raw_items (
    id          bigint generated always as identity primary key,
    run_id      text not null references public.apify_runs(run_id) on delete cascade,
    item_index  integer not null check (item_index >= 0),
    payload     jsonb not null check (jsonb_typeof(payload) = 'object'),
    imported_at timestamptz not null default now(),
    unique (run_id, item_index)
);
create index if not exists apify_raw_items_imported_idx on public.apify_raw_items (imported_at);
alter table public.apify_raw_items enable row level security;

alter table public.acquisition_runs drop constraint if exists acquisition_runs_acquisition_method_check;
alter table public.acquisition_runs add constraint acquisition_runs_acquisition_method_check
    check (acquisition_method in ('facebook_connector', 'agent_ridge', 'scrapling', 'apify'));

alter table public.campaigns
    add column if not exists acquisition_provider text not null default 'local'
        check (acquisition_provider in ('local', 'apify'));

comment on table public.apify_runs is
    'One Apify actor run per Facebook batch: run id before import, LAUNCH_UNKNOWN never relaunched, dataset import checkpoint, billed cost.';
comment on table public.apify_raw_items is
    'Raw Apify dataset items of a run (audit, re-mapping); deleted after APIFY_FACEBOOK_RAW_RETENTION_DAYS.';
comment on column public.campaigns.acquisition_provider is
    'Who reads the campaign''s Facebook groups: local (the browser collector) or apify (public groups, FEATURE_APIFY_FACEBOOK).';
