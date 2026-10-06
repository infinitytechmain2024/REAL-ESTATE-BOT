-- Index-page TTL and a durable render budget for the web stage.
--
-- web_seen_urls already has `kind` (listing / index / unknown, set when a fetch finishes) and
-- `finished_at` (the last fetch), so no column is added there: an index page whose last fetch is older
-- than WEB_SEARCH_INDEX_TTL_DAYS (default 7) is queued and read again; listings stay never-twice.
create index if not exists web_seen_urls_index_ttl_idx
    on public.web_seen_urls (finished_at) where kind = 'index';

-- A page the web stage re-read in the browser (WEB_SEARCH_MAX_RENDERS_PER_CAMPAIGN counts these rows,
-- so the budget survives a restart of the worker).
alter table public.web_campaign_urls add column if not exists rendered boolean not null default false;
create index if not exists web_campaign_urls_rendered_idx
    on public.web_campaign_urls (campaign_id) where rendered;
