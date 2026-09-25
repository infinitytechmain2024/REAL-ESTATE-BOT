-- Runtime switches owners change from Telegram (today: `auto_mode`, set with
-- /auto on|off). A missing row means the .env default (AUTO_MODE) applies.
-- Every insert and update lands in orchestration_audit_log with its actor.

create extension if not exists pgcrypto;

create table if not exists public.control_settings (
    key                 text primary key check (key ~ '^[a-z_]{1,64}$'),
    -- Stable identity for orchestration_audit_log.entity_id (a uuid).
    id                  uuid not null default gen_random_uuid() unique,
    value               jsonb not null,
    updated_by          bigint not null,
    updated_at          timestamptz not null default now()
);

-- The shared audit function expects id/state columns shaped like a lifecycle
-- table; this one records the value (a JSON scalar) as the state instead.
create or replace function public.audit_control_setting()
returns trigger language plpgsql as $$
begin
    if tg_op = 'INSERT' then
        insert into public.orchestration_audit_log(entity_type, entity_id, action, new_state, new_data)
        values (tg_table_name, new.id, 'insert', new.value #>> '{}', to_jsonb(new));
    else
        insert into public.orchestration_audit_log(entity_type, entity_id, action, old_state, new_state, old_data, new_data)
        values (tg_table_name, new.id, 'update', old.value #>> '{}', new.value #>> '{}', to_jsonb(old), to_jsonb(new));
    end if;
    return new;
end;
$$;

drop trigger if exists control_settings_audit on public.control_settings;
create trigger control_settings_audit
    after insert or update on public.control_settings
    for each row execute function public.audit_control_setting();

alter table public.control_settings enable row level security;

comment on table public.control_settings is
    'Owner-controlled runtime switches (e.g. auto_mode on/off); absent rows fall back to .env defaults. Audited.';
