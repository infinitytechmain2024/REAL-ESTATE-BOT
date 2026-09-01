-- REAL-ESTATE-BOT :: initial schema
--
-- Run in the Supabase SQL editor, or with:
--   psql "$SUPABASE_DB_URL" -f bot/services/db/migrations/001_init.sql
--
-- The bot connects with the service_role key and therefore bypasses RLS.
-- Policies are still defined so that the anon/authenticated keys cannot read
-- anything if the project is ever exposed to a client.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------------
create table if not exists public.users (
    telegram_id   bigint       primary key,
    username      text,
    first_name    text,
    language_code text,
    current_mode  text         check (current_mode in ('land', 'investors')),
    is_blocked    boolean      not null default false,
    created_at    timestamptz  not null default now(),
    updated_at    timestamptz  not null default now()
);

comment on table public.users is 'Telegram users, keyed by their Telegram id.';

-- ---------------------------------------------------------------------------
-- searches: one row per user request (text or transcribed voice)
-- ---------------------------------------------------------------------------
create table if not exists public.searches (
    id           uuid        primary key default gen_random_uuid(),
    user_id      bigint      not null references public.users (telegram_id) on delete cascade,
    mode         text        not null check (mode in ('land', 'investors')),
    raw_query    text        not null,
    transcript   text,                      -- set when the input was a voice note
    parsed       jsonb       not null default '{}'::jsonb,
    queries      jsonb       not null default '[]'::jsonb,
    hits_found   integer     not null default 0,
    results_sent integer     not null default 0,
    created_at   timestamptz not null default now()
);

create index if not exists searches_user_created_idx
    on public.searches (user_id, created_at desc);

comment on column public.searches.parsed is 'ParsedQuery as extracted by the LLM.';
comment on column public.searches.queries is 'The search strings actually sent to SearXNG.';

-- ---------------------------------------------------------------------------
-- results: one row per (user, unique url)
-- ---------------------------------------------------------------------------
create table if not exists public.results (
    id         uuid        primary key default gen_random_uuid(),
    search_id  uuid        references public.searches (id) on delete set null,
    user_id    bigint      not null references public.users (telegram_id) on delete cascade,
    mode       text        not null check (mode in ('land', 'investors')),
    url        text        not null,
    url_hash   text        not null,
    title      text        not null default '',
    summary    text        not null default '',
    score      integer     not null default 0 check (score between 0 and 100),
    status     text        not null default 'new'
               check (status in ('new', 'sent', 'interesting', 'not_interesting', 'saved')),
    raw        jsonb       not null default '{}'::jsonb,
    content    text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    -- The de-duplication guarantee. url_hash is sha256 of the normalised URL
    -- (see bot/utils/urls.py), so the same listing reached through a tracking
    -- link, http vs https, or with/without www is stored once per user.
    -- Scoped per user rather than globally so two users can each be shown the
    -- same listing.
    constraint results_user_url_unique unique (user_id, url_hash)
);

create index if not exists results_user_status_idx  on public.results (user_id, status);
create index if not exists results_search_idx       on public.results (search_id);
create index if not exists results_user_score_idx   on public.results (user_id, score desc);
create index if not exists results_url_hash_idx     on public.results (url_hash);

-- ---------------------------------------------------------------------------
-- feedback: an append-only log of button presses
--
-- results.status holds the current state; this table keeps the history, which
-- is what any future relevance tuning would be trained on.
-- ---------------------------------------------------------------------------
create table if not exists public.feedback (
    id         uuid        primary key default gen_random_uuid(),
    result_id  uuid        not null references public.results (id) on delete cascade,
    user_id    bigint      not null references public.users (telegram_id) on delete cascade,
    action     text        not null
               check (action in ('interesting', 'not_interesting', 'save', 'details')),
    created_at timestamptz not null default now()
);

create index if not exists feedback_result_idx on public.feedback (result_id);
create index if not exists feedback_user_idx   on public.feedback (user_id, created_at desc);

-- ---------------------------------------------------------------------------
-- updated_at maintenance
-- ---------------------------------------------------------------------------
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists users_touch_updated_at on public.users;
create trigger users_touch_updated_at
    before update on public.users
    for each row execute function public.touch_updated_at();

drop trigger if exists results_touch_updated_at on public.results;
create trigger results_touch_updated_at
    before update on public.results
    for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- Row level security
--
-- Enabled with no permissive policies: the service_role key the bot uses
-- bypasses RLS entirely, while anon and authenticated keys get nothing. If you
-- later build a user-facing client, add policies here rather than disabling RLS.
-- ---------------------------------------------------------------------------
alter table public.users    enable row level security;
alter table public.searches enable row level security;
alter table public.results  enable row level security;
alter table public.feedback enable row level security;
