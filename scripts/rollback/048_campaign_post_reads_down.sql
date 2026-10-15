-- Undo of bot/services/db/migrations/048_campaign_post_reads.sql, for an operator only (migrations are applied
-- forward by scripts/apply_migrations.sh; this file is never applied automatically).
-- Refused while the table holds any row (a read, its run id and cost are the record of a paid Apify run).
-- After running it, delete the 048 row from schema_migrations so the migration can be applied again.
begin;
do $$
begin
    if to_regclass('public.campaign_post_reads') is not null
       and exists (select 1 from public.campaign_post_reads) then
        raise exception 'campaign_post_reads still has rows';
    end if;
end $$;
drop table if exists public.campaign_post_reads;
commit;
