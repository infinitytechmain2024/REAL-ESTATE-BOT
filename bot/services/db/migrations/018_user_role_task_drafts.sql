-- The `user` role (Пользователь): approved by an owner like helpers and
-- operators, but only picks a mode and gives search tasks in Telegram; each
-- launch is confirmed with a button. No verification, no control.

alter table public.telegram_operators
    drop constraint if exists telegram_operators_role_check;
alter table public.telegram_operators
    add constraint telegram_operators_role_check
    check (role in ('helper', 'user', 'operator'));

-- One task being written per person: the chosen mode, the answers to the
-- clarifying questions and the step reached. Launching resets it atomically,
-- so a double tap on "Запустить" queues one campaign.
create table if not exists public.user_task_drafts (
    telegram_user_id    bigint primary key,
    telegram_chat_id    bigint not null,
    mode                text check (mode in ('real_estate', 'investors')),
    step                text not null default 'idle'
                        check (step in ('idle', 'city', 'deal', 'budget', 'summary')),
    draft               jsonb not null default '{}'::jsonb check (jsonb_typeof(draft) = 'object'),
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now(),
    launched_at         timestamptz
);

alter table public.user_task_drafts enable row level security;

comment on table public.user_task_drafts is
    'Per-person Telegram task intake: mode (real_estate|investors), clarifying answers and step; reset on launch.';
