-- The Apify run of a comment read (v2 B2, LEADS_PROVIDER=apify, bot/campaign/comment_reader.py).
-- run_id is stored before the run is awaited, so a second tick or a restart resumes the same paid run and never
-- launches another; its presence also counts the day's Apify reads (APIFY_FACEBOOK_COMMENTS_MAX_PER_DAY).
-- Additive and nullable: the browser reader never writes it.

alter table public.campaign_comment_reads
    add column if not exists run_id text check (char_length(run_id) <= 100);
