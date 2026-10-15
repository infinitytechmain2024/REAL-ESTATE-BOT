-- The task intake gets a third mode, "both" (real estate + investors), but the check from 018
-- allowed only real_estate and investors, so saving such a draft would fail. Widen the check.
-- Only widens: older code keeps working.

do $$
declare
    existing text;
begin
    for existing in
        select c.conname
          from pg_constraint c
          join pg_attribute a on a.attrelid = c.conrelid and a.attnum = any (c.conkey)
         where c.conrelid = 'public.user_task_drafts'::regclass
           and c.contype = 'c'
           and a.attname = 'mode'
    loop
        execute format('alter table public.user_task_drafts drop constraint %I', existing);
    end loop;
end
$$;

alter table public.user_task_drafts
    add constraint user_task_drafts_mode_check
    check (mode is null or mode in ('real_estate', 'investors', 'both'));
