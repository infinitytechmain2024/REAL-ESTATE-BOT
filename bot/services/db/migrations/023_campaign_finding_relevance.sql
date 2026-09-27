-- The campaign relevance check (bot/campaign/relevance.py): one small AI call
-- per finding compares it with the campaign's task before it is bucketed
-- (bot/campaign/tolerance.py). The verdict is stored once per finding and
-- campaign, so a finding is never judged twice (a failed send puts an exact
-- finding back in the queue; its verdict stays). Rows also count the calls
-- against the per-campaign cap. verdict null: the call failed and the
-- deterministic rules decided. reason is Russian, for owners' logs only;
-- deviation is the short Russian phrase the «Одобрить» question may show.

create table if not exists public.campaign_finding_relevance (
    finding_id  uuid not null references public.findings(id) on delete cascade,
    campaign_id uuid not null references public.campaigns(id) on delete cascade,
    verdict     text check (verdict in ('match', 'near', 'reject')),
    reason      text not null default '' check (char_length(reason) <= 300),
    deviation   text check (char_length(deviation) <= 120),
    model       text check (char_length(model) <= 120),
    created_at  timestamptz not null default now(),
    primary key (finding_id, campaign_id)
);
create index if not exists campaign_finding_relevance_campaign_idx
    on public.campaign_finding_relevance (campaign_id);
alter table public.campaign_finding_relevance enable row level security;

comment on table public.campaign_finding_relevance is
    'AI verdict (match/near/reject) of a finding against its campaign task; null verdict: the call failed.';
