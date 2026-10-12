-- Count structured listing-source reads separately from paid HTML unlocker reads.
alter table public.campaign_metrics
    add column if not exists pages_api integer not null default 0 check (pages_api >= 0);

comment on column public.campaign_metrics.pages_api is
    'Fetched structured portal API listings (web_campaign_urls.layer = api), excluding search snippets.';
