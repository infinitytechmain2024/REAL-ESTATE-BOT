-- Undo of bot/services/db/migrations/049_campaign_comment_reads_run_id.sql, for an operator only (migrations are
-- applied forward by scripts/apply_migrations.sh; this file is never applied automatically).
-- Refused while any read holds a run id (it is the record of a paid Apify run).
-- After running it, delete the 049 row from schema_migrations so the migration can be applied again.
begin;
do $$
begin
    if exists (select 1 from information_schema.columns
                where table_schema = 'public' and table_name = 'campaign_comment_reads' and column_name = 'run_id')
       and exists (select 1 from public.campaign_comment_reads where run_id is not null) then
        raise exception 'campaign_comment_reads still has run ids';
    end if;
end $$;
alter table public.campaign_comment_reads drop column if exists run_id;
commit;
