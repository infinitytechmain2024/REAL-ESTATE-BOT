-- Operators approved from Telegram, next to the owners fixed in .env
-- (TELEGRAM_OPERATOR_IDS). Owners approve; everything is kept as history.

create table if not exists public.telegram_access_requests (
    id                  uuid primary key default gen_random_uuid(),
    telegram_user_id    bigint not null,
    display_name        text,
    username            text,
    state               text not null default 'pending' check (state in ('pending', 'approved', 'denied')),
    requested_at        timestamptz not null default now(),
    decided_at          timestamptz,
    decided_by          bigint
);
-- One open request per person: repeated taps do not spam the owners.
create unique index if not exists telegram_access_requests_one_pending_idx
    on public.telegram_access_requests (telegram_user_id) where state = 'pending';
create index if not exists telegram_access_requests_user_idx
    on public.telegram_access_requests (telegram_user_id, requested_at desc);

create table if not exists public.telegram_operators (
    telegram_user_id    bigint primary key,
    display_name        text,
    username            text,
    state               text not null check (state in ('approved', 'revoked')),
    -- helper: human verification only; operator: also controls collection.
    role                text not null default 'helper' check (role in ('helper', 'operator')),
    approved_by         bigint not null,
    approved_at         timestamptz not null default now(),
    revoked_by          bigint,
    revoked_at          timestamptz,
    request_id          uuid references public.telegram_access_requests(id) on delete set null
);

comment on table public.telegram_access_requests is
    'Access requests from the Telegram "Request access" button; decided by an owner.';
comment on table public.telegram_operators is
    'People approved by an owner at runtime (role helper or operator), in addition to TELEGRAM_OPERATOR_IDS; revoked rows are kept.';
