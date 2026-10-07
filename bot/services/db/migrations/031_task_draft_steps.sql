-- The intake saves step = 'ask' (a clarifying question is on screen) and step = 'target'
-- (investors: who to look for), but the check from 018 allowed neither, so those saves
-- failed and the question was lost. Replace the check with one that lists every step.

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
           and a.attname = 'step'
    loop
        execute format('alter table public.user_task_drafts drop constraint %I', existing);
    end loop;
end
$$;

alter table public.user_task_drafts
    add constraint user_task_drafts_step_check
    check (step in ('idle', 'city', 'deal', 'budget', 'summary', 'ask', 'target'));
