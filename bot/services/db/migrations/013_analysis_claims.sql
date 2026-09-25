-- Integration hand-offs between collectors, analysis and delivery.
--
-- A durable, expiring claim on a collected post: the analysis worker holds it
-- across the OpenRouter call, so two workers never analyse (or pay for) the
-- same post, and a worker that dies mid-call frees it after the claim time.

alter table public.collected_posts
    add column if not exists analysis_claim_token uuid,
    add column if not exists analysis_claimed_at  timestamptz;

create index if not exists collected_posts_analysis_claim_idx
    on public.collected_posts (analysis_claimed_at)
    where state = 'normalised';

comment on column public.collected_posts.analysis_claim_token is
    'Set by the analysis worker while it analyses the post; only the holder may finalise it.';
