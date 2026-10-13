-- Selectable campaign acquisition, budget pause, and links from ordinary analysis rows to Apify audit.
alter table public.campaigns
    add column if not exists acquisition_provider text
        check (acquisition_provider is null or acquisition_provider in ('local', 'apify'));

alter table public.campaigns drop constraint if exists campaigns_state_check;
alter table public.campaigns add constraint campaigns_state_check check (state in (
    'planned', 'discovering', 'running', 'paused_verification', 'paused_budget',
    'completed', 'cancelled', 'failed'
));

create or replace function public.enforce_campaign_transition()
returns trigger language plpgsql as $$
begin
    if new.state is distinct from old.state then
        if not (old.state, new.state) in (
            ('planned', 'discovering'), ('planned', 'running'), ('planned', 'cancelled'), ('planned', 'failed'),
            ('discovering', 'running'), ('discovering', 'paused_verification'), ('discovering', 'completed'),
            ('discovering', 'cancelled'), ('discovering', 'failed'),
            ('running', 'paused_verification'), ('running', 'paused_budget'), ('running', 'completed'),
            ('running', 'cancelled'), ('running', 'failed'),
            ('paused_verification', 'discovering'), ('paused_verification', 'running'),
            ('paused_verification', 'cancelled'), ('paused_verification', 'failed'),
            ('paused_budget', 'running'), ('paused_budget', 'cancelled'), ('paused_budget', 'failed')
        ) then
            raise exception 'illegal campaign transition % -> %', old.state, new.state
                using errcode = 'check_violation';
        end if;
        if new.state in ('completed', 'cancelled', 'failed') then
            new.finished_at := coalesce(new.finished_at, now());
        end if;
    end if;
    new.updated_at := now();
    return new;
end;
$$;

alter table public.acquisition_batches
    drop constraint if exists acquisition_batches_acquisition_method_check;
alter table public.acquisition_batches
    add constraint acquisition_batches_acquisition_method_check
        check (acquisition_method in ('facebook_connector', 'apify', 'agent_ridge', 'scrapling'));

alter table public.acquisition_runs
    drop constraint if exists acquisition_runs_acquisition_method_check;
alter table public.acquisition_runs
    add constraint acquisition_runs_acquisition_method_check
        check (acquisition_method in ('facebook_connector', 'apify', 'agent_ridge', 'scrapling'));
alter table public.acquisition_runs
    add column if not exists apify_run_id bigint references public.apify_runs(id) on delete set null;
create index if not exists acquisition_runs_apify_run_idx
    on public.acquisition_runs (apify_run_id) where apify_run_id is not null;
