# REAL-ESTATE-BOT

## VPS foundation (PostgreSQL, Redis, Caddy)

The Docker foundation starts the stateful services required by the monitoring
orchestra. PostgreSQL and Redis are private to Docker; Caddy is the only
gateway and binds to `127.0.0.1:8080` until a production domain and access
policy are configured.

```sh
cp .env.example .env
# Edit .env: at minimum replace POSTGRES_PASSWORD and REDIS_PASSWORD.
docker compose --env-file .env config
docker compose up -d postgres redis caddy
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
./scripts/apply_migrations.sh
```

The migration script applies `001_init.sql` through
`034_web_fetch_layers.sql` in order. It records SHA-256 checksums in
`public.schema_migrations`, locks concurrent runs, and refuses an edited
already-applied migration. Use `docker compose down` for a normal stop; never
use `down -v` on a system containing needed data.
`022_social_search.sql` adds LinkedIn profiles and social network search
(TikTok, Instagram, LinkedIn) bookkeeping.
`023_campaign_finding_relevance.sql` stores the once-per-finding AI verdict
(match / near / reject) of a campaign finding against the campaign's task.
`024_agent_findings.sql` stores every campaign finding before it is sent (and
held / excluded ones): phase 1 of the hybrid pipeline (`docs/HYBRID_AGENTS.md`).
`025_agent_reductions.sql` holds the Reduction agents' claims and decision
traces (Claude extraction, Jev answers, gate), phase 3 in shadow mode.
`026_campaign_comment_leads.sql` queues the Facebook posts a campaign sent for
a comment read, stores the investor / buyer leads found in their comments and
which of them an investor search already sent.
`027_investor_reach.sql` keeps the investor search's reach across platforms
(search-engine results about investors, agents, agencies, funds, networks).
`028_reach_company_kind.sql` adds the `company` kind (a company of the kind the task asks for).
`029_web_search_snippets.sql` keeps the search engine's title and snippet of each queued URL, so a
listing on a site that refuses bots (Idealista) still becomes a card built from the search result.
`030_campaign_summary.sql` marks the end-of-campaign summary (what each source gave) as sent, once.
`031_task_draft_steps.sql` lets a task draft be saved at the `ask` and `target` steps, so clarifying questions are kept.
`032_web_seen_urls_ttl.sql` lets an index (search/list) page be read again after `WEB_SEARCH_INDEX_TTL_DAYS`
and keeps the web stage's browser-render count in the database.
`033_campaign_finding_hold_reason.sql` stores why a finding was held unverified (the AI check could not run), for owners.
`034_web_fetch_layers.sql` tracks a site's refusals and blocks per fetch layer (HTTP, browser) and counts scrape-API reads.

Future Telegram, controlled workers, and persistent browser services are
intentional disabled placeholders under the Compose `future` profile. Their
implementation must have bounded permissions and own health checks before it
is enabled. See [VPS hardening notes](docs/VPS_HARDENING.md) before deployment.

### Open Telegram control plane

The `telegram` Compose service receives only text and voice control messages.
It accepts messages from every Telegram user and chat, records each inbound
message with a unique `(chat_id, message_id)` idempotency key, and transcribes
operators' voice notes with OpenRouter (`STT_MODEL`, default
`openai/whisper-large-v3-turbo`) using the existing `OPENROUTER_API_KEY`. Each
voice note is one bounded request (`STT_MAX_AUDIO_BYTES`,
`STT_MAX_AUDIO_SECONDS`, `STT_TIMEOUT_SECONDS`) that is never retried; the
transcript, detected language, model, HTTP status and the exact cost OpenRouter
returns are stored on the message row, and failures are stored with an
`error_code`. Duplicates, non-operators and over-limit notes are refused before
any download or paid call. Anyone can use `/status` and
`/help`, but only the Telegram user IDs in `TELEGRAM_OPERATOR_IDS` can use
`/run`, `/pause`, `/resume`, `/cancel`, or `confirm`. Everyone else is told
their own user ID, which is how an operator finds the value to add. An empty
list refuses every state change. The dispatcher checks the list again before
acting, so commands queued by someone who is no longer an operator are rejected.
Operator commands also require a short-lived `confirm <token>` response. A confirmed command
is durably queued for the Main Orchestra. The dispatcher validates a tiny
command grammar, selects a bounded acquisition plan, and writes the plan plus
audit records to PostgreSQL. It never launches a collector, browser, shell,
or unrestricted agent process itself.

After setting `TELEGRAM_TOKEN` and `TELEGRAM_OPERATOR_IDS`, apply migrations before starting it:

```sh
./scripts/apply_migrations.sh
docker compose up -d --build telegram
docker compose logs -f telegram
```

### Access from Telegram: owners, operators, users and helpers

`TELEGRAM_OPERATOR_IDS` in `.env` names the **owners**. Everyone else can ask:
anyone without access sees a **Request access** button (on /help, /status or
any refused command). Every owner then gets the request with the person's
name, @username and ID and four buttons:

- **Approve as helper** -- human verification only: the helper gets the
  verification messages (log in, CAPTCHA, checkpoint), opens the browser,
  presses Solved / Resume, and may use `/login`. Nothing else.
- **Approve as user (Пользователь)** -- no verification, no browser, no
  control: the user picks a mode (/start), describes a search task in text or
  voice, answers up to three clarifying questions and launches it with the
  **Запустить** button; they may also use `/campaign status` and
  `/campaign cancel <id>` for their own campaigns (see
  [docs/CAMPAIGNS.md](docs/CAMPAIGNS.md)). User campaigns share the global
  Facebook quotas and breakers.
- **Approve as operator** -- verification plus control of collection
  (`/run`, `/pause`, `/resume`, `/cancel`, voice commands, detailed `/status`).
- **Deny** -- the person may ask again after 24 hours.

Approvals take effect immediately, survive restarts and are stored with who
decided (migrations `011_operator_access_requests.sql`,
`018_user_role_task_drafts.sql`); they are not written
to `.env`. Owners manage them with `/operators`, `/role <ID> helper|user|operator`
and `/revoke <ID>`; only owners can approve, change or revoke, and owners
themselves can only be changed in `.env`. Administrative notices -- access
requests, a verification marked Failed, an expired job, identity checks, new
2FA and account restrictions -- go to owners only. Everyone who approves must
remember that helpers and operators open the browser logged into the Facebook
account.

### Main Orchestra dispatcher

Campaigns, auto mode and the operator how-to (in Russian): [docs/CAMPAIGNS.md](docs/CAMPAIGNS.md).

The dispatcher runs inside the Telegram service and claims confirmed commands
from a PostgreSQL inbox with an expiring lease. A restart requeues only an
expired claim, and each Telegram confirmation message has a unique idempotency
key. It supports:

- `/run facebook-group(s) <https-url> [...]`: queues a 1–20 group Facebook
  batch using the dedicated connector.
- `/run website <https-url>`: queues one HTTP-first Scrapling run limited to
  one explicit page and 45 seconds. It has no browser profile or browser API
  access.
- `/run instagram|tiktok <https-url>` and `/run facebook <https-url>`: queue
  a single Agent Reach-compatible run limited to five pages and 120 seconds.
- `/pause source:<uuid>`, `/resume source:<uuid>`, and
  `/cancel batch:<uuid>|run:<uuid>|command:<uuid>|all`.

Planning and completing a command happen in one transaction, so a crash never
leaves a half-planned or duplicate batch. `/run` refuses a source that is
paused, disabled, retired, deleted, or waiting for human verification.
Cancelling a running Facebook batch stops the collector before its next group;
the group in progress finishes and the browser profile is handed back. A
running Agent Reach or Facebook item run is not cancelled directly: cancel its
batch instead. Every change is audited with the Telegram user as the actor.

An active, platform-matched browser profile must already be provisioned in
PostgreSQL. The dispatcher only writes plans; long-running workers in the
default stack pick them up from PostgreSQL, one at a time
(`facebook-runner`, `scrapling-worker`, `agent-reach-worker`). The Telegram
bot never turns chat input into Docker, shell or browser launches: a worker
runs only a plan the Orchestra validated, with its own fixed limits.

#### Safety quotas and circuit breakers

`/run` is checked before anything is written, from the lifecycle tables
themselves (rolling 24 hours, serialised with an advisory lock):

| Limit | Default |
| --- | --- |
| Facebook batches per day (`SAFETY_MAX_FACEBOOK_BATCHES_PER_DAY`) | 6 |
| Facebook group reads per day (`SAFETY_MAX_FACEBOOK_GROUPS_PER_DAY`) | 60 |
| Single Scrapling / Agent Reach runs per day, per method (`SAFETY_MAX_RUNS_PER_DAY`) | 40 |
| Breaker: challenges on a platform within the window (`SAFETY_BREAKER_CHALLENGES`) | 2 |
| Breaker: failed runs of a method since its last success, within the window (`SAFETY_BREAKER_FAILURES`) | 3 |
| Breaker window (`SAFETY_BREAKER_WINDOW_HOURS`) | 6 h |

A refused command is answered in Telegram ("cannot run: daily quota
reached ..." or "safety breaker open ...") and recorded as a failed command
with `precondition_failed`; no batch or run is created. Breakers close on
their own when the window passes (failures also after the next success).

### Bounded analysis pipeline

`analysis-worker` (default stack; idle without `OPENROUTER_API_KEY`) runs the
pipeline every `ANALYSIS_POLL_SECONDS` (60). It reads only normalised posts, rejects
stale/spam/irrelevant evidence deterministically, then requests strict JSON
from OpenRouter with the same `OPENROUTER_API_KEY`. It stores the model,
prompt version, language, confidence and a stable finding key (migration
`008_analysis_pipeline.sql`). Optionally set `ANALYSIS_TELEGRAM_CHAT_ID` to
send an idempotent digest to one chat.

Sources created by `/run` are analysed for both verticals. Each post is held
by a durable claim (migration `013_analysis_claims.sql`) while OpenRouter is
called, so two workers never pay for the same post and a crashed worker's
posts are retried after `ANALYSIS_CLAIM_SECONDS`. A post ends `analysed` (at
least one finding) or `rejected`. Digests are split to fit Telegram, never
truncated; one Telegram refused stays `queued` and is sent on the next cycle,
after which its findings are `delivered`. A single manual cycle:

```sh
docker compose --env-file .env --profile analysis run --rm analysis-pipeline
```

It never browses, follows links, or sends raw post text as instructions.

### Future Supabase integration

Supabase is not connected by the monitoring foundation or its Telegram control
plane. The local Docker PostgreSQL database remains the primary database and
the migration runner is the only supported schema path today. `.env.example`
reserves `SUPABASE_URL`, `SUPABASE_KEY`, and `SUPABASE_SERVICE_ROLE_KEY` for a
future server-side integration. Never expose the service-role key to a client,
browser, logs, or source control.

### Dedicated Facebook batch collector

The collector processes one already-queued `facebook_connector` batch at a
time. Migration `003_orchestration.sql` is authoritative: it permits at most
20 ordered groups per batch. The collector limits each group to 1–20 newest
post candidates (15 by default), uses the Browser Session Manager lease, and
inserts a short randomized pause between groups. It stops immediately on
checkpoint/login/CAPTCHA/account-warning signals, opens a verification job,
and releases the browser in `VERIFICATION_REQUIRED` state.

A group counts as inaccessible only when no posts were read and the page says
so; joined private groups are read normally. Each snapshot waits (bounded) for
Facebook's feed to render and scrolls it three times before extracting posts.

#### Logging a profile in, and clearing checkpoints, from Telegram

A new profile is logged out, and a checkpoint puts it in
`human_verification_required`. Both are fixed by a human in the real browser,
and the bot brings that browser to the operator's phone:

- `/login` (or `/login facebook <profile-name>`) in a private chat with the
  bot creates the profile if needed and answers with three buttons.
- When a collector hits a checkpoint, the bot sends the same message to every
  operator on its own, within `LIVE_VIEW_POLL_SECONDS`. A request nobody opens
  lapses after `LIVE_VIEW_REQUEST_MINUTES` and is sent again.
- **Open browser** is a Telegram Mini App. Telegram signs the operator's
  identity into it; the gate (`bot/control_plane/live_view.py`) checks that
  signature and `TELEGRAM_OPERATOR_IDS`, then has the browser service open the
  profile under the collectors' lease and start noVNC with a one-time
  password. Only that operator's session gets through; a forwarded message
  opens nothing. The window closes after `LIVE_VIEW_OPEN_MINUTES`.
- Log in, enter the SMS or authenticator code, or pass the check by hand, then
  press **Done, I am logged in**: the window closes, the login is saved in the
  profile, the profile becomes `ready`, open `facebook_challenge` jobs are
  resolved and held sources become `active`. **Close** leaves everything as it
  was. The bot never types, clicks or solves anything in that window.

There is one virtual display, so the window refuses to open while a collector
is running. Every session is recorded in `live_view_sessions` (migration 007).

It needs a public HTTPS name for the VPS. No domain purchase is needed:
`<ip-with-dashes>.sslip.io` resolves to the IP. In `.env`:

```sh
LIVE_VIEW_DOMAIN=203-0-113-7.sslip.io
LIVE_VIEW_PUBLIC_URL=https://203-0-113-7.sslip.io
LIVE_VIEW_BIND=0.0.0.0     # publish Caddy's 80/443; loopback by default
```

Ports 80 and 443 must be open in the VPS firewall. Caddy obtains the
certificate and serves only `/live/*`; everything else is 404.

The terminal route below still works when Telegram is not an option:

```sh
bash scripts/browser_login.sh facebook facebook-main
```

The script creates the `browser_profiles` row if needed and refuses a profile a
collector is using. It then opens Chromium on the profile under the same
lease and lock collectors use, and starts noVNC for that session only, with a
one-time password printed in the terminal. noVNC is never published on the
host; the script prints an SSH tunnel to the browser container's Docker bridge
address, which only the VPS itself can reach:

```sh
ssh -N -L 6090:<browser-container-ip>:6080 <user>@<vps-host>   # on your own computer
# then open http://localhost:6090/vnc.html
```

Log in or clear the checkpoint, press Ctrl+C in the script, and confirm. The
profile becomes `ready`; open `facebook_challenge` verification jobs are
resolved and sources held for verification become `active` again. Batches that
a checkpoint stopped stay as they are; cancel them and `/run` again.

#### Human verification flow

`bot/verification` handles the jobs a challenge creates, end to end, as a
Telegram Mini App: nobody installs anything, and several people can share
the work.

1. The collector stops on a checkpoint, CAPTCHA, login page or account
   warning and opens a `verification_jobs` row, as before.
2. Within `VERIFICATION_POLL_SECONDS` everyone in `TELEGRAM_OPERATOR_IDS`
   gets a Telegram message with an **Open verification page** button. It
   opens `https://<LIVE_VIEW_DOMAIN>/verify/...` inside Telegram. The link's
   token is single-use, expires after `VERIFICATION_TOKEN_MINUTES`, and is
   bound to that job, that Telegram user and that browser profile; only its
   SHA-256 is stored. Unused links are renewed every
   `VERIFICATION_RENOTIFY_MINUTES`.
3. Telegram signs who pressed the button into the page. The service checks
   that signature first, then that the user is an operator, then that the
   link was sent to that very user -- so a forwarded message opens nothing
   and cannot even use up the link. The page session is an HttpOnly, Secure,
   SameSite=Strict cookie bound to job, user and profile; every button
   carries a CSRF token; the pages carry no script of their own.
4. On the page: **Claim** (one operator holds the job; the others see it
   taken), **Open live browser** (the profile's own browser through noVNC,
   same lease as collectors), **Solved**, **Cancel** (the stopped batch is
   cancelled) and **Failed** (the batch fails, the profile is quarantined,
   the owner is told).
5. **Solved** closes the window and a watchdog takes the profile's lease,
   reloads the page that was challenged and checks it with the collector's
   detector plus the flow's own classifier. Only a clean page marks the job
   `verified` and the profile `ready`. **Resume the run** checks once more,
   then requeues the stopped batch from the challenged group (the attempt is
   kept as `stopped`), and `facebook-runner` starts it automatically; the
   operator is told when it starts and how it ends (see *Running a batch*).
   Passwords and codes are typed into the server's browser only; the bot
   never sees them.
6. Identity verification, new two-factor enrolment and account restrictions
   are never offered a browser: the profile is quarantined, links are
   revoked, and only the owner (`VERIFICATION_OWNER_TELEGRAM_ID`, by default
   the lowest operator ID) gets a message plus a one-time link to close the
   job.

Every step is recorded in the append-only `verification_events` table
(detected, notified, token_issued, opened, access_denied, claim, view, solve,
recovery_confirmed, recovery_failed, resume, cancel, fail, sensitive_stop,
expire), and state changes also land in `orchestration_audit_log` with the
operator as actor. Unsolved jobs expire after `VERIFICATION_JOB_HOURS`.

It runs in the default stack behind Caddy, on the same HTTPS name as the
`/login` window (`LIVE_VIEW_DOMAIN` / `LIVE_VIEW_PUBLIC_URL`, see below);
without that name it stays idle. Whoever opens the window controls the
Facebook account, so list only trusted people in `TELEGRAM_OPERATOR_IDS`.
While the verification service handles a job, the `/login` watcher stays quiet
about that profile, so each checkpoint produces one message.

#### Opening the window in Safari or a desktop browser

Every `/login` and verification message also carries its link as text, so it
can be copied into Safari or a desktop browser and used full size. There is no
Telegram signature there, so the page offers **Continue in this browser**, and
the bot then asks the message's recipient to approve that browser. The
message shows the browser's user agent and IP. `/login` asks with an
**Approve** button in the chat; a verification link asks with a signed Mini
App button. Only the browser that asked, which holds a short-lived pending
cookie, gets the session, once, and only after that person approves. A
forwarded link is useless without that approval. At most three requests per
link; each lapses after 10 minutes. Inside Telegram the Mini App now opens
full screen, and vertical swipes no longer close it while you use the remote
page.

#### Running a batch

A confirmed `/run facebook-groups ...` starts by itself: the Orchestra writes
a launch request with the batch and `facebook-runner` runs it (see below).
To run a queued batch by hand instead (safe: a batch is claimed only once):

```sh
FACEBOOK_BATCH_ID=<queued-batch-uuid> docker compose --profile collector up --build facebook-collector
```

Each group's latest read is explained in
`monitoring_sources.configuration->'facebook_last_read'` and in the collector's
log: article counts, how many had a post link, whether a feed rendered, the
final URL and page title, but no post content. A read that yields no posts or
fails also saves a screenshot in the private `browser_screenshots` volume and
records its file name there. Those screenshots show group content, so delete
them once diagnosed.

If the collector dies mid-batch, nothing stays locked for long. The Browser
Session Manager closes a session that makes no request for
`BROWSER_IDLE_SECONDS` (300 by default). The dispatcher fails a running batch
with no progress for `ORCHESTRA_STALE_BATCH_SECONDS` (900 by default), skips
its remaining groups, and returns the profile to `ready`.

This one-shot service is not a daemon and contains no Agent Ridge,
analysis, or human-verification UI.

#### Automatic restart after verification

`facebook-runner` (default stack, same image and limits as
`facebook-collector`) starts nothing on its own: a verification **Resume**
writes a row to
`collector_launch_requests` (migration `012_collector_launch_requests.sql`) in
the same transaction that requeues the batch, and the runner claims that row
and runs the batch, one at a time, through the same collector code. Batches
confirmed with `/run facebook-groups` get a launch request the same way, in
the Orchestra's planning transaction. A request waits while an Agent Reach
task holds the Facebook profile.

The operator who resumed hears when the batch starts and how it ends
(finished, cancelled, a new challenge, failed or skipped); failures and skips
also go to the owner. A request not picked up within 5 minutes (runner
stopped) is reported with the manual command, which is safe to use: a batch
can be claimed only once, so the runner then skips it. A runner restarted
mid-batch marks its request `failed` with `runner_restarted`, and the
dispatcher's stale-batch rule closes the batch. The runner polls every
`FACEBOOK_COLLECTOR_RUNNER_POLL_SECONDS` (15 by default).

### Controlled Agent Reach adapter

The `agent-reach` Compose profile is a strictly bounded public-page reader for
future Orchestra fallback tasks. It uses an existing Browser Session Manager
lease and can only take explicit HTTPS targets for `facebook`, `instagram`,
`tiktok`, or `website`. It does not run Agent Reach's upstream CLI: that CLI
can install or execute tools and manage a browser, neither of which is an
acceptable privilege in this system. The adapter supports only
`read_public_page` and `extract_public_text`, visits at most five explicit
pages by default, has a 120-second total limit, never follows discovered
links, and exits for CAPTCHA, checkpoint, login, account-warning, or unusual
activity signals. It cannot join groups, send messages, or change accounts.

`/run instagram|tiktok|facebook <https-url>` queues a run that
`agent-reach-worker` (default stack) claims once the platform's profile is
`ready`. It stores each page read as normalised evidence for analysis. A
challenge stops the run and opens a verification job exactly as a Facebook
batch does; **Resume** in the verification page requeues the run and Cancel /
Failed close it. For a one-off task outside the database:

```sh
AGENT_REACH_TASK_JSON='{"task_id":"task-1","platform":"website","targets":["https://example.org"],"browser_profile_id":"website-main","browser_profile_name":"website-main"}' \
  docker compose --profile agent-reach run --rm agent-reach
```

The JSON result is normalized for the later analysis pipeline. A future
upstream integration must expose a read-only adapter compatible with this
policy; flipping an environment variable cannot enable it.

### Scrapling website connector

The `scrapling-connector` Compose profile is the lightweight choice for a
single ordinary public website. `/run website <https-url>` creates its bounded
database run and `scrapling-worker` (default stack) runs it. To run one by hand:

```sh
SCRAPLING_RUN_ID=<queued-run-uuid> docker compose --profile scrapling run --rm scrapling-connector
```

It makes exactly one HTTPS GET for the requested page, follows at most three
validated HTTPS redirects, blocks local/private DNS and redirect targets,
enforces a 20-second request / 45-second total budget and a 1.5 MB response
cap, then uses `scrapling.Selector` only to parse the already-downloaded HTML.
It does **not** use Scrapling fetchers, spiders, stealth tooling, browser
features, link discovery, challenge bypassing, or browser profiles. Its output
uses the same normalized public-page structure as controlled Agent Reach and
is persisted as a normalized `collected_posts` record for later analysis.

---

Telegram-бот, который ищет объекты недвижимости и потенциальных партнёров по
открытым источникам: разбирает запрос через LLM, ищет в собственном инстансе
SearXNG, читает найденные страницы, фильтрует результаты и присылает каждый
отдельным сообщением.

Материалы для BotFather (имя, описания, команды) — в [`bot_description.md`](bot_description.md).

---

## Как это работает

```
сообщение (текст или голос)
   │
   ├─ голос → STT (Whisper через OpenAI-совместимый API)
   │
   ├─ LLM: извлечение параметров        → ParsedQuery (локация, бюджет, площадь, язык…)
   ├─ QueryBuilder: 4–6 поисковых строк    на языке региона + английском
   ├─ SearXNG: параллельный поиск       → слияние и дедупликация по url_hash
   │  └─ + доп. источники (группы Facebook, Google Maps) — параллельно,
   │     текст поста приходит уже прочитанным
   ├─ Supabase: отсев уже показанного
   ├─ Fetcher: загрузка и извлечение текста топ-N страниц
   ├─ LLM: оценка, фильтрация, структурирование
   └─ Supabase: сохранение (UNIQUE user_id + url_hash) → отправка пользователю
```

После извлечения параметров каждый этап **деградирует, а не падает**: не
работает парсер — ранжируем по сниппетам; не отвечает LLM-ранкер — отдаём
результаты поиска с пометкой; недоступен Supabase — результаты всё равно
отправляются, просто не запоминаются.

Facebook — тот же принцип, только жёстче. Источник читает разметку, которую
Facebook меняет без предупреждения, поэтому «сломался» — это ожидаемое
состояние, а не исключительное. Упал, завис, разлогинился, показал
checkpoint — поиск отвечает тем, что нашёл в вебе, источник попадает в
список недоступных, а через PIPELINE_SOURCE_TIMEOUT_SECONDS зависший
источник просто перестают ждать.
`scripts/pipeline_probe.py` проверяет именно это обещание.

## Структура

```
bot/
├── main.py            точка входа: сборка сервисов, polling или webhook
├── config.py          все настройки из окружения (pydantic-settings v2)
├── handlers/          /start, текст, голос, кнопки, глобальный обработчик ошибок
├── keyboards/         только inline-клавиатуры
├── middlewares/       контекст логов, upsert пользователя, троттлинг
├── models/            Pydantic-модели: ParsedQuery, SearchHit, StructuredResult…
├── prompts/           тексты промптов (extract / rank / details)
├── services/
│   ├── llm/           абстракция провайдеров + менеджер с фолбэком
│   ├── stt/           распознавание речи, та же схема
│   ├── search/        клиент SearXNG и построитель запросов
│   ├── parser/        загрузка страниц и извлечение текста
│   ├── db/            репозиторий Supabase + SQL-миграция
│   └── pipeline.py    оркестрация всего сценария
└── utils/             нормализация URL и url_hash, работа с текстом

searxng/               полный снимок официального SearXNG (см. searxng/VENDOR.md)
docker/entrypoint.sh   запуск SearXNG и бота в одном контейнере
```

## Быстрый старт

### Docker (рекомендуется)

Один контейнер поднимает и SearXNG, и бота — ровно то же самое поедет на Render.

```sh
python scripts/setup_env.py   # спросит ключи и запишет .env
docker compose up --build
```

Скрипт читает ключи скрытым вводом (в терминале и в истории команд они не
остаются) и пишет `.env` с правами `0600`. Ничего никуда не отправляется —
ключи попадают только в локальный файл. Можно и вручную: `cp .env.example .env`
и заполнить `TELEGRAM_TOKEN` плюс ключ одного LLM-провайдера.

### Локально, без Docker

```sh
make install    # venv, зависимости, Chromium для резервного фетчера
make setup      # спросит ключи и запишет .env
make searxng    # терминал 1
make run        # терминал 2
```

То же самое руками, если make не нужен:

```sh
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt \
    -r searxng/requirements.txt -r searxng/requirements-server.txt
playwright install chromium

cp .env.example .env

# терминал 1 — SearXNG
SEARXNG_SECRET=dev-secret \
SEARXNG_SETTINGS_PATH=$PWD/searxng/settings/settings.yml \
PYTHONPATH=$PWD:$PWD/searxng \
granian --interface wsgi --host 127.0.0.1 --port 8888 searxng.api_only:application

# терминал 2 — бот
python -m bot.main
```

Проверить, что поиск отвечает:

```sh
curl -s 'http://127.0.0.1:8888/search?q=land+for+sale+cyprus&format=json' | head -c 400
```

## Локальная разработка

### Проверки

```sh
make check         # всё разом: снимок, lint, импорты, конфиг, миграция, гейт
make lint          # только ruff
make probe-gate    # только живой просмотр Facebook
make check-vendor  # только целостность вендоренного SearXNG
```

`make check-vendor` проверяет, что снимок SearXNG дошёл до репозитория целиком.
Проверка появилась не на пустом месте: правило `data/` в корневом `.gitignore`
(написанное для рабочего каталога бота) не было привязано к корню, а непривязанное
правило совпадает на любой глубине — и git молча не закоммитил
`searxng/searx/data/`. Это пакет из 16 файлов, который SearXNG импортирует при
старте, так что на свежем клоне поиск не поднимался вообще:
`ImportError: cannot import name 'data' from 'searx'`. На машине, где снимок
делали, всё работало — файлы просто лежали на диске. Ни lint, ни байт-компиляция
`searxng/` не трогают, поэтому не поймал никто. Теперь ловит эта проверка — и
заодно любое другое ignore-правило, дотягивающееся до снимка.

`make probe-gate` — единственная проверка здесь, которая гоняет настоящий код
по настоящему сценарию: поднимает заглушки вместо noVNC и websockify и дёргает
гейт так, как это сделал бы телефон (ссылка, страница, сокет, подпротокол,
кадры в обе стороны, закрытие). Браузер ей не нужен, идёт полсекунды, и именно
она поймала три ошибки, из-за которых ссылка из Telegram открывалась в пустоту.
Её же гоняет CI на каждый push.

`ruff format` в `make check` нет намеренно: дерево старше текущего
форматтера, 16 файлов не прошли бы. Это отдельная уборка, а не условие для
того, чтобы проверки вообще были.

### macOS на Apple Silicon

Повседневно бот запускается нативно — `make run`, безо всякого Docker, и
`make browsers` качает arm64-сборку Chromium. Быстро и без эмуляции.

Docker на M-процессоре — это про «проверить перед деплоем», а не про
повседневную работу: образ собирается под `linux/amd64`, потому что Google не
выпускает `google-chrome-stable` под arm64. Docker Desktop прогонит его через
Rosetta (включается в Settings → General → «Use Rosetta for x86/amd64
emulation», заметно быстрее) или через QEMU (медленно, по умолчанию).
Платформа уже закреплена в `docker-compose.yml`, отдельных флагов не нужно.

### Facebook локально

Локально ничего из серверной машинерии не нужно: ни Xvfb, ни x11vnc, ни noVNC,
ни туннеля. Без `FACEBOOK_CDP_URL` бот сам запускает браузер через Playwright и
просто открывает окно у вас на экране — входите и проходите проверку в нём.

Нужен установленный **Google Chrome** (обычное приложение в `/Applications`):
сессия Facebook запускается с `channel="chrome"`, то есть настоящим Chrome, а
не сборкой Chromium из Playwright — у настоящего браузера обычный отпечаток, и
для автоматизации Facebook это строго лучше. `make browsers` его не качает и
не должен.

```sh
FACEBOOK_ENABLED=true
FACEBOOK_HEADLESS=false                   # чтобы окно было видно
FACEBOOK_ADMIN_TELEGRAM_IDS=<ваш id>
FACEBOOK_GROUP_URLS=https://www.facebook.com/groups/...
# FACEBOOK_CDP_URL и FACEBOOK_DESKTOP_PUBLIC_BASE — не задавать
```

Профиль ложится в `./data/facebook_profile` и переживает перезапуски, так что
вход нужен один раз. `/facebook` в Telegram работает и здесь, только кнопки
«Открыть Facebook» не будет: `FACEBOOK_DESKTOP_PUBLIC_BASE` не задан, ссылку
выдавать не на что — бот так и напишет и предложит окно на этой машине. Это не
ошибка, а ровно тот случай, для которого писался запасной текст.

Прочитать реальную группу и посмотреть, что вышло:

```sh
python scripts/facebook_probe.py "<ссылка на группу>" "<поисковая фраза>"
```

### Что на Mac проверить нельзя

Xvfb, x11vnc и noVNC — линуксовые, и весь путь «кнопка в Telegram → токен →
живое окно на сервере» целиком собирается только там. Логику гейта закрывает
`make probe-gate`, всё остальное — только на VPS (или в Docker на линуксовой
машине). Зелёный `make check` про этот стек не говорит ничего.

## Настройка

Все параметры — в [`.env.example`](.env.example) с комментариями. Обязательны
только `TELEGRAM_TOKEN` и ключ одного LLM-провайдера.

### Смена LLM-провайдера

Меняется одной переменной. Все перечисленные провайдеры уже реализованы:

| `LLM_PROVIDER` | Ключ | Комментарий |
|---|---|---|
| `openrouter` | `OPENROUTER_API_KEY` | один ключ, большинство моделей |
| `groq` | `GROQ_API_KEY` | самый быстрый инференс |
| `together`, `fireworks` | `TOGETHER_API_KEY`, `FIREWORKS_API_KEY` | открытые модели |
| `anthropic` | `ANTHROPIC_API_KEY` | Claude, через Messages API |
| `openai` | `OPENAI_API_KEY` | |
| `nvidia` | `NVIDIA_API_KEY` | NIM / каталог NGC |
| `openai_compatible` | `LLM_API_KEY` + `LLM_BASE_URL` | vLLM, Ollama, LM Studio и любой другой совместимый endpoint |

Можно задать цепочку резерва — `LLM_FALLBACK_PROVIDERS=anthropic,groq`. Провайдер
без настроенного ключа просто пропускается, а не считается сбоем.

Разные модели на разные задачи:

```sh
LLM_MODEL_EXTRACT=openai/gpt-4o-mini              # дешёвая, разбирает запрос
LLM_MODEL_RANK=anthropic/claude-sonnet-4.5        # умная, ранжирует и пишет описания
```

### Добавление нового провайдера

Один файл и одна строка декоратора — остальной код не меняется.
Для OpenAI-совместимого API достаточно трёх атрибутов класса:

```python
# bot/services/llm/my_provider.py
from typing import ClassVar

from bot.services.llm.openai_compatible import OpenAICompatibleProvider
from bot.services.llm.registry import register_llm


@register_llm("my_provider")
class MyProvider(OpenAICompatibleProvider):
    name: ClassVar[str] = "my_provider"
    default_base_url: ClassVar[str | None] = "https://api.example.com/v1"
    api_key_env_vars: ClassVar[tuple[str, ...]] = ("MY_PROVIDER_API_KEY",)
```

Импортируйте модуль в `bot/services/llm/__init__.py` — и `LLM_PROVIDER=my_provider`
работает. Для API с другим протоколом наследуйтесь от `LLMProvider` и реализуйте
`chat()`; `chat_structured()` со схемой и починкой невалидного JSON достанется
бесплатно. Речь (`bot/services/stt/`) устроена точно так же.

### Распознавание речи

```sh
STT_PROVIDER=openrouter             # или groq_whisper, openai_whisper, nvidia
STT_MODEL=openai/whisper-large-v3-turbo
```

Голосовые Telegram приходят в OGG/Opus, который принимают все перечисленные
endpoint'ы, — ffmpeg не нужен. `STT_ENABLED=false` вежливо отключает приём
голоса.

## Supabase

Применить миграцию (SQL-редактор Supabase или `psql`):

```sh
psql "$SUPABASE_DB_URL" -f bot/services/db/migrations/001_init.sql
```

Затем указать `SUPABASE_URL` и `SUPABASE_KEY` (**service_role** — на таблицах
включён RLS без разрешающих политик).

Таблицы: `users`, `searches`, `results`, `feedback`.
Защита от дубликатов — ограничение `UNIQUE (user_id, url_hash)` на `results`,
где `url_hash` = SHA-256 от нормализованного URL (`bot/utils/urls.py`: нижний
регистр хоста, без `www.`, без фрагмента, без `utm_*`/`fbclid`, отсортированные
параметры, `http` сведён к `https`). Ограничение действует на пользователя, а не
глобально, — два разных человека могут увидеть один и тот же объект, но каждый
только один раз.

**Без Supabase бот работает**, но: результаты не сохраняются, дедупликация между
сессиями не работает и **кнопки под результатами не показываются** — нажатие
некуда записать. При старте об этом пишется предупреждение.

## Деплой на VPS

### Обновление бота на VPS — одна команда

```sh
cd /opt/real-estate-bot && ./scripts/update.sh
```

Если не помните, где лежит проект: `docker compose ls` — путь в колонке
`CONFIG FILES`.

Скрипт берёт свежий код с GitHub (только fast-forward: если на сервере
правили файлы руками, он остановится и покажет какие), скачивает свежие
образы postgres/redis/caddy/searxng, пересобирает образы бота, накатывает
миграции базы (`scripts/apply_migrations.sh`), пересоздаёт и перезапускает
все контейнеры, удаляет старые образы и показывает `docker compose ps`.
Данные (база, профиль Facebook, сертификаты) живут в томах и не трогаются.
Другая ветка: `BRANCH=main ./scripts/update.sh`. То же самое: `make update`.

Боевой вариант, если нужен Facebook. Браузер с залогиненным профилем должен
жить постоянно, а к нему в любой момент должен прийти человек с телефона —
когда Facebook попросит подтверждение. Render так не умеет: там имеет смысл
только «бот без Facebook» из раздела ниже.

### Какая машина нужна

| | минимум | рекомендуется |
|---|---|---|
| vCPU | 2 | 4 |
| RAM | 8 GB | 16 GB |
| Диск | 50 GB NVMe | 100 GB |

**Архитектура — обязательно x86_64 (amd64).** Google не собирает
`google-chrome-stable` под arm64, поэтому на ARM-машине образ просто не
соберётся; в `docker-compose.yml` платформа закреплена явно.

Почему два ядра — минимум. В момент проверки одно ядро занято Chrome, который
рисует страницу Facebook, второе — x11vnc, который кодирует картинку для
вашего телефона. На одном ядре живой просмотр начинает ощутимо тормозить ровно
тогда, когда по нему нужно попадать пальцем.

Почему 8 GB — минимум. Постоянно висят Chrome с профилем Facebook (0.8–1.5 GB),
SearXNG (~0.3 GB), бот (~0.2 GB), Xvfb с x11vnc (~0.1 GB); на пике добавляется
Chromium парсера — ещё до 1 GB. Если память кончится, ядро Linux убьёт самый
крупный процесс, то есть именно тот Chrome, в котором лежит залогиненная
сессия, и проверку придётся проходить заново.

**ОС:** чистый Ubuntu 24.04 LTS. Образы с панелями (CyberPanel, CloudPanel и
подобные) занимают порты 80/443 и память — они тут только мешают.

**Локация ЦОД:** выбирайте ближе к региону, из которого аккаунт Facebook
логинится обычно. Несовпадение географии IP — главная причина, по которой
checkpoint появляется чаще, чем хотелось бы. Ни один тариф от этого не
избавляет; смягчает только `PARSER_BROWSER_PROXY_URL` с резидентским прокси.

### 1. Docker

```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker
```

На машине с 8 GB стоит добавить swap — он не заменяет память, но даёт ядру
шанс вытеснить что-то неактивное вместо того, чтобы кого-то убить:

```sh
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 2. Код и `.env`

```sh
sudo git clone https://github.com/infinitytechmain2024/REAL-ESTATE-BOT.git /opt/real-estate-bot
sudo chown -R $USER: /opt/real-estate-bot
cd /opt/real-estate-bot
python3 scripts/setup_env.py        # спросит ключи, запишет .env с правами 0600
```

Каталог `data/` переживает пересоздание контейнера — в нём лежат профиль
Chrome и файл токена. Контейнер работает не от root (uid 10001), а bind-mount
перекрывает права, выставленные при сборке, поэтому каталог нужно создать
заранее и отдать ему:

```sh
mkdir -p data && sudo chown 10001:10001 data
```

Без этого Chrome не сможет писать в профиль, и логин не переживёт ни одного
перезапуска.

### 3. Tailscale Funnel

Нужен только для Facebook: это то, что делает кнопку в Telegram открываемой
с любого телефона, не публикуя наружу ни одного своего порта.

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale funnel --bg 8090     # именно 8090 — не 6080 и не порт CDP
sudo tailscale funnel status
```

Funnel включается для tailnet один раз в
[админке ACL](https://login.tailscale.com/admin/acls); если он ещё не включён,
команда сама напечатает нужный фрагмент. Адрес вида
`https://<машина>.<tailnet>.ts.net`, который она выведет, идёт в
`FACEBOOK_DESKTOP_PUBLIC_BASE`. После перезагрузки проверьте
`tailscale funnel status` — конфигурация обычно восстанавливается сама, но если
нет, команду нужно повторить (или положить в systemd-юнит).

### 4. Переменные Facebook

В `.env` (подробные комментарии — в [`.env.example`](.env.example)):

```sh
FACEBOOK_ENABLED=true
FACEBOOK_ADMIN_TELEGRAM_IDS=123456789          # кому придёт кнопка
FACEBOOK_DESKTOP_PUBLIC_BASE=https://<машина>.<tailnet>.ts.net
FACEBOOK_GROUP_URLS=https://www.facebook.com/groups/...
# FACEBOOK_DESKTOP_PIN=1234                    # ещё один рубеж перед живым просмотром
```

Двухфакторную аутентификацию на рабочем аккаунте лучше выключить — она убирает
из входа шаг с кодом. Checkpoint она не убирает: ради него всё это и сделано.

### 5. Запуск

```sh
docker compose up -d --build        # первая сборка долгая: ставится Chrome
docker compose logs -f
```

В логах при `FACEBOOK_ENABLED=true` должны появиться строки `entrypoint:`
про Xvfb, Chrome, x11vnc и noVNC — если какой-то из них нет, дальше смотреть
нечего, Facebook работать не будет.

### 6. Проверка

Сначала — путь «ссылка из Telegram → живой браузер», без Chrome и без телефона:

```sh
docker compose exec bot python scripts/gate_probe.py
```

Скрипт поднимает заглушки вместо noVNC и websockify и дёргает гейт так, как это
сделал бы телефон. Все проверки должны пройти. Это дешевле, чем выяснять, что
ссылка не открывается, в момент, когда checkpoint уже висит.

Потом — по-настоящему: отправьте боту `/facebook`, нажмите кнопку и убедитесь,
что окно Chrome видно и на нажатия реагирует. Сделайте это **до** того, как
понадобится, а не после.

### Лимиты ресурсов

`docker-compose.yml` ограничивает контейнер: `mem_limit`, `cpus` и `shm_size`.
Значения рассчитаны на 4 vCPU / 16 GB; для 2 vCPU / 8 GB в комментарии рядом
указаны другие. Смысл в том, чтобы предел выставил Docker — раньше, чем до
процессов доберётся OOM killer ядра и убьёт залогиненный Chrome.

`shm_size` отдельно: Docker по умолчанию монтирует `/dev/shm` размером 64 MB,
а Chrome держит там разделяемую память между своими процессами. При прокрутке
группы с картинками этого не хватает, и вкладки начинают умирать с «Target
closed» на вид случайно, при свободной памяти на хосте.

### Что смотрит наружу

Ничего, что нужно открывать самому. Бот работает в режиме polling — входящие
подключения ему не нужны; SearXNG, noVNC, x11vnc и порт CDP слушают только
`127.0.0.1`. Единственная дверь снаружи — гейт на 8090, и до него доходит
только трафик через Tailscale Funnel. Поэтому firewall можно закрыть целиком,
кроме SSH:

```sh
sudo ufw allow OpenSSH && sudo ufw enable
```

Никогда не выставляйте наружу 6080 (noVNC) или порт CDP: первый пускает к
браузеру без токена, второй — это полное управление браузером по HTTP без
всякой аутентификации.

### Обслуживание

```sh
docker compose logs -f --tail=100        # логи
docker compose restart bot               # перезапуск без пересборки
git pull && docker compose up -d --build # обновление
tar czf fb-profile.tgz data/             # бэкап профиля (в нём живая сессия)
```

Бэкап профиля стоит снять сразу после того, как вы первый раз прошли вход и
проверку: восстановить его быстрее, чем проходить checkpoint заново.

## Деплой на Render

Вариант для бота без Facebook. Постоянно живого браузера, в который можно
зайти с телефона, здесь не получится — для этого нужен раздел выше.

[`render.yaml`](render.yaml) описывает один Docker-сервис с обоими процессами.

1. New → Blueprint, указать репозиторий.
2. Заполнить переменные, помеченные `sync: false` (токен, ключи, Supabase).
3. Deploy.

По умолчанию это **background worker** в режиме polling: публичный URL не нужен,
а API SearXNG остаётся на loopback, недоступный извне. Фоновые воркеры доступны
только на платных планах.

Для webhook закомментируйте worker и раскомментируйте web-сервис в конце
`render.yaml`, затем задайте `TELEGRAM_WEBHOOK_URL` = адрес сервиса. Наружу
торчит webhook-сервер бота, SearXNG по-прежнему на `127.0.0.1`.

## SearXNG

`searxng/` — полный снимок официального репозитория, коммит и все локальные
изменения зафиксированы в [`searxng/VENDOR.md`](searxng/VENDOR.md).

Требование «только backend + JSON API» выполнено на уровне маршрутизации, а не
удалением файлов, — чтобы снимок можно было обновлять:

- `searxng/settings/settings.yml` задаёт `search.formats: [json]`. В upstream
  стоит `[html]`, при котором `/search?format=json` отдаёт 403.
- `searxng/api_only.py` — WSGI-обёртка, пропускающая только `/search`,
  `/healthz`, `/config`, `/stats` и `/metrics`. На `/`, `/preferences`,
  `/about`, `/static/*`, `/autocompleter` и `/image_proxy` возвращается 404.

Движки: Google, Bing, DuckDuckGo, Brave, Startpage, Mojeek, Qwant, Wikipedia,
Wikidata. Первые четыре и Qwant с Mojeek в upstream выключены по умолчанию,
поэтому включены явно и взвешены — универсальные поисковики выше
энциклопедических.

Обновление снимка — по инструкции в `VENDOR.md`.

## Полезные команды

## Browser session manager

`browser` owns persistent, non-headless Chromium contexts for future controlled
collectors. It is not a scraper and has no published host port. A caller on the
private Docker network must use `Authorization: Bearer $BROWSER_SESSION_API_TOKEN`
to acquire a profile, release its opaque session token, or request a screenshot.
Redis leases and kernel filesystem locks ensure one live browser per profile;
leases expire after a process crash. Profile data and screenshots are stored in
private named Docker volumes with owner-only permissions.

```sh
docker compose up -d --build browser
docker compose logs -f browser
```

Set `BROWSER_SESSION_API_TOKEN` to a unique long secret before starting it.
The persisted states remain the migration-003 values (`ready`, `in_use`,
`human_verification_required`, etc.); `LOCKED` and `COOLDOWN` are transient
operational API conditions and are never written to the database.

```sh
make help          # список целей
make setup         # спросить ключи и записать .env
make install       # venv, зависимости и браузер
make browsers      # только Chromium для Playwright
make run           # бот локально
make searxng       # SearXNG локально
make lint          # ruff
make probe-gate    # живой просмотр Facebook, целиком
make check-vendor  # целостность вендоренного SearXNG
make check         # снимок, lint, импорты, конфиг, миграция, гейт
make check-api     # SearXNG JSON API (SearXNG должен быть запущен)
make docker-up     # то же, что на Render
```

## Лицензии

Код бота — в этом репозитории. `searxng/` распространяется под AGPL-3.0-or-later
(см. `searxng/LICENSE`) и является немодифицированным снимком стороннего проекта.
