-- REAL-ESTATE-BOT :: facebook group tracking
--
-- Run after 001_init.sql:
--   psql "$SUPABASE_DB_URL" -f bot/services/db/migrations/002_facebook.sql
--
-- A property/investor hit read from a Facebook group post still lands in
-- `results` like any other hit (see bot/services/facebook/client.py) --
-- these tables only track the groups themselves and the shared login
-- session, which have no equivalent in 001_init.sql.

create table if not exists public.facebook_groups (
    id               uuid        primary key default gen_random_uuid(),
    url              text        not null unique,
    label            text,
    access_state     text        not null default 'unknown_error'
                     check (access_state in
                         ('accessible', 'membership_required', 'pending_approval',
                          'login_required', 'unavailable', 'unknown_error')),
    last_checked_at  timestamptz,
    last_activity_at timestamptz,
    notes            text,
    created_at       timestamptz not null default now()
);

comment on table public.facebook_groups is
    'Groups the operator has supplied (v1: no automatic discovery). access_state is set by '
    'bot.services.facebook.groups.check_access; a failed extraction must never be recorded here '
    'as unavailable -- see the implementation plan on distinguishing extraction failure from a '
    'genuinely inaccessible group.';

create index if not exists facebook_groups_access_idx on public.facebook_groups (access_state);

create table if not exists public.facebook_session_incidents (
    id           uuid        primary key default gen_random_uuid(),
    state        text        not null,
    detected_at  timestamptz not null default now(),
    resolved_at  timestamptz,
    notes        text
);

comment on table public.facebook_session_incidents is
    'One row per login/verification incident on the shared browser session. Kept append-only so '
    'an admin alert is never sent twice for the same unresolved incident (resolved_at is null).';

create index if not exists facebook_incidents_open_idx
    on public.facebook_session_incidents (detected_at desc)
    where resolved_at is null;

alter table public.facebook_groups            enable row level security;
alter table public.facebook_session_incidents enable row level security;
