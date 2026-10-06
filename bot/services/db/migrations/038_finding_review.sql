-- Reviewer and final report (PLAN stage 4, bot/agents/reviewer.py, bot/campaign/final_report.py).
--
-- review: the reviewer's criteria matrix for a finding, next to the verdict it was mapped to:
--   {"criteria": [{"name", "verdict": pass|fail|unknown, "quote", "note_ru"}], "overall": match|near|reject,
--    "deviation_ru", "confidence", "tolerance_pct"}; null for the legacy judge and for failed calls.
-- why: the category a held / excluded finding was filed under (place, deal, type, budget, rooms, area,
--   criteria, kind, unverified, ai), so the final report can count rejections by reason.
-- final_report_sent_at: the user's final report is claimed once, like summary_sent_at (migration 030).

alter table public.campaign_finding_relevance
    add column if not exists review jsonb check (review is null or jsonb_typeof(review) = 'object');
alter table public.campaign_findings
    add column if not exists why text check (char_length(why) <= 40);
alter table public.campaign_runs
    add column if not exists final_report_sent_at timestamptz;

comment on column public.campaign_finding_relevance.review is
    'Reviewer criteria matrix (pass/fail/unknown with a quote each) and overall verdict; null for the legacy judge.';
comment on column public.campaign_findings.why is
    'Reason category of a held or excluded finding (place, deal, type, budget, rooms, area, criteria, kind, unverified, ai).';
comment on column public.campaign_runs.final_report_sent_at is
    'Set when the final report for the requester is claimed (cleared if Telegram failed): sent once.';
