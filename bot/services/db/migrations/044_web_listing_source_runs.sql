-- Durable paid-source launch and import checkpoints; no portal/network lock is held here.
create table if not exists public.web_listing_source_runs (
    campaign_id uuid not null references public.campaigns(id) on delete cascade,
    name text not null,
    state text not null default 'starting' check (state in ('starting', 'running', 'ready', 'completed', 'failed')),
    run_id text,
    dataset_id text,
    listings jsonb not null default '[]'::jsonb check (jsonb_typeof(listings) = 'array'),
    import_offset integer not null default 0 check (import_offset >= 0 and import_offset <= jsonb_array_length(listings)),
    error_code text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    primary key (campaign_id, name)
);
alter table public.web_listing_source_runs enable row level security;
alter table public.campaign_costs add column if not exists idempotency_key text;
create unique index if not exists campaign_costs_idempotency_key_idx on public.campaign_costs (idempotency_key);
