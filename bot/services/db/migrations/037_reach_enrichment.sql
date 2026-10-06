-- Investor reach enrichment (bot/campaign/people.py): after a reach result is judged relevant, its public page
-- (a site or a public Telegram channel, never LinkedIn/Instagram/TikTok/X/Reddit) is opened once with the web
-- stage's fetcher. What it tells is kept on the contact: contacts (emails, phones, website, company, last_activity),
-- the first 600 characters of readable text, the time it was read, and the 0-100 score against the task's spec.

alter table public.reach_contacts add column if not exists enriched_at timestamptz;
alter table public.reach_contacts add column if not exists contacts jsonb not null default '{}'::jsonb
    check (jsonb_typeof(contacts) = 'object');
alter table public.reach_contacts add column if not exists profile_text text
    check (profile_text is null or char_length(profile_text) <= 2000);
alter table public.reach_contacts add column if not exists score integer
    check (score is null or (score >= 0 and score <= 100));

comment on column public.reach_contacts.enriched_at is 'When the public page of the result was read (null: never opened).';
comment on column public.reach_contacts.contacts is
    'Found on the page: emails, phones, website, company, last_activity (JSON object, only what was found).';
comment on column public.reach_contacts.profile_text is 'First 600 characters of the readable text of the public page.';
comment on column public.reach_contacts.score is 'Fit to the task spec, 0-100 (bot/campaign/people.py score).';
