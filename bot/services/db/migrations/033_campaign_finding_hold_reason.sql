-- Why a finding was held (bot/campaign/runner.py): for an exact finding the AI relevance check could not
-- verify (cap reached, no judge, failed calls) this is the owner-facing Russian note
-- («Не проверено ИИ: ...»); null for findings held by the deterministic rules.
alter table public.campaign_findings
    add column if not exists hold_reason text check (char_length(hold_reason) <= 300);

comment on column public.campaign_findings.hold_reason is
    'Owner-facing reason a finding was held unverified (AI check missing); null when the rules held it.';
