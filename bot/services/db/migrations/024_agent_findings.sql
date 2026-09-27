-- Hybrid pipeline, phase 1 (docs/HYBRID_AGENTS.md, SA-3 Recorder): every
-- finding a campaign sends, holds or excludes is stored here BEFORE anything
-- is sent. state: to_send (written just before the Telegram call; a failed
-- send stays here with send_attempts + 1), sent (with the message id), held
-- (similar/other, waiting for «Одобрить»), excluded (never sent). A sent row
-- is never moved back. site / url_key / fingerprint / simhash are what the
-- later Deduplication & Final Analysis sub-agent groups by: the same site
-- with the same listing, facts or text is a duplicate.

create table if not exists public.agent_findings (
    id                  uuid primary key default gen_random_uuid(),
    campaign_id         uuid not null references public.campaigns(id) on delete cascade,
    finding_id          uuid not null references public.findings(id) on delete cascade,
    state               text not null check (state in ('to_send', 'sent', 'held', 'excluded')),
    bucket              text check (bucket in ('exact', 'similar', 'other', 'excluded')),
    site                text not null check (char_length(site) <= 300),
    url                 text check (char_length(url) <= 2000),
    url_key             text not null check (char_length(url_key) <= 200),
    fingerprint         text check (char_length(fingerprint) <= 400),
    simhash             bigint,
    facts               jsonb not null default '{}'::jsonb,
    card_text           text check (char_length(card_text) <= 4100),
    telegram_message_id bigint,
    sent_at             timestamptz,
    send_attempts       integer not null default 0 check (send_attempts >= 0),
    agent               text not null default 'campaign-runner' check (char_length(agent) <= 80),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    unique (campaign_id, finding_id),
    check (state <> 'sent' or sent_at is not null)
);
create index if not exists agent_findings_campaign_state_idx on public.agent_findings (campaign_id, state);
create index if not exists agent_findings_campaign_site_idx on public.agent_findings (campaign_id, site);
alter table public.agent_findings enable row level security;

comment on table public.agent_findings is
    'SA-3 Recorder: every campaign finding stored before it is sent (to_send -> sent), plus held and excluded ones.';
