-- A running search needs a platform whose browser profile is not signed in (bot/campaign/logins.py).
-- The campaign runner records the need here; the Telegram control plane's live-view watcher then opens a
-- login request for <platform>-main and sends every operator the «Открыть браузер» button itself, exactly
-- as it does for a Facebook checkpoint. Nothing is typed into a login form by the bot.

create table if not exists public.login_requests (
    platform        text primary key check (platform in ('facebook', 'instagram', 'tiktok', 'linkedin', 'x')),
    searches        integer not null default 1 check (searches >= 0),
    first_needed_at timestamptz not null default now(),
    last_needed_at  timestamptz not null default now()
);
alter table public.login_requests enable row level security;

comment on table public.login_requests is
    'Platforms a running search needs a login for; the control plane sends operators the login link.';
