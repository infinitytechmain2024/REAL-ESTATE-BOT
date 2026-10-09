-- Structured listing sources use 'api'; preserve every existing fetch layer and NULL.
-- Replace only the layer CHECK introduced by migration 039, leaving other checks intact.
alter table public.web_campaign_urls
    drop constraint if exists web_campaign_urls_layer_check;
alter table public.web_campaign_urls
    add constraint web_campaign_urls_layer_check
        check (layer is null or layer in ('http', 'render', 'scrape', 'none', 'api'));

comment on column public.web_campaign_urls.layer is
    'Fetch layer: http, render (browser), scrape (HTML unlocker), api (structured listing source); none when the site was never asked.';
