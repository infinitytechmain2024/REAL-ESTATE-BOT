-- Restricted Telegram control-plane persistence.  This migration deliberately
-- does not modify the orchestration state-machine schema.

create table if not exists public.telegram_inbound_messages (
    id uuid primary key default gen_random_uuid(),
    telegram_chat_id bigint not null,
    telegram_message_id bigint not null,
    telegram_user_id bigint,
    message_kind text not null check (message_kind in ('text', 'voice')),
    text_body text,
    transcript text,
    detected_language text,
    transcription_confidence real check (transcription_confidence between 0 and 1),
    transcription_model text,
    processing_state text not null default 'received' check (processing_state in ('received', 'processed', 'failed')),
    received_at timestamptz not null default now(),
    processed_at timestamptz,
    error_code text,
    -- The pair is Telegram's durable update identity for one bot/chat.
    constraint telegram_inbound_messages_idempotency unique (telegram_chat_id, telegram_message_id)
);
create index if not exists telegram_inbound_messages_received_idx
    on public.telegram_inbound_messages (received_at desc);

create table if not exists public.telegram_command_confirmations (
    id uuid primary key default gen_random_uuid(),
    token text not null unique check (token ~ '^[a-f0-9]{10}$'),
    telegram_chat_id bigint not null,
    telegram_user_id bigint not null,
    command text not null check (command in ('run', 'pause', 'resume', 'cancel')),
    arguments text not null default '',
    state text not null default 'pending' check (state in ('pending', 'confirmed', 'expired', 'cancelled')),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    confirmed_at timestamptz,
    check (expires_at > created_at)
);
create index if not exists telegram_command_confirmations_pending_idx
    on public.telegram_command_confirmations (telegram_chat_id, telegram_user_id, expires_at)
    where state = 'pending';

comment on table public.telegram_inbound_messages is
    'Durable inbound Telegram audit and idempotency boundary; content is limited to operator control messages and voice transcripts.';
comment on table public.telegram_command_confirmations is
    'One-time operator confirmations; confirmed tokens cannot be reused.';
