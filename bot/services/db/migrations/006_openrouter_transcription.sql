-- Audit columns for hosted (OpenRouter) voice transcription. Additive only:
-- existing rows keep NULLs, and nothing in 004 is rewritten.

alter table public.telegram_inbound_messages
    add column if not exists transcription_provider text,
    -- Exactly what the provider reported as billed, in USD; NULL when absent.
    add column if not exists transcription_cost_usd numeric(14, 10)
        check (transcription_cost_usd is null or transcription_cost_usd >= 0),
    add column if not exists transcription_audio_seconds real
        check (transcription_audio_seconds is null or transcription_audio_seconds >= 0),
    add column if not exists transcription_request_status smallint;

create index if not exists telegram_inbound_messages_transcription_cost_idx
    on public.telegram_inbound_messages (received_at desc)
    where transcription_cost_usd is not null;

comment on column public.telegram_inbound_messages.transcription_cost_usd is
    'Cost returned by the transcription provider for this voice note; the voice budget is the sum of this column.';
