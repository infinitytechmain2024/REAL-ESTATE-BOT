-- Durable Apify Actor launches, resumable Dataset imports, billing, and short-lived raw payloads.
create table if not exists public.apify_runs (
    id                  bigint generated always as identity primary key,
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    batch_id            uuid not null references public.acquisition_batches(id) on delete restrict,
    provider            text not null default 'apify' check (provider = 'apify'),
    actor_id            text not null check (char_length(actor_id) between 1 and 200),
    actor_build         text not null check (char_length(actor_build) between 1 and 100),
    run_id              text unique,
    dataset_id          text,
    status              text not null check (status in (
                            'RUNNING', 'SUCCEEDED', 'FAILED', 'TIMED_OUT', 'ABORTED',
                            'LAUNCH_UNKNOWN')),
    input_hash          text not null check (input_hash ~ '^[0-9a-f]{64}$'),
    launched_at         timestamptz not null default now(),
    finished_at         timestamptz,
    import_checkpoint   integer not null default 0 check (import_checkpoint >= 0),
    items_imported      integer not null default 0 check (items_imported >= 0),
    billed_usd          numeric(10, 4) check (billed_usd is null or billed_usd >= 0),
    error_message       text check (error_message is null or char_length(error_message) <= 1000),
    constraint apify_runs_remote_identity check (
        (status = 'LAUNCH_UNKNOWN' and run_id is null)
        or (status <> 'LAUNCH_UNKNOWN' and run_id is not null)
    )
);

create index if not exists apify_runs_campaign_idx
    on public.apify_runs (campaign_id, launched_at desc);
create index if not exists apify_runs_batch_idx
    on public.apify_runs (batch_id, launched_at desc);
create index if not exists apify_runs_status_idx
    on public.apify_runs (status, launched_at);

create table if not exists public.apify_raw_items (
    id                  bigint generated always as identity primary key,
    run_id              text not null,
    item_index          integer not null check (item_index >= 0),
    payload             jsonb not null check (jsonb_typeof(payload) = 'object'),
    imported_at         timestamptz not null default now(),
    constraint apify_raw_items_run_fk foreign key (run_id)
        references public.apify_runs(run_id) on delete cascade,
    constraint apify_raw_items_run_item_unique unique (run_id, item_index)
);

create index if not exists apify_raw_items_imported_at_idx
    on public.apify_raw_items (imported_at);

alter table public.apify_runs enable row level security;
alter table public.apify_raw_items enable row level security;

comment on table public.apify_runs is
    'Audit and resume state for paid Apify Facebook Actor runs; LAUNCH_UNKNOWN records have no remote run_id.';
comment on table public.apify_raw_items is
    'Short-lived raw Apify Dataset items; retention cleanup removes these without deleting run audit rows.';
