-- Undo of bot/services/db/migrations/050_task_drafts_mode_both.sql, for an operator only (migrations are
-- applied forward by scripts/apply_migrations.sh; this file is never applied automatically).
-- Refused while any task draft holds mode = 'both'.
-- After running it, delete the 050 row from schema_migrations so the migration can be applied again.
begin;
do $$
declare
    has_both boolean;
begin
    -- dynamic, so the file stays valid on a repeated run
    execute 'select exists (select 1 from public.user_task_drafts where mode = ''both'')' into has_both;
    if has_both then
        raise exception 'user_task_drafts still has drafts with mode both';
    end if;
end $$;
alter table public.user_task_drafts drop constraint if exists user_task_drafts_mode_check;
alter table public.user_task_drafts
    add constraint user_task_drafts_mode_check check (mode in ('real_estate', 'investors'));
commit;
