-- Durable, idempotent command inbox for the Main Orchestra dispatcher.
-- A confirmed Telegram command is persisted before any source/batch/run is made.

create table if not exists public.orchestration_commands (
    id uuid primary key default gen_random_uuid(),
    confirmation_id uuid references public.telegram_command_confirmations(id) on delete restrict,
    telegram_chat_id bigint not null,
    telegram_user_id bigint not null,
    telegram_message_id bigint not null,
    command text not null check (command in ('run', 'pause', 'resume', 'cancel')),
    arguments text not null default '',
    state text not null default 'queued' check (state in
        ('queued', 'running', 'paused', 'finished', 'needs_verification', 'failed', 'cancelled')),
    idempotency_key text not null unique,
    attempt_count smallint not null default 0 check (attempt_count between 0 and 10),
    lease_expires_at timestamptz,
    result jsonb not null default '{}'::jsonb check (jsonb_typeof(result) = 'object'),
    error_code text,
    error_detail text,
    created_at timestamptz not null default now(),
    started_at timestamptz,
    finished_at timestamptz,
    updated_at timestamptz not null default now(),
    constraint orchestration_commands_telegram_message_unique
        unique (telegram_chat_id, telegram_message_id, command)
);

create index if not exists orchestration_commands_claim_idx
    on public.orchestration_commands (state, created_at)
    where state in ('queued', 'running');
create index if not exists orchestration_commands_chat_idx
    on public.orchestration_commands (telegram_chat_id, created_at desc);

comment on table public.orchestration_commands is
    'Durable, idempotent inbox for confirmed Telegram commands. Workers only claim a short Redis-free database lease; no external browser action runs in the claim transaction.';
comment on column public.orchestration_commands.idempotency_key is
    'Stable Telegram confirmation-message identity: prevents duplicate run planning after delivery retries or restarts.';

-- Extend the state-machine function rather than weakening any existing graph.
create or replace function public.orchestration_transition_allowed(
    p_entity text, p_from text, p_to text
) returns boolean language sql immutable strict as $$
    select case p_entity
      when 'orchestration_commands' then (p_from, p_to) in (
        ('queued','running'), ('queued','cancelled'),
        ('running','queued'), ('running','paused'), ('running','finished'), ('running','needs_verification'), ('running','failed'), ('running','cancelled'),
        ('paused','queued'), ('paused','cancelled')
      )
      when 'monitoring_sources' then (p_from, p_to) in (
        ('draft','active'), ('draft','paused'), ('draft','disabled'), ('draft','retired'),
        ('active','paused'), ('active','human_verification_required'), ('active','disabled'), ('active','retired'),
        ('paused','active'), ('paused','disabled'), ('paused','retired'),
        ('human_verification_required','active'), ('human_verification_required','paused'),
        ('human_verification_required','disabled'), ('disabled','draft'), ('disabled','retired')
      )
      when 'browser_profiles' then (p_from, p_to) in (
        ('provisioned','ready'), ('provisioned','disabled'), ('provisioned','retired'),
        ('ready','in_use'), ('ready','human_verification_required'), ('ready','disabled'), ('ready','retired'),
        ('in_use','ready'), ('in_use','human_verification_required'), ('in_use','quarantined'), ('in_use','disabled'),
        ('human_verification_required','ready'), ('human_verification_required','quarantined'), ('human_verification_required','disabled'),
        ('quarantined','ready'), ('quarantined','disabled'), ('quarantined','retired'), ('disabled','retired')
      )
      when 'acquisition_batches' then (p_from, p_to) in (
        ('planned','queued'), ('planned','cancelled'), ('queued','running'), ('queued','cancelled'), ('queued','failed'),
        ('running','human_verification_required'), ('running','succeeded'), ('running','failed'), ('running','cancelled'),
        ('human_verification_required','queued'), ('human_verification_required','failed'), ('human_verification_required','cancelled')
      )
      when 'acquisition_batch_items' then (p_from, p_to) in (
        ('queued','running'), ('queued','skipped'), ('queued','cancelled'),
        ('running','awaiting_human_verification'), ('running','succeeded'), ('running','failed'), ('running','cancelled'),
        ('awaiting_human_verification','queued'), ('awaiting_human_verification','failed'), ('awaiting_human_verification','cancelled')
      )
      when 'batch_runs' then (p_from, p_to) in (
        ('queued','running'), ('queued','cancelled'), ('running','human_verification_required'),
        ('running','succeeded'), ('running','partial'), ('running','failed'), ('running','stopped'), ('running','cancelled'),
        ('human_verification_required','queued'), ('human_verification_required','stopped'), ('human_verification_required','cancelled')
      )
      when 'acquisition_runs' then (p_from, p_to) in (
        ('queued','running'), ('queued','cancelled'), ('running','awaiting_human_verification'),
        ('running','succeeded'), ('running','partial'), ('running','failed'), ('running','stopped'), ('running','cancelled'),
        ('awaiting_human_verification','queued'), ('awaiting_human_verification','stopped'), ('awaiting_human_verification','cancelled')
      )
      when 'collected_posts' then (p_from, p_to) in (
        ('discovered','normalised'), ('discovered','rejected'), ('discovered','expired'),
        ('normalised','analysed'), ('normalised','rejected'), ('normalised','expired'), ('analysed','expired')
      )
      when 'collected_comments' then (p_from, p_to) in (
        ('collected','relevant'), ('collected','irrelevant'), ('collected','expired'),
        ('relevant','expired'), ('irrelevant','expired')
      )
      when 'profile_extracts' then (p_from, p_to) in (
        ('collected','analysed'), ('collected','not_relevant'), ('collected','expired'), ('collected','purged'),
        ('analysed','expired'), ('analysed','purged'), ('not_relevant','expired'), ('not_relevant','purged'), ('expired','purged')
      )
      when 'findings' then (p_from, p_to) in (
        ('draft','ready'), ('draft','dismissed'), ('draft','superseded'), ('ready','delivered'),
        ('ready','delivery_failed'), ('ready','dismissed'), ('ready','superseded'),
        ('delivery_failed','ready'), ('delivery_failed','dismissed'), ('delivery_failed','superseded'), ('delivered','superseded')
      )
      when 'finding_deliveries' then (p_from, p_to) in (
        ('queued','sending'), ('queued','cancelled'), ('sending','sent'), ('sending','failed'), ('sending','cancelled'), ('failed','queued'), ('failed','cancelled')
      )
      when 'verification_jobs' then (p_from, p_to) in (
        ('requested','active'), ('requested','cancelled'), ('requested','expired'),
        ('active','verified'), ('active','rejected'), ('active','expired'), ('active','cancelled')
      )
      else false
    end;
$$;

-- Apply the same update/timestamp guard and append-only audit treatment used
-- by the orchestration tables in migration 003.
drop trigger if exists orchestration_commands_state_guard on public.orchestration_commands;
create trigger orchestration_commands_state_guard
    before update on public.orchestration_commands
    for each row execute function public.enforce_orchestration_transition();
drop trigger if exists orchestration_commands_audit on public.orchestration_commands;
create trigger orchestration_commands_audit
    after insert or update on public.orchestration_commands
    for each row execute function public.audit_orchestration_row();
alter table public.orchestration_commands enable row level security;
