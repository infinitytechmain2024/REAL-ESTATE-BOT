-- Hybrid pipeline, phase 3 (docs/HYBRID_AGENTS.md, SA-2 Reduction agents):
-- one row per campaign post a Reduction agent handles. The row is the claim
-- (state 'claimed', claimed_by / claimed_until: a crashed worker's post is taken
-- over when the lease ends) and then the whole decision trace of that post:
-- Claude's extraction, Jev's answers (p + confidence per question), the gate's
-- action / bucket / reason, the deterministic rules' bucket, the policy and the
-- models used. mode 'shadow': decided and stored, never sent (the running
-- analysis path still sends); 'live' is for a later phase.

create table if not exists public.agent_reductions (
    post_id        uuid not null references public.collected_posts(id) on delete cascade,
    campaign_id    uuid not null references public.campaigns(id) on delete cascade,
    state          text not null check (state in ('claimed', 'done', 'failed')),
    mode           text not null default 'shadow' check (mode in ('shadow', 'live')),
    claimed_by     text check (char_length(claimed_by) <= 120),
    claimed_until  timestamptz,
    attempts       integer not null default 0 check (attempts >= 0),
    model_calls    integer not null default 0 check (model_calls >= 0),
    extraction     jsonb,
    jev            jsonb,
    action         text check (action in ('send', 'hold', 'discard')),
    bucket         text check (bucket in ('exact', 'similar', 'other', 'excluded')),
    reason         text check (char_length(reason) <= 120),
    score          numeric(4, 3),
    rules_bucket   text check (rules_bucket in ('exact', 'similar', 'other', 'excluded')),
    policy         jsonb,
    models         jsonb,
    prompt_version text check (char_length(prompt_version) <= 40),
    error          text check (char_length(error) <= 120),
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    primary key (post_id, campaign_id),
    check (state <> 'done' or action is not null)
);
create index if not exists agent_reductions_claim_idx on public.agent_reductions (state, claimed_until);
create index if not exists agent_reductions_campaign_idx on public.agent_reductions (campaign_id, action);
create index if not exists agent_reductions_recent_idx on public.agent_reductions (updated_at);
alter table public.agent_reductions enable row level security;

comment on table public.agent_reductions is
    'SA-2 Reduction agents: claim + decision trace per campaign post (Claude extraction, Jev answers, gate).';
