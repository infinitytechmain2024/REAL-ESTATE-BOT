-- What a campaign run costs and what it dropped on the way (bot/utils/costs.py). Additive only.
--
-- One row per paid call (kind 'cost'): an LLM answer (stage 'llm', item = the model, cost = OpenRouter's usage.cost
-- or an estimate), a scrape-API page ('scrape', item = the site), a paid search ('search'); 'fetch' and 'api' are
-- for plain fetches and listing APIs. The same table keeps what the final report must not hide: a failed LLM call
-- (kind 'error', code = its short code) and a page dropped by a cheap filter before any model saw it (kind 'skip',
-- code = the reason). campaign_id is null for calls made outside a campaign (Facebook monitoring, the interview).
-- CAMPAIGN_BUDGET_USD is checked against sum(cost_usd) of a campaign's 'cost' rows by every service.

create table if not exists public.campaign_costs (
    id          bigint generated always as identity primary key,
    campaign_id uuid references public.campaigns(id) on delete cascade,
    stage       text not null check (stage in ('search', 'fetch', 'scrape', 'api', 'llm')),
    kind        text not null default 'cost' check (kind in ('cost', 'error', 'skip')),
    provider    text check (provider is null or char_length(provider) <= 40),
    item        text check (item is null or char_length(item) <= 120),
    code        text check (code is null or char_length(code) <= 80),
    units       integer not null default 1 check (units >= 0),
    cost_usd    numeric(12, 6) not null default 0 check (cost_usd >= 0),
    created_at  timestamptz not null default now()
);
create index if not exists campaign_costs_campaign_idx on public.campaign_costs (campaign_id, kind, stage);
create index if not exists campaign_costs_created_idx on public.campaign_costs (created_at);
alter table public.campaign_costs enable row level security;

comment on table public.campaign_costs is
    'Ledger of a campaign run: paid calls by stage (cost_usd), failed LLM calls (kind error) and pages skipped before the LLM (kind skip).';
