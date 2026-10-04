-- What the search engine showed for a URL (its title and snippet), kept with the queue row.
-- When the site itself cannot be read (it refuses bots: 403/429, a captcha page, robots.txt,
-- a temporary block) the web stage builds the listing from this text instead, so the
-- campaign still gets a card with the link (and the price, when the snippet shows one).

alter table public.web_campaign_urls add column if not exists search_title text
    check (search_title is null or length(search_title) <= 300);
alter table public.web_campaign_urls add column if not exists search_snippet text
    check (search_snippet is null or length(search_snippet) <= 500);
