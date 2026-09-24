-- Human-in-the-loop verification flow (bot/verification). Additive only: the
-- existing verification_jobs states and transition graph are unchanged; this
-- adds who holds a job, what kind of challenge it is, and when the watchdog
-- saw the browser recover.

alter table public.verification_jobs
    add column if not exists browser_profile_id uuid references public.browser_profiles(id) on delete restrict,
    add column if not exists challenge_kind text check (challenge_kind is null or challenge_kind in
        ('checkpoint', 'captcha', 'login', 'account_warning',
         'identity_verification', 'two_factor_setup', 'account_restricted', 'unknown')),
    -- Identity checks, new 2FA enrolment and account restrictions are for the
    -- owner alone: the flow stops and never offers a live browser for them.
    add column if not exists sensitive boolean not null default false,
    add column if not exists claimed_by bigint,
    add column if not exists claimed_at timestamptz,
    add column if not exists notified_at timestamptz,
    add column if not exists solved_at timestamptz,
    add column if not exists recovered_at timestamptz,
    add column if not exists resumed_at timestamptz;

-- One-time links sent in Telegram. Only a SHA-256 of the token is stored.
create table if not exists public.verification_access_tokens (
    id                  uuid primary key default gen_random_uuid(),
    verification_job_id uuid not null references public.verification_jobs(id) on delete cascade,
    telegram_user_id    bigint not null,
    browser_profile_id  uuid not null references public.browser_profiles(id) on delete restrict,
    token_sha256        text not null unique check (token_sha256 ~ '^[0-9a-f]{64}$'),
    created_at          timestamptz not null default now(),
    expires_at          timestamptz not null,
    used_at             timestamptz,
    used_by_login       text,
    revoked_at          timestamptz,
    check (expires_at > created_at)
);
create index if not exists verification_access_tokens_job_idx
    on public.verification_access_tokens (verification_job_id);

-- A consumed token becomes a short page session, bound to the same job, user,
-- profile and Tailscale login. Only a SHA-256 of the cookie is stored.
create table if not exists public.verification_page_sessions (
    id                  uuid primary key default gen_random_uuid(),
    access_token_id     uuid not null unique references public.verification_access_tokens(id) on delete cascade,
    verification_job_id uuid not null references public.verification_jobs(id) on delete cascade,
    telegram_user_id    bigint not null,
    browser_profile_id  uuid not null references public.browser_profiles(id) on delete restrict,
    tailscale_login     text not null,
    cookie_sha256       text not null unique check (cookie_sha256 ~ '^[0-9a-f]{64}$'),
    csrf_token          text not null,
    created_at          timestamptz not null default now(),
    expires_at          timestamptz not null,
    revoked_at          timestamptz,
    check (expires_at > created_at)
);

-- Append-only: every view, claim, decision, watchdog result and expiry.
create table if not exists public.verification_events (
    id                  bigint generated always as identity primary key,
    verification_job_id uuid not null references public.verification_jobs(id) on delete cascade,
    event               text not null check (event in (
        'detected', 'notified', 'token_issued', 'access_denied', 'opened', 'view', 'claim',
        'solve', 'recovery_confirmed', 'recovery_failed', 'resume', 'cancel', 'fail',
        'sensitive_stop', 'expire')),
    actor               text not null,
    detail              jsonb not null default '{}'::jsonb check (jsonb_typeof(detail) = 'object'),
    occurred_at         timestamptz not null default now()
);
create index if not exists verification_events_job_idx
    on public.verification_events (verification_job_id, occurred_at);

create or replace function public.verification_events_append_only()
returns trigger language plpgsql as $$
begin
    raise exception 'verification_events is append-only' using errcode = '42501';
end;
$$;
drop trigger if exists verification_events_no_change on public.verification_events;
create trigger verification_events_no_change before update or delete on public.verification_events
    for each row execute function public.verification_events_append_only();

comment on table public.verification_access_tokens is
    'Single-use, short-lived verification links bound to one job, Telegram user and browser profile; hashes only.';
comment on table public.verification_page_sessions is
    'Verification page sessions created by consuming an access token over Tailscale; hashes only.';
comment on table public.verification_events is
    'Append-only audit trail of the human verification flow.';
