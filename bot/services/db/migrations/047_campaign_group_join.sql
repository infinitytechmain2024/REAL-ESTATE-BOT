-- A closed Facebook group (Apify says group_private) is not read until a person joins it: the runner tells them
-- once (join_notice_at); their «Вступил» (join_confirmed_at) queues the group for a browser read. Additive only.
alter table public.campaign_groups
    add column if not exists join_notice_at timestamptz,
    add column if not exists join_confirmed_at timestamptz;
