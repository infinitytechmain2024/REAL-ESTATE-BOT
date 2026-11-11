-- Undo of bot/services/db/migrations/051_apify_source_runs.sql, for an operator only (migrations are applied
-- forward by scripts/apply_migrations.sh; this file is never applied automatically).
-- Refused while the table holds any row (a row is the record of a paid, or possibly paid, Apify run).
-- After running it, delete the 051 row from schema_migrations so the migration can be applied again.
begin;
do $$
declare
    has_rows boolean;
begin
    if to_regclass('public.apify_source_runs') is not null then
        -- dynamic: a plain query would be planned (and fail) even when the table is already gone
        execute 'select exists (select 1 from public.apify_source_runs)' into has_rows;
        if has_rows then
            raise exception 'apify_source_runs still has rows';
        end if;
    end if;
end $$;
drop table if exists public.apify_source_runs;
commit;
