-- One card for the same property seen on several sites or in several groups (bot/campaign/dedup.py).
-- The first sent card is the cluster head: its cluster_id is its own finding id and cluster_links lists
-- the other sightings as [{"url": ..., "site": ...}]. A later finding of the same object is not sent:
-- its row gets state 'duplicate' and duplicate_of = the head's finding id (never counted as sent).

alter table public.campaign_findings
    add column if not exists cluster_id uuid;
alter table public.campaign_findings
    add column if not exists cluster_links jsonb not null default '[]'::jsonb;
alter table public.campaign_findings
    add column if not exists duplicate_of uuid;

alter table public.campaign_findings
    drop constraint if exists campaign_findings_state_check;
alter table public.campaign_findings
    add constraint campaign_findings_state_check check (state in ('held', 'sending', 'sent', 'duplicate'));

create index if not exists campaign_findings_cluster_idx
    on public.campaign_findings (campaign_id, cluster_id);

comment on column public.campaign_findings.cluster_id is
    'Finding id of the cluster head (the card that was sent) for the head and its duplicates; null when not clustered.';
comment on column public.campaign_findings.cluster_links is
    'On a cluster head: the other sightings of the object, a list of {url, site}.';
comment on column public.campaign_findings.duplicate_of is
    'Finding id of the head this finding duplicates; such a finding is stored, never sent (state duplicate).';
