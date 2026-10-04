-- The end-of-campaign summary («Итог поиска»: what each source gave) is sent once:
-- the runner claims it by setting summary_sent_at, and clears it again if Telegram failed.

alter table public.campaign_runs add column if not exists summary_sent_at timestamptz;
