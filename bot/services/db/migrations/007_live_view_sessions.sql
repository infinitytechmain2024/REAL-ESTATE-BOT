-- Human login / checkpoint sessions: the bot asks an operator to open the
-- profile's live browser through Telegram, and records who did what.

create table if not exists public.live_view_sessions (
    id                  uuid primary key default gen_random_uuid(),
    browser_profile_id  uuid not null references public.browser_profiles(id) on delete restrict,
    reason              text not null check (reason in ('login', 'checkpoint')),
    state               text not null default 'requested' check (state in
                        ('requested', 'open', 'completed', 'cancelled', 'expired', 'failed')),
    requested_by        text not null,
    opened_by           bigint,
    closed_by           text,
    error_code          text,
    created_at          timestamptz not null default now(),
    -- A request nobody opens lapses; an open session is closed at its deadline.
    expires_at          timestamptz not null,
    opened_at           timestamptz,
    closed_at           timestamptz,
    check (expires_at > created_at)
);

-- At most one live session per profile: one display, one human, one browser.
create unique index if not exists live_view_sessions_one_active_idx
    on public.live_view_sessions (browser_profile_id)
    where state in ('requested', 'open');

create index if not exists live_view_sessions_active_expiry_idx
    on public.live_view_sessions (expires_at)
    where state in ('requested', 'open');

comment on table public.live_view_sessions is
    'Operator live-browser sessions for Facebook login and checkpoints; opened only through a Telegram-signed Mini App.';
