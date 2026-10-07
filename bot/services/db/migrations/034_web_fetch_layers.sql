-- Fetch layers of the web stage: a host's refusals and blocks are tracked per layer.
--
-- Layers: http (plain/impersonated GET), render (the browser), and an optional scrape API.
-- A refusal (403/429/503, a captcha page) on one layer blocks only that layer of the host for
-- 12 hours; the worker goes on with the next layer. `blocked_until` stays as "every enabled layer
-- is blocked" (the host gets no fetch at all, only search-result cards).
alter table public.web_hosts add column if not exists http_refusals int not null default 0 check (http_refusals >= 0);
alter table public.web_hosts add column if not exists http_blocked_until timestamptz;
alter table public.web_hosts add column if not exists render_refusals int not null default 0 check (render_refusals >= 0);
alter table public.web_hosts add column if not exists render_blocked_until timestamptz;

-- A block set before this migration applied to every layer.
update public.web_hosts
   set http_blocked_until = blocked_until, render_blocked_until = blocked_until
 where blocked_until is not null and blocked_until > now();

-- A page the web stage fetched through the scrape API (WEB_SEARCH_MAX_SCRAPE_API_PER_CAMPAIGN counts these rows).
alter table public.web_campaign_urls add column if not exists scraped boolean not null default false;
create index if not exists web_campaign_urls_scraped_idx
    on public.web_campaign_urls (campaign_id) where scraped;
