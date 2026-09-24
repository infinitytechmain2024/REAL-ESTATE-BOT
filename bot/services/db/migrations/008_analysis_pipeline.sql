-- Additive metadata and idempotent digest registry for the bounded analysis worker.
alter table public.findings add column if not exists analysis_metadata jsonb not null default '{}'::jsonb
  check (jsonb_typeof(analysis_metadata) = 'object');

create table if not exists public.analysis_digests (
    id uuid primary key default gen_random_uuid(),
    idempotency_key text not null unique,
    telegram_chat_id bigint not null,
    vertical text not null check (vertical in ('real_estate', 'investors')),
    finding_ids jsonb not null default '[]'::jsonb check (jsonb_typeof(finding_ids) = 'array'),
    body text not null,
    state text not null default 'queued' check (state in ('queued', 'sent', 'failed')),
    telegram_message_id bigint,
    created_at timestamptz not null default now(),
    sent_at timestamptz,
    updated_at timestamptz not null default now()
);
create index if not exists analysis_digests_pending_idx on public.analysis_digests(state, created_at) where state='queued';
comment on table public.analysis_digests is 'Idempotent, bounded Telegram digest registry. A digest key is never re-sent.';
