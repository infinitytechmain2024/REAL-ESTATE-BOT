-- The structured task (TaskSpec, bot/campaign/spec.py) the interviewer builds with the person:
-- kept on the draft across answers and stored on the campaign it launches.
-- Nullable: campaigns planned from a free-text goal (/campaign <goal>) have no spec.

alter table public.campaigns add column if not exists spec jsonb
    check (spec is null or jsonb_typeof(spec) = 'object');

alter table public.user_task_drafts add column if not exists spec jsonb
    check (spec is null or jsonb_typeof(spec) = 'object');

comment on column public.campaigns.spec is
    'TaskSpec JSON (hard/soft requirements, exclusions, sources, delivery); null for goals given as free text.';
comment on column public.user_task_drafts.spec is
    'TaskSpec JSON the interviewer has filled so far; reset on launch.';
