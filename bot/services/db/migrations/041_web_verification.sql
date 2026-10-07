-- Human verification for the web search (bot/web_search/verification.py).
-- When the browser layer lands on a CAPTCHA / anti-bot page, the web stage opens a
-- verification job for that site's source and the shared "web-search-render" browser
-- profile (platform 'website'); the existing verification flow (Telegram button, live
-- browser, watchdog) hands it to a person. Additive only.
--
-- platform 'website' already exists in monitoring_sources and browser_profiles, and
-- challenge_kind already has captcha / checkpoint / unknown, so only two things change:
--   * job_type gets 'web_challenge' (one open job per site: the existing
--     verification_jobs_one_open_idx on (source_id, job_type) dedupes it);
--   * target_url: the page the browser opened on and the watchdog reloads (a site's
--     source URL is only https://<host>/, the challenged page is deeper).

alter table public.verification_jobs
    add column if not exists target_url text check (target_url is null or length(target_url) <= 2048);

do $$
declare
    existing text;
begin
    for existing in
        select c.conname from pg_constraint c
         where c.conrelid = 'public.verification_jobs'::regclass and c.contype = 'c'
           and pg_get_constraintdef(c.oid) like '%job_type%'
    loop
        execute format('alter table public.verification_jobs drop constraint %I', existing);
    end loop;
end $$;

alter table public.verification_jobs
    add constraint verification_jobs_job_type_check check (job_type in
        ('facebook_challenge', 'login', 'consent', 'manual_source_review', 'web_challenge'));

comment on column public.verification_jobs.target_url is
    'The page a person opens in the live browser and the watchdog reloads (web_challenge jobs); null: the source URL.';
