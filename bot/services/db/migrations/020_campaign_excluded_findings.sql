-- A finding whose deal contradicts the request (a rental for a purchase) is
-- filed as 'excluded': held like similar/other so it is never picked up again,
-- but no question is ever asked about it and it is never sent
-- (bot/campaign/tolerance.py).

alter table public.campaign_findings
    drop constraint if exists campaign_findings_bucket_check;
alter table public.campaign_findings
    add constraint campaign_findings_bucket_check check (bucket in ('exact', 'similar', 'other', 'excluded'));
