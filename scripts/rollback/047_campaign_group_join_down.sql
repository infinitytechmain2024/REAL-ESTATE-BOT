-- Undo of bot/services/db/migrations/047_campaign_group_join.sql, for an operator only (migrations are applied
-- forward by scripts/apply_migrations.sh; this file is never applied automatically).
-- Refused while any group carries a join notice or confirmation: they would be lost.
-- After running it, delete the 047 row from schema_migrations so the migration can be applied again.
begin;
do $$
begin
    if exists (select 1 from public.campaign_groups
                where join_notice_at is not null or join_confirmed_at is not null) then
        raise exception 'campaign_groups still has join_notice_at / join_confirmed_at data';
    end if;
end $$;
alter table public.campaign_groups drop column if exists join_confirmed_at;
alter table public.campaign_groups drop column if exists join_notice_at;
commit;
