-- Exact, similar and other findings of a campaign (bot/campaign/tolerance.py).
-- Every finding of a campaign is filed in exactly one bucket: the primary key
-- on campaign_findings.finding_id already makes it one row per finding. Exact
-- ones are streamed at once as before; similar and other ones are 'held' until
-- the requester answers one question per bucket with «Одобрить» / «Нет»
-- (campaign_offers). The Telegram control plane records the answer; the
-- campaign runner streams the approved held cards on its next tick.

alter table public.campaign_findings
    add column if not exists bucket text not null default 'exact'
        check (bucket in ('exact', 'similar', 'other'));
-- Relative distance from the requested budget (0.2 = 20 % away); null when unknown.
alter table public.campaign_findings
    add column if not exists distance double precision;

alter table public.campaign_findings
    drop constraint if exists campaign_findings_state_check;
alter table public.campaign_findings
    add constraint campaign_findings_state_check check (state in ('held', 'sending', 'sent'));
-- Exact findings are never held.
alter table public.campaign_findings
    drop constraint if exists campaign_findings_exact_not_held;
alter table public.campaign_findings
    add constraint campaign_findings_exact_not_held check (bucket <> 'exact' or state <> 'held');
create index if not exists campaign_findings_held_idx
    on public.campaign_findings (campaign_id, bucket, distance) where state = 'held';

-- One question per campaign and bucket; the primary key makes it asked once.
create table if not exists public.campaign_offers (
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    bucket              text not null check (bucket in ('similar', 'other')),
    state               text not null default 'asked' check (state in ('asked', 'approved', 'declined')),
    telegram_message_id bigint,
    asked_at            timestamptz not null default now(),
    decided_at          timestamptz,
    -- The Telegram user who pressed the button: the requester or an owner.
    decided_by          bigint,
    primary key (campaign_id, bucket),
    constraint campaign_offers_decision_consistent
        check ((state = 'asked') = (decided_at is null) and (state = 'asked') = (decided_by is null))
);
alter table public.campaign_offers enable row level security;

comment on column public.campaign_findings.bucket is
    'exact (sent at once), similar or other (held until the requester approves that bucket).';
comment on table public.campaign_offers is
    'The once-per-campaign question to show similar/other findings, and the requester''s answer.';
