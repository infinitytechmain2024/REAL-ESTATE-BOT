-- Undo of bot/services/db/migrations/046_facebook_apify_runs.sql, for an operator only (migrations are applied
-- forward by scripts/apply_migrations.sh; this file is never applied automatically).
-- Refused while any acquisition run still uses the 'apify' method: retire those rows first.
-- After running it, delete the 046 row from schema_migrations so the migration can be applied again.
begin;
do $$
begin
    if exists (select 1 from public.acquisition_runs where acquisition_method = 'apify') then
        raise exception 'acquisition_runs still has apify rows';
    end if;
end $$;
alter table public.campaigns drop column if exists acquisition_provider;
alter table public.acquisition_runs drop constraint if exists acquisition_runs_acquisition_method_check;
alter table public.acquisition_runs add constraint acquisition_runs_acquisition_method_check
    check (acquisition_method in ('facebook_connector', 'agent_ridge', 'scrapling'));
drop table if exists public.apify_raw_items;
drop table if exists public.apify_runs;
commit;
