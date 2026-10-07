-- Stage 4.3 / 4.4 and the dedup review (bot/campaign/runs.py, bot/campaign/metrics.py, bot/agents/recorder.py).
--
-- campaign_findings.card_number: the number on the card's «🔎 Найдено: N», stored when the send slot is taken, so the
--   head card keeps its number when it is edited later to show «Также на: ...» (null on older rows: the runner then
--   falls back to counting the sent exact cards).
-- agent_findings.reason: why a finding was excluded without being sent, e.g. «duplicate_of:<head finding id>».
-- web_campaign_urls.layer: the fetch layer that read the page (http, render, scrape, none = never asked).
-- campaign_metrics: one row per campaign, recomputed from the other tables (PostgresCampaignStore.refresh_metrics)
--   while the campaign runs and when it ends; `/campaign report` prints it.

alter table public.campaign_findings
    add column if not exists card_number integer check (card_number is null or card_number >= 1);
alter table public.agent_findings
    add column if not exists reason text check (reason is null or char_length(reason) <= 120);
alter table public.web_campaign_urls
    add column if not exists layer text check (layer is null or layer in ('http', 'render', 'scrape', 'none'));

create table if not exists public.campaign_metrics (
    campaign_id  uuid primary key references public.campaigns(id) on delete cascade,
    queries      integer not null default 0 check (queries >= 0),
    results      integer not null default 0 check (results >= 0),
    pages_http   integer not null default 0 check (pages_http >= 0),
    pages_render integer not null default 0 check (pages_render >= 0),
    pages_scrape integer not null default 0 check (pages_scrape >= 0),
    pages_failed integer not null default 0 check (pages_failed >= 0),
    findings     integer not null default 0 check (findings >= 0),
    exact        integer not null default 0 check (exact >= 0),
    "similar"    integer not null default 0 check ("similar" >= 0),
    other        integer not null default 0 check (other >= 0),
    excluded     jsonb not null default '{}'::jsonb check (jsonb_typeof(excluded) = 'object'),
    duplicates   integer not null default 0 check (duplicates >= 0),
    updated_at   timestamptz not null default now()
);
alter table public.campaign_metrics enable row level security;

comment on column public.campaign_findings.card_number is
    'The card''s «Найдено: N» as sent (findings sent or being sent when it was claimed); null on rows from before this column.';
comment on column public.agent_findings.reason is
    'Why a finding was excluded without being sent, e.g. duplicate_of:<head finding id>.';
comment on column public.web_campaign_urls.layer is
    'Fetch layer that read the page: http, render (browser), scrape (API); none when the site was never asked.';
comment on table public.campaign_metrics is
    'Per-campaign totals recomputed from web_search_queries, web_campaign_urls and campaign_findings: queries, pages per layer, findings per bucket, excluded by reason, duplicates.';
