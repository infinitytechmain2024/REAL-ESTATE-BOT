# Откат v2 (DoD-6) — черновик D3

Дата: 2026-10-15. Для владельца, на VPS в `/opt/real-estate-bot`. Порядок деплоя и бэкапа — `docs/v2/DEPLOY.md`
(пишет deployer) и `docs/v2/OPS.md`. Все переменные и строки ниже сверены с `.env.example` и `docker-compose.yml`
этой ветки; ничего другого в `.env` для отката не нужно.

## 0. Как откатывать любую строку

```bash
cd /opt/real-estate-bot && cp .env .env.bak.$(date +%F-%H%M)
# поправить ОДНУ строку в .env (значение из колонки «Откат»), затем:
docker compose --env-file .env up -d <сервисы из колонки «Сервисы»>
docker compose --env-file .env ps                      # сервисы Up / healthy
docker compose --env-file .env logs --tail=30 <сервис> | grep -iE "error|traceback" || echo ok
```

`up -d` пересоздаёт только те контейнеры, у которых изменилось окружение. Код и схема БД при этом не меняются.
Умолчания в `docker-compose.yml` совпадают со значениями отката, поэтому удалить строку из `.env` — тоже откат.

## 1. Таблица «часть → флаг → откат»

| # | Часть v2 (задача) | Переменная (`.env.example`) | Откат | Сервисы для `up -d` (`docker-compose.yml`) | Что остаётся в БД | Безопасно оставить? |
|---|---|---|---|---|---|---|
| 1 | Apify-порталы первыми, поисковик запасной, браузерный соцпоиск выкл (A1); группы из задачи без браузера и без `site:`-запросов соцсетей в reach (B3) | `ACQUISITION_MODE` (:260), `APIFY_MIN_LISTINGS` (:261, порог; при `legacy` не читается; умолчание `5`, откат не нужен) | `ACQUISITION_MODE=legacy` | `campaign-runner` (:653) | Новых таблиц нет. Группы из задачи, уже поставленные в `campaign_groups` (seed `required`), остаются | Да: при `legacy` читаются обычным порядком |
| 2 | Закрытая группа FB → «вступите» → кнопка «Вступил ✅» → браузер (A2) | `CAMPAIGN_JOIN_NOTICES` (:323) | `CAMPAIGN_JOIN_NOTICES=false` | `campaign-runner` (:687), `telegram` (:102) | 047: `campaign_groups.join_notice_at`, `join_confirmed_at` | Да: при `false` код их не читает (`bot/campaign/runs.py:453`) |
| 3 | Порталы P2: Pisos, Habitaclia, Milanuncios (B5) | `APIFY_PISOS_ENABLED` (:133), `APIFY_HABITACLIA_ENABLED` (:138), `APIFY_MILANUNCIOS_ENABLED` (:143) | каждый `=false` | `campaign-runner` (:569, :573, :577) | Строки `web_listing_source_runs` (044, до v2) | Да |
| 4 | Порталы P1, включаемые в D2 (до v2; A3 — проверка мапперов) | `APIFY_FOTOCASA_ENABLED` (:120), `APIFY_YAENCONTRE_ENABLED` (:128), `APIFY_IDEALISTA_PROVIDER` (:116) | `=false`, `=false`, `APIFY_IDEALISTA_PROVIDER=axlymxp` (или `APIFY_IDEALISTA_ENABLED=false`, :95) | `campaign-runner` (:561, :565, :559, :550) | То же, 043–045 | Да |
| 5 | Группы FB через Apify (A4; адаптер до v2) | `FEATURE_APIFY_FACEBOOK` (:452) | `FEATURE_APIFY_FACEBOOK=false` | `facebook-runner` (:261); `facebook-collector` (:219) — только если запускали профиль `collector` | 046: `apify_runs`, `apify_raw_items`, `campaigns.acquisition_provider` | Да; `apify_raw_items` чистит cron из `OPS.md`. Кампании с `acquisition_provider='apify'` при `false` идут браузером |
| 5a | Охват Apify для FB | `APIFY_FACEBOOK_SCOPE` (:466) | `APIFY_FACEBOOK_SCOPE=campaign` | `facebook-runner` (:270), `facebook-collector` (:228) | — | — |
| 6 | Пост по ссылке → комментарии через Apify → лиды (B1) | `APIFY_FACEBOOK_COMMENTS_ENABLED` (:475) | `APIFY_FACEBOOK_COMMENTS_ENABLED=false` | `campaign-runner` (:583) **и** `telegram` (:131) | 048: таблица `campaign_post_reads`; лиды в `investor_leads` (026) | Да. Ссылка снова становится обычным текстом задачи; открытая кампания-ссылка завершится без чтения (`bot/campaign/runner.py:439-443`, `:586-593`) |
| 7 | Лиды под отправленными объектами через Apify (B2) | `LEADS_PROVIDER` (:489) | `LEADS_PROVIDER=browser` | `campaign-runner` (:591) | 049: `campaign_comment_reads.run_id` (nullable) | Да: браузерный путь колонку не пишет и не читает |
| 8 | Отчёт с разделом «💼 Инвесторы» и расходом по веткам (B4) | `CAMPAIGN_INVESTOR_REPORT` (:493) | `CAMPAIGN_INVESTOR_REPORT=false` | `campaign-runner` (:594) | Нет | — |
| 9 | Архитектор задачи на Claude API (C1-lite) | `ARCHITECT_PROVIDER` (:568); ключ `ANTHROPIC_API_KEY` (:569) и `ANTHROPIC_INTERVIEW_MODEL`/`ANTHROPIC_TIMEOUT_SECONDS` (:572-573) отката не требуют: при `openrouter` не читаются, ключ можно оставить | `ARCHITECT_PROVIDER=openrouter` | `telegram` (:135, ключи :136-138) | Строки `campaign_costs` (stage `llm`) без кампании — только учёт | Да |
| 10 | Watchdog: алерты и дневная сводка (C3) | `WATCHDOG_ENABLED` (:389); `OWNER_ALERT_CHAT_ID` (:390, `0` = нет получателя), `WATCHDOG_DOCKER_GID` (:397, закомментирован, умолчание 999) отката не требуют | мягко: `WATCHDOG_ENABLED=false` (контейнер спит); полностью: убрать `COMPOSE_PROFILES=ops` (:388) и `docker compose --env-file .env --profile ops rm -sf watchdog` | `watchdog` (:834, профиль `ops` :820) | Нет | — |
| 10a | Watchdog: проверка контейнеров через docker.sock | `WATCHDOG_DOCKER_SOCKET` (:396) | пусто | `watchdog` (:840) | Нет | Сокет остаётся смонтирован, пока включён профиль `ops` (`docker-compose.yml:843`); убрать доступ к нему полностью — только снятием профиля (строка 10) |
| 11 | Модели по этапам (A5) — по одной строке на этап | см. §2 | прежний id | см. §2 | Нет | — |
| 12 | Режим «🏡💼 Недвижимость + инвесторы» в меню Telegram (C2) | `INTAKE_BOTH_MODE` (:969) | `INTAKE_BOTH_MODE=false` | `telegram` (:133) | 050: check `user_task_drafts.mode` допускает `both`; черновики `both` (24 ч) | Да: при `false` кнопки нет, черновик `both` начинается заново с меню (`bot/control_plane/intake.py:398-401`). Перед откатом **кода** — раздел «C2» ниже |
| 13 | Карточка объекта: «Площадь» (участок отдельно), «Телефон», «Агентство» (D1a) | `CARD_EXTRA_FIELDS` (:580) | `CARD_EXTRA_FIELDS=false` | `analysis-worker` (:447), `campaign-runner` (:531); `analysis-pipeline` (:350, профиль `analysis`) — если запускается | Поля `plot_m2`, `phone`, `advertiser` в `findings.structured_payload` уже сохранённых находок | Да: при `false` новые находки их не пишут, карточка их не выводит. ⛔ Включать только после O1 (телефон частника = ПДн) |

Номера в скобках: `(:N)` в колонке переменной — строка `.env.example`; в колонке сервисов — строка
`docker-compose.yml`.

Без своего флага (не откатываются через `.env`):
- **B3** живёт под `ACQUISITION_MODE` (строка 1) и `FEATURE_APIFY_FACEBOOK`/`APIFY_FACEBOOK_SCOPE` (строки 5, 5a).
- **C3 healthcheck** и **C4** (ротация логов `x-logging`, `scripts/backup.sh`, cron) — это не функции, а
  эксплуатация. Откат: строка `logging: *logging` у сервиса (`OPS.md` «Ротация логов»), строки в `crontab -e`
  (`OPS.md` «Расписание»). Healthcheck откатывается только откатом кода.

## 2. Модели (A5)

Сейчас рабочие строки `.env.example` содержат **прежние** id, а v2-значения лежат закомментированными строками над
ними (`docs/v2/MODELS.md`). Откат — вернуть прежний id в `.env` VPS.

| Этап | Переменная (`.env.example`) | Прежний id (откат) | Сервисы (`docker-compose.yml`) |
|---|---|---|---|
| анализ страницы | `OPENROUTER_ANALYSIS_MODEL` (:555) | `anthropic/claude-sonnet-4.5` | `analysis-worker` (:442); `analysis-pipeline` (:347, профиль `analysis`) — если запускается |
| отбор (reviewer) | `OPENROUTER_REVIEW_MODEL` (:305) | `anthropic/claude-sonnet-4.5` | `campaign-runner` (:680) |
| план поиска | `OPENROUTER_PLAN_MODEL` (:163) | `anthropic/claude-sonnet-4.5` | `campaign-runner` (:671) |
| итоговый отчёт | `OPENROUTER_FINAL_MODEL` (:320) | `anthropic/claude-sonnet-4.5` | `campaign-runner` (:685) |
| запросы web | `OPENROUTER_WEB_QUERY_MODEL` (:158) | `openai/gpt-4o-mini` | `campaign-runner` (:604) |
| лиды | `OPENROUTER_LEADS_MODEL` (:337) | `openai/gpt-4o-mini` | `campaign-runner` (:696) |
| reach | `OPENROUTER_REACH_MODEL` (:349) | `openai/gpt-4o-mini` | `campaign-runner` (:705) |
| интервью | `OPENROUTER_INTERVIEW_MODEL` (:561) | `anthropic/claude-sonnet-4.5` | `telegram` (:127) |

Признак, что откат нужен: 400 от OpenRouter в логах сервиса, посты анализа закрываются с `error`, в отчёте
«⚠️ Ошибки ИИ» (`bot/campaign/final_report.py:657`).

## 3. Откат кода

Порядок и команды — `docs/v2/DEPLOY.md` (раздел отката) и скилл `.claude/skills/deploy-vps/SKILL.md` п. 5:
бэкап (`scripts/backup.sh`) → `git checkout <коммит до v2>` → `docker compose --env-file .env up -d --build`.

- Сначала флаги (§1) в значения отката, потом код. Если откатываете на версию без C2 — сначала
  `update user_task_drafts set mode = null where mode = 'both';` (раздел «C2» ниже). Старый код новые колонки и
  таблицу не видит, миграции 047–050 только добавляют или расширяют check (`ADD COLUMN IF NOT EXISTS`, `CREATE TABLE IF NOT EXISTS`), поэтому **схему после отката кода
  оставляем как есть**.
- Не запускайте `scripts/update.sh` с веткой v2 после отката схемы: он снова применит 047–050
  (`scripts/apply_migrations.sh:78-81`).

## 4. Откат миграций (только если без него нельзя)

Не нужен для отката функций: все четыре миграции добавляющие, данные в них — журнал платных запусков и отметки
пользователя. Если всё же нужно убрать схему: **сначала откат кода (§3), бэкап**, затем строго
**050 → 049 → 048 → 047**. Скрипты не применяются автоматически.

```bash
cd /opt/real-estate-bot && set -o pipefail && ./scripts/backup.sh
psql_db() { docker compose --env-file .env exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'; }
psql_db < scripts/rollback/050_task_drafts_mode_both_down.sql
echo "delete from schema_migrations where filename = '050_task_drafts_mode_both.sql';" | psql_db
psql_db < scripts/rollback/049_campaign_comment_reads_run_id_down.sql
echo "delete from schema_migrations where filename = '049_campaign_comment_reads_run_id.sql';" | psql_db
psql_db < scripts/rollback/048_campaign_post_reads_down.sql
echo "delete from schema_migrations where filename = '048_campaign_post_reads.sql';" | psql_db
psql_db < scripts/rollback/047_campaign_group_join_down.sql
echo "delete from schema_migrations where filename = '047_campaign_group_join.sql';" | psql_db
```

Каждый скрипт откатывается в транзакции и **отказывает** (`raise exception`, ничего не меняет), если данные есть:

| Скрипт | Отказывает, если | Что это значит |
|---|---|---|
| `scripts/rollback/049_campaign_comment_reads_run_id_down.sql` | в `campaign_comment_reads` есть строка с `run_id is not null` | был хотя бы один платный Apify-запуск B2 |
| `scripts/rollback/048_campaign_post_reads_down.sql` | в `campaign_post_reads` есть хотя бы одна строка | был хотя бы один пост по ссылке (B1) |
| `scripts/rollback/047_campaign_group_join_down.sql` | в `campaign_groups` есть `join_notice_at` или `join_confirmed_at` | бот хотя бы раз отправил «вступите» |
| `scripts/rollback/050_task_drafts_mode_both_down.sql` | в `user_task_drafts` есть черновик с `mode = 'both'` | C2: кто-то выбрал «Недвижимость + инвесторы» (черновик 24 ч; обнулить: `update user_task_drafts set mode = null where mode = 'both';`) |

Отказ — штатный исход: значит, откат схемы не нужен, оставьте её. Удалять эти данные ради отката не рекомендуется
(это учёт расходов и аудит); если владелец решит иначе — отдельная задача db-агенту с «ок» владельца.
Восстановление БД из бэкапа — только в крайнем случае (`OPS.md` «Бэкапы», `scripts/restore_check.sh`).

## C2: режим «Недвижимость + инвесторы»

Откат функции: `INTAKE_BOTH_MODE=false` в `.env` и перезапуск `telegram` (кнопки нет; уже начатый черновик `both`
начинается заново с меню). Перед откатом **кода** на версию без C2: `update user_task_drafts set mode = null where
mode = 'both';` (старый код не знает этот режим). Кампании `both` в `campaigns` остаются рабочими. Миграция 050
расширяющая; откат схемы — `scripts/rollback/050_task_drafts_mode_both_down.sql` (строка в таблице §4), первой в
порядке §4 и только после отката кода.
