-- The live status line shows where the bot searches right now, with a link (bot/campaign/status_text.py).
--
-- campaign_social_state.current_url: the social network search page the running query has open.
-- campaign_reach.platform: the platform the running reach query is aimed at (linkedin, reddit, x, ...); the reach
--   reads search-engine results only, so the status shows the platform's own address.

alter table public.campaign_social_state
    add column if not exists current_url text check (current_url is null or length(current_url) <= 2000);
alter table public.campaign_reach
    add column if not exists platform text check (platform is null or length(platform) <= 40);
