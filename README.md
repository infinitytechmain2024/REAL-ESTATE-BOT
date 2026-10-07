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
`041_web_verification.sql` in order. It records SHA-256 checksums in
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
`035_campaign_specs.sql` adds the structured task (`TaskSpec`, JSON) to campaigns and to the task draft, so the interviewer keeps it across answers.
`036_campaign_finding_clusters.sql` groups the same property seen on several sites into one card (`cluster_id`, `cluster_links`, `duplicate_of`; state `duplicate`).
`037_reach_enrichment.sql` adds the investor reach enrichment to `reach_contacts` (`enriched_at`, `contacts`, `profile_text`, `score`).
`038_finding_review.sql` stores the reviewer's criteria matrix per finding (`review`), the reason a finding was held or excluded (`why`) and marks the user's final report as sent once (`final_report_sent_at`).
`039_campaign_metrics.sql` adds `campaign_metrics` (per-campaign totals for `/campaign report`), the stored card number (`campaign_findings.card_number`), the fetch layer per page (`web_campaign_urls.layer`) and the reason a finding was excluded unsent (`agent_findings.reason`).
`041_web_verification.sql` adds the `web_challenge` verification job type and `verification_jobs.target_url`: the web search hands a CAPTCHA / anti-bot page of a site to a person through the existing verification flow (`WEB_SEARCH_HUMAN_VERIFICATION`, off by default; see [docs/WEB_SEARCH.md](docs/WEB_SEARCH.md)).

The whole pipeline (interviewer, campaigns, web search, analysis, reviewer, final
report) is described in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). See the
[VPS hardening notes](docs/VPS_HARDENING.md) before deployment.

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
  voice, answers the interviewer's questions one at a time (no fixed limit
  of three; at most `INTERVIEW_MAX_ROUNDS`, and «Хватит, ищи» ends the interview) and launches
  the task from the confirmation card with the **Запустить** button; they may also use `/campaign status` and
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
pipeline every `ANALYSIS_POLL_SECONDS` (15). It reads only normalised posts, rejects
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

## Legacy: автономный бот

Прежний автономный Telegram-бот (поиск через встроенный SearXNG, LLM-конвейер,
Render) не входит в кампанейный стек и не разворачивается. Код, тесты, вендоренный
SearXNG, `Dockerfile` и `render.yaml` лежат в [`legacy/`](legacy/README.md).

## Локальная разработка

```sh
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env              # или: python scripts/setup_env.py
make check                        # lint, импорты, тесты
```

`make test` гонит тесты без Postgres (Postgres-тесты пропускаются без
`SYSTEM_TEST_DATABASE_URL`). `ruff format` в проверках нет намеренно: дерево
старше текущего форматтера. Тесты прежнего бота — в `legacy/tests/`, pytest их
не собирает.

## Деплой на VPS

Боевой стек — только `docker-compose.yml`: сервисы перечислены в
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Прежний автономный бот, Tailscale
Funnel, шлюз `FACEBOOK_DESKTOP_*` и Render сюда не относятся (см. `legacy/`).
Браузер с залогиненным профилем Facebook живёт в сервисе `browser`; к нему с
телефона приходят через Telegram Mini App по HTTPS-адресу VPS (Caddy).

### Какая машина нужна

| | минимум | рекомендуется |
|---|---|---|
| vCPU | 2 | 4 |
| RAM | 8 GB | 16 GB |
| Диск | 50 GB NVMe | 100 GB |

ОС: чистый Ubuntu 24.04 LTS, архитектура x86_64. Образы с панелями (CyberPanel,
CloudPanel и подобные) занимают порты 80/443 и память, они только мешают.
Память нужна в первую очередь сервису `browser`: Chromium с профилем Facebook
(0.8–1.5 GB) плюс разовые чтения страниц; если память кончится, ядро убьёт самый
крупный процесс, то есть браузер с залогиненной сессией. Центр обработки данных
выбирайте ближе к региону, из которого аккаунт Facebook обычно входит: чужой IP
чаще вызывает checkpoint.

### 1. Что нужно заранее

- Токен бота от @BotFather (`TELEGRAM_TOKEN`) и ваш числовой Telegram ID
  (`TELEGRAM_OPERATOR_IDS`; бот сообщит его в ответ на `/run`).
- Ключ OpenRouter (`OPENROUTER_API_KEY`): интервьюер, план поиска, анализ,
  рецензент, отчёт и голос работают через него. Без ключа бот стартует, но
  вопросы задают встроенные правила, а находки без проверки модели считаются
  «похожими».
- Необязательно, для сайтов: ключ Google CSE (`GOOGLE_CSE_API_KEY`,
  `GOOGLE_CSE_CX`, второй поисковый бэкенд), прокси с резидентскими адресами
  (`WEB_SEARCH_PROXY_URL`) и scrape API для порталов, которые отказывают и
  HTTP, и браузеру (`WEB_SEARCH_SCRAPE_API_URL`, `WEB_SEARCH_SCRAPE_API_KEY`).
- Имя для HTTPS: не нужна покупка домена, `<ip-через-дефисы>.sslip.io`
  указывает на IP (`203-0-113-7.sslip.io`). Порты 80 и 443 должны быть открыты.

### 2. Docker, swap, код

```sh
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER && newgrp docker

# на машине с 8 GB: swap не заменяет память, но даёт ядру что вытеснить
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

sudo git clone https://github.com/infinitytechmain2024/REAL-ESTATE-BOT.git /opt/real-estate-bot
sudo chown -R $USER: /opt/real-estate-bot
cd /opt/real-estate-bot
cp .env.example .env && chmod 600 .env
```

### 3. `.env`: минимум

Подробные комментарии к каждому ключу — в [`.env.example`](.env.example). Эти
значения нужны, чтобы стек поднялся и бот отвечал:

```sh
TELEGRAM_TOKEN=123456789:AA...
TELEGRAM_OPERATOR_IDS=123456789            # владельцы, через запятую
POSTGRES_PASSWORD=<openssl rand -base64 32>
REDIS_PASSWORD=<openssl rand -base64 32>
# Пароли должны совпасть с теми, что внутри URL:
DATABASE_URL=postgresql://monitoring_app:<POSTGRES_PASSWORD>@postgres:5432/monitoring
REDIS_URL=redis://:<REDIS_PASSWORD>@redis:6379/0
BROWSER_SESSION_API_TOKEN=<openssl rand -base64 32>
OPENROUTER_API_KEY=sk-or-v1-...
# Живой браузер для /login и проверок Facebook:
LIVE_VIEW_DOMAIN=203-0-113-7.sslip.io
LIVE_VIEW_PUBLIC_URL=https://203-0-113-7.sslip.io
LIVE_VIEW_BIND=0.0.0.0                     # открыть 80/443 у Caddy

# необязательно
GOOGLE_CSE_API_KEY=
GOOGLE_CSE_CX=
WEB_SEARCH_BACKENDS=searxng,google_cse     # без ключей Google пропускается
WEB_SEARCH_PROXY_URL=
WEB_SEARCH_SCRAPE_API_URL=
WEB_SEARCH_SCRAPE_API_KEY=
```

`python3 scripts/setup_env.py` спросит ключи и запишет `.env` с правами 0600;
`python3 scripts/fix_env.py` проверит согласованность паролей и URL. Модели
по ролям (интервьюер, архитектор, рецензент, отчёт) и их рекомендуемые значения —
в [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); переменная доходит до контейнера
только если она перечислена в блоке `environment` его сервиса в
`docker-compose.yml`.

Firewall: снаружи нужны только SSH и HTTPS для Caddy; Postgres, Redis, SearXNG и
API браузера наружу не публикуются, а noVNC и порт отладки Chromium открывать
нельзя никогда.

```sh
sudo ufw allow OpenSSH && sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw enable
```

### 4. Миграции и запуск

```sh
./scripts/apply_migrations.sh            # 001 ... 039, с контрольными суммами
docker compose up -d --build            # первая сборка долгая
docker compose ps
curl -fsS http://127.0.0.1:8080/healthz
docker compose logs -f telegram campaign-runner
```

В логах `campaign-runner` должна появиться строка `campaign.runner.ready`, в
логах `telegram` — готовность бота. Если `campaign.runner.relevance_rules_only`
или `discovery_disabled` — не задан `OPENROUTER_API_KEY` или
`BROWSER_SESSION_API_TOKEN`.

### 5. Вход в Facebook

В личном чате с ботом (вы в `TELEGRAM_OPERATOR_IDS`):

```text
/login facebook facebook-main
```

Нажмите **Open browser**, войдите в аккаунт руками (в том числе код из SMS),
затем **Done, I am logged in**. Профиль станет `ready`. Двухфакторную
аутентификацию на рабочем аккаунте лучше выключить: она убирает шаг с кодом, но
не убирает checkpoint. То же для `/login instagram`, `/login tiktok`,
`/login linkedin`, если нужен соцпоиск. Запасной путь без Telegram:
`bash scripts/browser_login.sh facebook facebook-main`.

### 6. Обновление

```sh
cd /opt/real-estate-bot && ./scripts/update.sh
```

Если не помните, где лежит проект: `docker compose ls`, путь в колонке
`CONFIG FILES`. Скрипт берёт свежий код с GitHub (только fast-forward: если на
сервере правили файлы руками, он остановится и покажет какие), скачивает свежие
образы postgres/redis/caddy/searxng, пересобирает образы бота, накатывает
миграции (`scripts/apply_migrations.sh`), пересоздаёт и перезапускает все
контейнеры, удаляет старые образы и показывает `docker compose ps`. Данные
(база, профили браузера, сертификаты) живут в томах и не трогаются. Другая
ветка: `BRANCH=main ./scripts/update.sh`; то же самое: `make update`.

### Проверка после деплоя

Золотая задача: **«квартира в Валенсии до 200 000 €, от 2 комнат, покупка»**.
Отправьте её боту в личном чате от имени одобренного пользователя (или владельца)
и пройдите шаги.

1. **Режим.** `/start`, выберите «🏡 Участки и объекты».
2. **Вопросы.** В этой формулировке сказано всё обязательное (место, сделка,
   тип, бюджет, комнаты), поэтому бот может сразу показать карточку. Чтобы
   увидеть интервью, начните короче: «квартира в Валенсии». Бот задаёт по одному
   вопросу (сделка, бюджет, комнаты) с примерами и кнопками «Не важно»,
   «Хватит, ищи», «Отмена»; ответ на несколько полей одним сообщением принимается.
3. **Карточка ТЗ** («Проверьте задачу»): город Валенсия, покупка, квартира, бюджет
   до 200 000 €, от 2 комнат; кнопки «Запустить / Изменить / Отмена». «Изменить»
   меняет одно поле и возвращает к карточке.
4. **Запуск.** «Запустить»: «Принято. Начинаю поиск.», затем одно сообщение статуса,
   которое редактируется на месте: «Ищу в Facebook…», «Ищу в интернете…» и строка
   по сайтам вида «Сейчас: сайты · idealista.com (браузер) · прочитано 37 ·
   найдено 12 · порталов 4/8» (слой: напрямую, браузер или API). Владельцы видят
   техническую строку с отказами по слоям.
5. **Карточки.** По одной, с хвостом «Найдено: N · ищу дальше». Объект, который
   нашёлся на нескольких сайтах, приходит одной карточкой со строкой «Также на: …».
   Похожие (чуть дороже бюджета) ждут «Одобрить». Каждая точная карточка: город
   Валенсия, цена не выше 220 000 € (бюджет плюс допуск 10 %), от 2 комнат.
6. **Итог.** «Поиск завершён.», затем «📋 Отчёт по поиску»: сколько отправлено и
   отклонено по причинам, 10 лучших карточек, воронка по сайтам, непрочитанные
   сайты и 2–4 рекомендации. Владельцам перед ним приходит «📊 Итог поиска».

Ориентир приёмки (`PLAN.md`): не меньше 30 точных карточек, не меньше 90 % из них
в Валенсии, и по 10 и больше с Idealista и Fotocasa. Если портал не читается,
в отчёте он будет в списке «не прочитались»: включите прокси или scrape API.

```sh
docker compose logs --tail 50 campaign-runner | grep web_search
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

```sql
select bucket, state, count(*) from campaign_findings
 where campaign_id = (select id from campaigns order by created_at desc limit 1)
 group by bucket, state;
```

### Обслуживание

```sh
docker compose logs -f --tail=100 campaign-runner   # логи сервиса
docker compose restart campaign-runner              # перезапуск без пересборки
docker compose down                                 # остановка; никогда down -v
```

Бэкап профилей браузера (в них живые сессии) снимите сразу после первого входа:

```sh
docker run --rm -v real-estate-monitor_browser_profiles:/p -v "$PWD":/b alpine \
  tar czf /b/browser-profiles.tgz -C /p .
```

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
make install       # venv и зависимости
make lint          # ruff
make test          # тесты без Postgres
make check         # lint, импорты, конфиг, миграция, тесты
make docker-up     # весь стек docker compose
```

## Лицензии

Код бота — в этом репозитории. `legacy/searxng/` распространяется под AGPL-3.0-or-later
(см. `legacy/searxng/LICENSE`) и является снимком стороннего проекта, в стеке не используется.
