-- Automatic restart of a Facebook batch after a human verification resume.
-- The verification service inserts a request in the same transaction that
-- requeues the batch; the long-running facebook-runner claims and runs it.
-- Nothing else is launched: queued batches without a request stay manual.

create table if not exists public.collector_launch_requests (
    id                  uuid primary key default gen_random_uuid(),
    batch_id            uuid not null references public.acquisition_batches(id) on delete cascade,
    verification_job_id uuid references public.verification_jobs(id) on delete set null,
    requested_by        text not null,
    -- Who hears about the outcome (the operator who resumed).
    notify_telegram_id  bigint,
    state               text not null default 'pending'
                        check (state in ('pending', 'running', 'finished', 'failed', 'skipped')),
    -- The collector's own outcome when finished: succeeded, cancelled, human_verification_required.
    result              text,
    error               text,
    requested_at        timestamptz not null default now(),
    started_at          timestamptz,
    finished_at         timestamptz,
    -- Last state reported in Telegram ('stale' = still pending after the grace time).
    notified_state      text
);
-- One open launch per batch, so a double tap never starts two collectors.
create unique index if not exists collector_launch_requests_one_open_idx
    on public.collector_launch_requests (batch_id) where state in ('pending', 'running');
create index if not exists collector_launch_requests_pending_idx
    on public.collector_launch_requests (requested_at) where state = 'pending';

comment on table public.collector_launch_requests is
    'Batches requeued by a verification resume, started automatically by facebook-runner; kept as history.';
