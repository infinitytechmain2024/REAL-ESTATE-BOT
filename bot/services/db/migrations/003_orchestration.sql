-- REAL-ESTATE-BOT :: monitored-source orchestration
--
-- Run after 001_init.sql and 002_facebook.sql:
--   psql "$SUPABASE_DB_URL" -f bot/services/db/migrations/003_orchestration.sql
--
-- This migration intentionally makes lifecycle changes database-enforced.  A
-- worker must create a new run/item to retry terminal work; it may not silently
-- move a completed or failed record back to "running".

create extension if not exists "pgcrypto";

-- A source is the durable operator-approved target.  Credentials, browser
-- cookies, and private profile data never belong in `configuration`.
create table if not exists public.monitoring_sources (
    id                  uuid primary key default gen_random_uuid(),
    platform            text not null check (platform in
                        ('facebook', 'tiktok', 'instagram', 'telegram', 'website')),
    source_kind         text not null check (source_kind in
                        ('group', 'account', 'channel', 'hashtag', 'website', 'feed')),
    vertical             text not null check (vertical in ('real_estate', 'investors', 'both')),
    canonical_url        text not null,
    display_name         text,
    acquisition_method   text not null check (acquisition_method in
                        ('facebook_connector', 'agent_ridge', 'scrapling')),
    state                text not null default 'draft' check (state in
                        ('draft', 'active', 'paused', 'human_verification_required',
                         'disabled', 'retired')),
    risk_level           text not null default 'standard' check (risk_level in
                        ('low', 'standard', 'high')),
    configuration        jsonb not null default '{}'::jsonb check (jsonb_typeof(configuration) = 'object'),
    last_success_at      timestamptz,
    last_failure_at      timestamptz,
    deleted_at           timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint monitoring_sources_url_unique unique (platform, canonical_url),
    constraint monitoring_sources_method_platform check (
        acquisition_method <> 'facebook_connector' or platform = 'facebook'
    )
);
create index if not exists monitoring_sources_ready_idx
    on public.monitoring_sources (state, platform, acquisition_method)
    where state = 'active';
create index if not exists monitoring_sources_retention_idx
    on public.monitoring_sources (deleted_at) where deleted_at is not null;

-- A deliberately small inventory of browser profiles. Profiles are never
-- deleted by a run and cannot be shared by concurrent browser work.
create table if not exists public.browser_profiles (
    id                  uuid primary key default gen_random_uuid(),
    profile_name        text not null unique,
    platform            text not null check (platform in
                        ('facebook', 'tiktok', 'instagram', 'telegram', 'website')),
    state                text not null default 'provisioned' check (state in
                        ('provisioned', 'ready', 'in_use', 'human_verification_required',
                         'quarantined', 'disabled', 'retired')),
    storage_locator      text not null,
    last_verified_at     timestamptz,
    last_used_at         timestamptz,
    deleted_at           timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);
create unique index if not exists browser_profiles_one_in_use_platform_idx
    on public.browser_profiles (platform) where state = 'in_use';
create index if not exists browser_profiles_available_idx
    on public.browser_profiles (platform, state) where state = 'ready';

-- A batch is deliberately separate from its ordered items: an operator can
-- schedule a Facebook group batch of at most 20 without duplicating sources.
create table if not exists public.acquisition_batches (
    id                  uuid primary key default gen_random_uuid(),
    platform            text not null check (platform in
                        ('facebook', 'tiktok', 'instagram', 'telegram', 'website')),
    acquisition_method  text not null check (acquisition_method in
                        ('facebook_connector', 'agent_ridge', 'scrapling')),
    vertical             text not null check (vertical in ('real_estate', 'investors', 'both')),
    state                text not null default 'planned' check (state in
                        ('planned', 'queued', 'running', 'human_verification_required',
                         'succeeded', 'failed', 'cancelled')),
    max_items            smallint not null default 20 check (max_items between 1 and 20),
    requested_by         text not null default 'system',
    started_at           timestamptz,
    finished_at          timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint acquisition_batches_method_platform check (
        acquisition_method <> 'facebook_connector' or platform = 'facebook'
    )
);
create index if not exists acquisition_batches_work_idx
    on public.acquisition_batches (state, created_at)
    where state in ('queued', 'running', 'human_verification_required');

create table if not exists public.acquisition_batch_items (
    id                  uuid primary key default gen_random_uuid(),
    batch_id            uuid not null references public.acquisition_batches(id) on delete cascade,
    source_id           uuid not null references public.monitoring_sources(id) on delete restrict,
    sequence_no         smallint not null check (sequence_no > 0),
    state                text not null default 'queued' check (state in
                        ('queued', 'running', 'awaiting_human_verification', 'succeeded',
                         'failed', 'skipped', 'cancelled')),
    attempt_count        smallint not null default 0 check (attempt_count between 0 and 10),
    started_at           timestamptz,
    finished_at          timestamptz,
    last_error_code      text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint acquisition_batch_items_source_unique unique (batch_id, source_id),
    constraint acquisition_batch_items_sequence_unique unique (batch_id, sequence_no)
);
-- The unique partial index is the database-level serialisation guarantee for
-- each batch.  A worker cannot claim two group items concurrently.
create unique index if not exists acquisition_batch_items_one_active_idx
    on public.acquisition_batch_items (batch_id)
    where state in ('running', 'awaiting_human_verification');
create index if not exists acquisition_batch_items_source_idx
    on public.acquisition_batch_items (source_id, state);

-- Each execution of a batch gets its own row, preserving the plan separately
-- from attempts and allowing a failed batch to be re-run without history loss.
create table if not exists public.batch_runs (
    id                  uuid primary key default gen_random_uuid(),
    batch_id            uuid not null references public.acquisition_batches(id) on delete restrict,
    browser_profile_id  uuid references public.browser_profiles(id) on delete restrict,
    state                text not null default 'queued' check (state in
                        ('queued', 'running', 'human_verification_required', 'succeeded',
                         'partial', 'failed', 'stopped', 'cancelled')),
    started_at           timestamptz,
    finished_at          timestamptz,
    stop_reason          text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);
create unique index if not exists batch_runs_one_active_batch_idx
    on public.batch_runs (batch_id) where state in ('running', 'human_verification_required');
create index if not exists batch_runs_work_idx
    on public.batch_runs (state, created_at) where state in ('queued', 'running', 'human_verification_required');

-- One attempt to collect a single source. Limits are persisted beside the run
-- so Agent Ridge/browser work remains reviewable after the configuration moves.
create table if not exists public.acquisition_runs (
    id                  uuid primary key default gen_random_uuid(),
    source_id           uuid not null references public.monitoring_sources(id) on delete restrict,
    batch_item_id       uuid references public.acquisition_batch_items(id) on delete set null,
    batch_run_id        uuid references public.batch_runs(id) on delete set null,
    browser_profile_id  uuid references public.browser_profiles(id) on delete restrict,
    acquisition_method  text not null check (acquisition_method in
                        ('facebook_connector', 'agent_ridge', 'scrapling')),
    state                text not null default 'queued' check (state in
                        ('queued', 'running', 'awaiting_human_verification', 'succeeded',
                         'partial', 'failed', 'stopped', 'cancelled')),
    max_pages            smallint not null default 10 check (max_pages between 1 and 100),
    max_runtime_seconds  integer not null default 300 check (max_runtime_seconds between 1 and 3600),
    allowed_skills       jsonb not null default '[]'::jsonb check (jsonb_typeof(allowed_skills) = 'array'),
    stop_reason          text,
    error_code           text,
    error_detail         text,
    started_at           timestamptz,
    finished_at          timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);
create unique index if not exists acquisition_runs_one_active_source_idx
    on public.acquisition_runs (source_id)
    where state in ('running', 'awaiting_human_verification');
create index if not exists acquisition_runs_work_idx
    on public.acquisition_runs (state, created_at)
    where state in ('queued', 'running', 'awaiting_human_verification');
create index if not exists acquisition_runs_batch_item_idx
    on public.acquisition_runs (batch_item_id, created_at desc);

-- Normalised collection records. `raw_payload` is an evidence snapshot, never
-- a browser cookie dump. Retention is governed by the application's policy.
create table if not exists public.collected_posts (
    id                  uuid primary key default gen_random_uuid(),
    source_id           uuid not null references public.monitoring_sources(id) on delete restrict,
    acquisition_run_id  uuid references public.acquisition_runs(id) on delete set null,
    platform_post_id    text not null,
    canonical_url        text not null,
    author_handle        text,
    published_at         timestamptz,
    collected_at         timestamptz not null default now(),
    body_text            text,
    media_urls           jsonb not null default '[]'::jsonb check (jsonb_typeof(media_urls) = 'array'),
    raw_payload          jsonb not null default '{}'::jsonb check (jsonb_typeof(raw_payload) = 'object'),
    content_hash         text not null,
    state                text not null default 'discovered' check (state in
                        ('discovered', 'normalised', 'analysed', 'rejected', 'expired')),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint collected_posts_external_unique unique (source_id, platform_post_id),
    constraint collected_posts_content_hash_unique unique (source_id, content_hash)
);
create index if not exists collected_posts_analysis_idx
    on public.collected_posts (state, collected_at)
    where state in ('discovered', 'normalised');
create index if not exists collected_posts_source_published_idx
    on public.collected_posts (source_id, published_at desc nulls last);

create table if not exists public.collected_comments (
    id                  uuid primary key default gen_random_uuid(),
    post_id             uuid not null references public.collected_posts(id) on delete cascade,
    acquisition_run_id  uuid references public.acquisition_runs(id) on delete set null,
    platform_comment_id text not null,
    parent_comment_id   uuid references public.collected_comments(id) on delete set null,
    author_handle        text,
    published_at         timestamptz,
    collected_at         timestamptz not null default now(),
    body_text            text,
    raw_payload          jsonb not null default '{}'::jsonb check (jsonb_typeof(raw_payload) = 'object'),
    state                text not null default 'collected' check (state in
                        ('collected', 'relevant', 'irrelevant', 'expired')),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint collected_comments_external_unique unique (post_id, platform_comment_id)
);
create index if not exists collected_comments_post_idx
    on public.collected_comments (post_id, published_at);
create index if not exists collected_comments_relevance_idx
    on public.collected_comments (state, collected_at)
    where state = 'collected';

-- Public-profile data is limited to the minimised, purpose-bound extract.
-- `profile_key` should be a stable salted hash when a platform identifier is
-- unavailable; do not store email, phone, cookies, or private fields here.
create table if not exists public.profile_extracts (
    id                  uuid primary key default gen_random_uuid(),
    source_id           uuid not null references public.monitoring_sources(id) on delete restrict,
    acquisition_run_id  uuid references public.acquisition_runs(id) on delete set null,
    related_post_id     uuid references public.collected_posts(id) on delete set null,
    related_comment_id  uuid references public.collected_comments(id) on delete set null,
    platform_profile_id text,
    profile_key         text not null,
    profile_url         text,
    public_data         jsonb not null default '{}'::jsonb check (jsonb_typeof(public_data) = 'object'),
    purpose             text not null default 'comment_context' check (purpose in
                        ('comment_context', 'investor_relevance', 'manual_review')),
    state                text not null default 'collected' check (state in
                        ('collected', 'analysed', 'not_relevant', 'expired', 'purged')),
    extracted_at         timestamptz not null default now(),
    expires_at           timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint profile_extracts_has_context check (
        related_post_id is not null or related_comment_id is not null
    )
);
-- Expression index is PostgreSQL 14-compatible while treating a missing
-- parent post/comment as part of the idempotency key.
create unique index if not exists profile_extracts_per_context_unique_idx
    on public.profile_extracts (
        source_id,
        profile_key,
        coalesce(related_post_id, '00000000-0000-0000-0000-000000000000'::uuid),
        coalesce(related_comment_id, '00000000-0000-0000-0000-000000000000'::uuid)
    );
create index if not exists profile_extracts_expiry_idx
    on public.profile_extracts (expires_at)
    where state not in ('purged', 'expired');

-- Findings are the structured, reviewable propositions delivered to Telegram.
create table if not exists public.findings (
    id                  uuid primary key default gen_random_uuid(),
    vertical             text not null check (vertical in ('real_estate', 'investors')),
    source_id           uuid not null references public.monitoring_sources(id) on delete restrict,
    post_id             uuid references public.collected_posts(id) on delete set null,
    profile_extract_id  uuid references public.profile_extracts(id) on delete set null,
    finding_type         text not null check (finding_type in
                        ('real_estate_proposition', 'investor_lead')),
    dedupe_key           text not null unique,
    structured_payload   jsonb not null check (jsonb_typeof(structured_payload) = 'object'),
    confidence           numeric(4,3) not null check (confidence >= 0 and confidence <= 1),
    state                text not null default 'draft' check (state in
                        ('draft', 'ready', 'delivered', 'delivery_failed', 'dismissed', 'superseded')),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint findings_subject_required check (post_id is not null or profile_extract_id is not null),
    constraint findings_vertical_type check (
        (vertical = 'real_estate' and finding_type = 'real_estate_proposition') or
        (vertical = 'investors' and finding_type = 'investor_lead')
    )
);
create index if not exists findings_delivery_idx
    on public.findings (state, vertical, created_at)
    where state in ('ready', 'delivery_failed');
create index if not exists findings_post_idx on public.findings (post_id);

create table if not exists public.finding_deliveries (
    id                  uuid primary key default gen_random_uuid(),
    finding_id          uuid not null references public.findings(id) on delete cascade,
    telegram_chat_id    bigint not null,
    telegram_message_id bigint,
    state                text not null default 'queued' check (state in
                        ('queued', 'sending', 'sent', 'failed', 'cancelled')),
    attempt_count        smallint not null default 0 check (attempt_count between 0 and 10),
    error_code           text,
    sent_at              timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    constraint finding_deliveries_message_unique unique
        (telegram_chat_id, telegram_message_id)
);
create unique index if not exists finding_deliveries_one_live_idx
    on public.finding_deliveries (finding_id, telegram_chat_id)
    where state in ('queued', 'sending');

create table if not exists public.verification_jobs (
    id                  uuid primary key default gen_random_uuid(),
    source_id           uuid not null references public.monitoring_sources(id) on delete restrict,
    acquisition_run_id  uuid references public.acquisition_runs(id) on delete set null,
    job_type            text not null check (job_type in
                        ('facebook_challenge', 'login', 'consent', 'manual_source_review')),
    state                text not null default 'requested' check (state in
                        ('requested', 'active', 'verified', 'rejected', 'expired', 'cancelled')),
    requested_at         timestamptz not null default now(),
    expires_at           timestamptz,
    resolved_at          timestamptz,
    requested_by         text not null default 'system',
    resolved_by          text,
    resolution_note      text,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);
create unique index if not exists verification_jobs_one_open_idx
    on public.verification_jobs (source_id, job_type)
    where state in ('requested', 'active');
create index if not exists verification_jobs_open_idx
    on public.verification_jobs (state, requested_at)
    where state in ('requested', 'active');

-- Append-only audit log. `actor` comes from `set_config('app.actor', ...)`
-- when available; it otherwise records `database` so direct administrative
-- changes remain attributable and visible.
create table if not exists public.orchestration_audit_log (
    id                  bigint generated always as identity primary key,
    occurred_at         timestamptz not null default now(),
    actor               text not null default coalesce(nullif(current_setting('app.actor', true), ''), 'database'),
    entity_type         text not null,
    entity_id           uuid not null,
    action              text not null check (action in ('insert', 'state_transition', 'update')),
    old_state           text,
    new_state           text,
    old_data            jsonb,
    new_data            jsonb not null,
    transaction_id      bigint not null default txid_current()
);
create index if not exists orchestration_audit_entity_idx
    on public.orchestration_audit_log (entity_type, entity_id, occurred_at desc);

comment on table public.monitoring_sources is
    'Operator-approved monitoring targets. deleted_at is a soft-delete marker; retired is terminal.';
comment on table public.browser_profiles is
    'Limited persistent browser-profile inventory; storage_locator must reference protected host storage only.';
comment on table public.acquisition_batches is
    'Ordered, bounded source plan (maximum 20 items), primarily for sequential Facebook processing.';
comment on table public.batch_runs is
    'An immutable execution attempt for an acquisition batch.';
comment on table public.acquisition_runs is
    'A hard-limited attempt to collect one source; max_pages/max_runtime_seconds are enforced worker limits.';
comment on table public.collected_posts is
    'Idempotently collected public post evidence, unique per source/platform post identifier.';
comment on table public.collected_comments is
    'Public comments associated with a collected post.';
comment on table public.profile_extracts is
    'Minimised public-profile context only; expires_at supports retention and purge.';
comment on table public.findings is
    'Structured Real Estate propositions or Investor leads ready for controlled Telegram delivery.';
comment on table public.verification_jobs is
    'Human-in-the-loop verification work; an open job blocks automated browser progress.';
comment on table public.orchestration_audit_log is
    'Append-only lifecycle and configuration audit trail; updates and deletes are rejected.';

-- Authoritative transition graph. Terminal states intentionally have no exit.
create or replace function public.orchestration_transition_allowed(
    p_entity text, p_from text, p_to text
) returns boolean language sql immutable strict as $$
    select case p_entity
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

create or replace function public.enforce_orchestration_transition()
returns trigger language plpgsql as $$
begin
    if new.state is distinct from old.state and not public.orchestration_transition_allowed(
        tg_table_name, old.state, new.state
    ) then
        raise exception 'illegal % state transition: % -> %', tg_table_name, old.state, new.state
            using errcode = '23514';
    end if;
    new.updated_at = now();
    return new;
end;
$$;

create or replace function public.enforce_acquisition_run_source_method()
returns trigger language plpgsql as $$
begin
    if new.acquisition_method = 'facebook_connector' and not exists (
        select 1 from public.monitoring_sources s where s.id = new.source_id and s.platform = 'facebook'
    ) then
        raise exception 'facebook_connector runs require a Facebook source' using errcode = '23514';
    end if;
    if new.browser_profile_id is not null and not exists (
        select 1 from public.browser_profiles p join public.monitoring_sources s on s.id = new.source_id
        where p.id = new.browser_profile_id and p.platform = s.platform
    ) then
        raise exception 'browser profile platform must match acquisition source platform' using errcode = '23514';
    end if;
    return new;
end;
$$;

create or replace function public.enforce_batch_run_profile_platform()
returns trigger language plpgsql as $$
begin
    if new.browser_profile_id is not null and not exists (
        select 1 from public.acquisition_batches b join public.browser_profiles p on p.id = new.browser_profile_id
        where b.id = new.batch_id and b.platform = p.platform
    ) then
        raise exception 'browser profile platform must match batch platform' using errcode = '23514';
    end if;
    return new;
end;
$$;

create or replace function public.enforce_batch_capacity_and_platform()
returns trigger language plpgsql as $$
declare batch_row public.acquisition_batches;
begin
    select * into batch_row from public.acquisition_batches where id = new.batch_id;
    if not found then raise exception 'batch % not found', new.batch_id using errcode = '23503'; end if;
    if (select platform from public.monitoring_sources where id = new.source_id) <> batch_row.platform then
        raise exception 'batch item source platform must match batch platform' using errcode = '23514';
    end if;
    if new.sequence_no > batch_row.max_items then
        raise exception 'batch item sequence exceeds batch capacity (%)', batch_row.max_items using errcode = '23514';
    end if;
    return new;
end;
$$;

create or replace function public.audit_orchestration_row()
returns trigger language plpgsql as $$
declare old_json jsonb; new_json jsonb; row_id uuid;
begin
    if tg_op = 'INSERT' then
        new_json := to_jsonb(new); row_id := new.id;
        insert into public.orchestration_audit_log(entity_type, entity_id, action, new_state, new_data)
        values (tg_table_name, row_id, 'insert', new.state, new_json);
    else
        old_json := to_jsonb(old); new_json := to_jsonb(new); row_id := new.id;
        insert into public.orchestration_audit_log(entity_type, entity_id, action, old_state, new_state, old_data, new_data)
        values (tg_table_name, row_id,
                case when new.state is distinct from old.state then 'state_transition' else 'update' end,
                old.state, new.state, old_json, new_json);
    end if;
    return new;
end;
$$;

create or replace function public.reject_orchestration_audit_mutation()
returns trigger language plpgsql as $$
begin
    raise exception 'orchestration_audit_log is append-only' using errcode = '55000';
end;
$$;

-- State, timestamp, and audit triggers are installed uniformly for every
-- lifecycle table.  Add a table here when adding a new state machine.
do $$
declare table_name text;
begin
    foreach table_name in array array[
        'monitoring_sources', 'browser_profiles', 'acquisition_batches', 'acquisition_batch_items', 'batch_runs',
        'acquisition_runs', 'collected_posts', 'collected_comments',
        'profile_extracts', 'findings', 'finding_deliveries', 'verification_jobs'
    ] loop
        execute format('drop trigger if exists %I on public.%I', table_name || '_state_guard', table_name);
        execute format('create trigger %I before update on public.%I for each row execute function public.enforce_orchestration_transition()', table_name || '_state_guard', table_name);
        execute format('drop trigger if exists %I on public.%I', table_name || '_audit', table_name);
        execute format('create trigger %I after insert or update on public.%I for each row execute function public.audit_orchestration_row()', table_name || '_audit', table_name);
    end loop;
end;
$$;

drop trigger if exists acquisition_runs_source_method_guard on public.acquisition_runs;
create trigger acquisition_runs_source_method_guard
    before insert or update of source_id, acquisition_method, browser_profile_id on public.acquisition_runs
    for each row execute function public.enforce_acquisition_run_source_method();
drop trigger if exists batch_runs_profile_guard on public.batch_runs;
create trigger batch_runs_profile_guard
    before insert or update of batch_id, browser_profile_id on public.batch_runs
    for each row execute function public.enforce_batch_run_profile_platform();
drop trigger if exists acquisition_batch_items_guard on public.acquisition_batch_items;
create trigger acquisition_batch_items_guard
    before insert or update of batch_id, source_id, sequence_no on public.acquisition_batch_items
    for each row execute function public.enforce_batch_capacity_and_platform();
drop trigger if exists orchestration_audit_no_update on public.orchestration_audit_log;
create trigger orchestration_audit_no_update before update or delete on public.orchestration_audit_log
    for each row execute function public.reject_orchestration_audit_mutation();

alter table public.monitoring_sources enable row level security;
alter table public.browser_profiles enable row level security;
alter table public.acquisition_batches enable row level security;
alter table public.acquisition_batch_items enable row level security;
alter table public.batch_runs enable row level security;
alter table public.acquisition_runs enable row level security;
alter table public.collected_posts enable row level security;
alter table public.collected_comments enable row level security;
alter table public.profile_extracts enable row level security;
alter table public.findings enable row level security;
alter table public.finding_deliveries enable row level security;
alter table public.verification_jobs enable row level security;
alter table public.orchestration_audit_log enable row level security;
