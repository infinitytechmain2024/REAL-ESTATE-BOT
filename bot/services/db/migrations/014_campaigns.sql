-- Autonomous campaigns: one planned goal (location + vertical + bounded
-- limits) per row, driven through an explicit state machine. Planning is
-- deterministic and never starts collectors; later phases move the state.

create extension if not exists pgcrypto;

create table if not exists public.campaigns (
    id                  uuid primary key default gen_random_uuid(),
    telegram_chat_id    bigint not null,
    requested_by        bigint not null,
    source_text         text not null check (length(source_text) between 1 and 2000),
    plan                jsonb not null check (jsonb_typeof(plan) = 'object'),
    plan_version        text,
    state               text not null default 'planned' check (state in (
                            'planned', 'discovering', 'running', 'paused_verification',
                            'completed', 'cancelled', 'failed')),
    stop_reason         text check (stop_reason is null or length(stop_reason) <= 500),
    status_message_id   bigint,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    finished_at         timestamptz
);

create index if not exists campaigns_state_created_idx
    on public.campaigns (state, created_at);

-- Own transition guard (003's shared CASE stays untouched). It also keeps
-- updated_at current and stamps finished_at on entering a terminal state.
create or replace function public.enforce_campaign_transition()
returns trigger language plpgsql as $$
begin
    if new.state is distinct from old.state then
        if not (old.state, new.state) in (
            ('planned', 'discovering'), ('planned', 'running'), ('planned', 'cancelled'), ('planned', 'failed'),
            ('discovering', 'running'), ('discovering', 'paused_verification'), ('discovering', 'completed'),
            ('discovering', 'cancelled'), ('discovering', 'failed'),
            ('running', 'paused_verification'), ('running', 'completed'), ('running', 'cancelled'), ('running', 'failed'),
            ('paused_verification', 'discovering'), ('paused_verification', 'running'),
            ('paused_verification', 'cancelled'), ('paused_verification', 'failed')
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

drop trigger if exists campaigns_state_guard on public.campaigns;
create trigger campaigns_state_guard
    before update on public.campaigns
    for each row execute function public.enforce_campaign_transition();
drop trigger if exists campaigns_audit on public.campaigns;
create trigger campaigns_audit
    after insert or update on public.campaigns
    for each row execute function public.audit_orchestration_row();
alter table public.campaigns enable row level security;

comment on table public.campaigns is
    'Autonomous search campaigns planned from a Telegram goal; state changes are guarded and audited in orchestration_audit_log.';
