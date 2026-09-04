-- REAL-ESTATE-BOT :: per-user daily quotas and LLM cost accounting
--
-- Apply after 001_init.sql:
--   psql "$SUPABASE_DB_URL" -f bot/services/db/migrations/002_limits_and_costs.sql
--
-- Adds two things the bot needs in order to bound what it can spend:
--   * daily_usage  -- how many searches / briefings each user asked for today
--   * llm_usage    -- what every single LLM call cost, so a daily hard limit
--                     can be enforced and an overspend can be explained after
--                     the fact
--
-- Both are keyed on the UTC calendar date. A sliding window would be fairer
-- but nobody can predict when it lets them back in; "resets at midnight UTC"
-- is something a user can be told in one sentence.

-- ---------------------------------------------------------------------------
-- daily_usage: one row per (user, UTC day)
-- ---------------------------------------------------------------------------
create table if not exists public.daily_usage (
    user_id    bigint      not null references public.users (telegram_id) on delete cascade,
    usage_date date        not null,
    searches   integer     not null default 0 check (searches >= 0),
    details    integer     not null default 0 check (details  >= 0),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    primary key (user_id, usage_date)
);

comment on table public.daily_usage is
    'Per-user request counters, one row per UTC day. Old rows can be pruned freely.';

create index if not exists daily_usage_date_idx on public.daily_usage (usage_date desc);

drop trigger if exists daily_usage_touch_updated_at on public.daily_usage;
create trigger daily_usage_touch_updated_at
    before update on public.daily_usage
    for each row execute function public.touch_updated_at();

-- ---------------------------------------------------------------------------
-- bump_daily_usage: atomically increment one counter and return its new value
--
-- Done as a function rather than a read-modify-write from the bot because two
-- concurrent requests from the same user would otherwise both read the old
-- count and both be allowed through. `on conflict do update` makes the whole
-- thing one statement, so the quota holds under concurrency.
-- ---------------------------------------------------------------------------
create or replace function public.bump_daily_usage(
    p_user_id bigint,
    p_kind    text,
    p_amount  integer default 1
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    v_new integer;
begin
    if p_kind not in ('searches', 'details') then
        raise exception 'unknown usage kind: %', p_kind;
    end if;

    insert into public.daily_usage as d (user_id, usage_date, searches, details)
    values (
        p_user_id,
        (now() at time zone 'utc')::date,
        case when p_kind = 'searches' then p_amount else 0 end,
        case when p_kind = 'details'  then p_amount else 0 end
    )
    on conflict (user_id, usage_date) do update
        set searches = d.searches + (case when p_kind = 'searches' then p_amount else 0 end),
            details  = d.details  + (case when p_kind = 'details'  then p_amount else 0 end)
    returning (case when p_kind = 'searches' then d.searches else d.details end) into v_new;

    return v_new;
end;
$$;

comment on function public.bump_daily_usage(bigint, text, integer) is
    'Increment today''s counter for a user and return the new value. Atomic.';

-- ---------------------------------------------------------------------------
-- llm_usage: an append-only ledger of every model call
--
-- cost_usd is an estimate: it is tokens x the per-million prices configured in
-- LLM_PRICE_PROMPT_USD_PER_1M / LLM_PRICE_COMPLETION_USD_PER_1M, because the
-- providers do not return a price. Treat it as a budget signal, not a bill.
-- ---------------------------------------------------------------------------
create table if not exists public.llm_usage (
    id                uuid          primary key default gen_random_uuid(),
    user_id           bigint        references public.users (telegram_id) on delete set null,
    provider          text          not null,
    model             text          not null,
    purpose           text          not null default 'chat',
    prompt_tokens     integer       not null default 0 check (prompt_tokens >= 0),
    completion_tokens integer       not null default 0 check (completion_tokens >= 0),
    cost_usd          numeric(12,6) not null default 0 check (cost_usd >= 0),
    usage_date        date          not null default (now() at time zone 'utc')::date,
    created_at        timestamptz   not null default now()
);

comment on table public.llm_usage is
    'One row per LLM call. cost_usd is estimated from token counts and configured prices.';

create index if not exists llm_usage_date_idx      on public.llm_usage (usage_date desc);
create index if not exists llm_usage_user_date_idx on public.llm_usage (user_id, usage_date desc);

-- ---------------------------------------------------------------------------
-- llm_cost_today: what the whole deployment has spent so far today
--
-- The bot keeps a running total in memory and only calls this at start-up and
-- when the process has no total of its own, so it stays cheap.
-- ---------------------------------------------------------------------------
create or replace function public.llm_cost_today()
returns numeric
language sql
stable
security definer
set search_path = public
as $$
    select coalesce(sum(cost_usd), 0)
    from public.llm_usage
    where usage_date = (now() at time zone 'utc')::date;
$$;

-- ---------------------------------------------------------------------------
-- Row level security
--
-- Same posture as 001_init.sql: enabled with no permissive policies, so only
-- the service_role key the bot uses can read or write these tables.
-- ---------------------------------------------------------------------------
alter table public.daily_usage enable row level security;
alter table public.llm_usage   enable row level security;

-- The helper functions are SECURITY DEFINER, so make sure they are not
-- reachable by the public-facing keys either.
revoke all on function public.bump_daily_usage(bigint, text, integer) from public, anon, authenticated;
revoke all on function public.llm_cost_today() from public, anon, authenticated;
