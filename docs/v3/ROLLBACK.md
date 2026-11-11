# Откат v3

Для владельца, на VPS в `/opt/real-estate-bot`. Формат — как `docs/v2/ROLLBACK.md` (§0 «как откатывать любую
строку» действует и здесь). Здесь — только части v3. Каждая задача v3 добавляет свою строку.

## 1. Таблица «часть → флаг → откат»

| # | Часть v3 (задача) | Переменная (`.env.example`) | Откат | Сервисы для `up -d` (`docker-compose.yml`) | Что остаётся в БД | Безопасно оставить? |
|---|---|---|---|---|---|---|
| 1 | Общий контракт соцсетей и журнал платных запусков (P1.1) | Нет: код никто не вызывает, флаги заводят источники P1.2–P1.8 | Не нужен | — | 051: таблица `apify_source_runs` (пустая, пока источники не включены) | Да: при выключенных источниках код таблицу не пишет и не читает |
| 2 | Instagram и TikTok через Apify в кампании (P1.8) | `APIFY_INSTAGRAM_ENABLED`, `APIFY_TIKTOK_ENABLED` (действуют только при `ACQUISITION_MODE=apify`); `APIFY_SOCIAL_MAX_TARGETS` — число целей на кампанию | `APIFY_INSTAGRAM_ENABLED=false` и/или `APIFY_TIKTOK_ENABLED=false` (или `ACQUISITION_MODE=legacy`) | `campaign-runner` | Строки в `apify_source_runs`, `social_search_queries`, `campaign_social_state`, `campaign_social_posts`, посты в `collected_posts` (источник `search` платформы) | Да: новых таблиц нет; посты уже проанализированы и остаются в отчётах |
| 2а | То же, откат флагом во время идущей кампании (P1.8) | `APIFY_INSTAGRAM_ENABLED=false` / `APIFY_TIKTOK_ENABLED=false` | Новые цели не планируются и не запускаются. Цели, начатые до перезапуска, остаются в `campaign_social_state` как `pending`/`running`: кампания ждёт их в окне ожидания не дольше `CAMPAIGN_SOCIAL_GRACE_SECONDS` (по умолчанию 1800 с) и закрывается | `campaign-runner` | Строки целей `planned`/`running` в `social_search_queries` | Да: оплаченный прогон, если был, учтён в `campaign_costs`; при повторном включении флага он продолжится по `run_id`, а не запустится заново |
| 2б | То же, откат через `ACQUISITION_MODE=legacy` (P1.8) | `ACQUISITION_MODE=legacy` | Apify-воркер не строится. Если в `SOCIAL_SEARCH_PLATFORMS` есть `instagram`/`tiktok` и есть профиль, браузерный `social_search` доделает оставшиеся цели `planned` той же кампании из `social_search_queries` (раунды считаются вместе). Цель, прерванная в `running`, браузерный `recover` закроет как `failed worker_restarted` | `campaign-runner` | То же | Да |
| 3 | LinkedIn через Apify в поиске инвесторов (P1.8) | `APIFY_LINKEDIN_ENABLED` (только при `ACQUISITION_MODE=apify`) | `APIFY_LINKEDIN_ENABLED=false`: LinkedIn снова ищется через `site:` в SearXNG | `campaign-runner` | Строки в `apify_source_runs`, контакты в `reach_contacts` (без `enriched_at`) | Да: контакты — обычные строки reach |
| 4 | Гигиена поисковых запросов (Q5): сиды только своей сделки, нейтральные сиды и термины для участка, языки запросов из `plan_languages` | `SEARCH_QUERY_HYGIENE` | `SEARCH_QUERY_HYGIENE=false`: сиды и запросы как раньше | `telegram`, `campaign-runner` | Сиды в `plan` уже созданных кампаний остаются как были записаны | Да: новых таблиц и колонок нет |

Остановка `campaign-runner` при включённых Apify-источниках (P1.8). При `APIFY_INSTAGRAM_ENABLED`, `APIFY_TIKTOK_ENABLED`
или `APIFY_LINKEDIN_ENABLED` (с `ACQUISITION_MODE=apify`) поставьте в `.env` `CAMPAIGN_RUNNER_STOP_GRACE=60s`. Так
запуск, начатый до SIGTERM, успеет сохранить `run_id` (до 45 с), и строка не закроется как `launch_unconfirmed` с
потолком в расходах. По умолчанию стоит `10s`, как было. Без флагов `python` работает как PID 1 без обработчика
SIGTERM, и остановка всегда длится весь этот срок и кончается SIGKILL. При откате флагов можно вернуть `10s`.

## 4. Откат миграций (только если без него нельзя)

Не нужен для отката функций: миграции v3 добавляющие. Если всё же нужно убрать схему: **сначала откат кода и
бэкап** (`docs/v2/ROLLBACK.md` §3), затем строго в обратном порядке номеров. Скрипты не применяются автоматически.

```bash
cd /opt/real-estate-bot && set -o pipefail && ./scripts/backup.sh
psql_db() { docker compose --env-file .env exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'; }
psql_db < scripts/rollback/051_apify_source_runs_down.sql
echo "delete from schema_migrations where filename = '051_apify_source_runs.sql';" | psql_db
```

Каждый скрипт откатывается в транзакции и **отказывает** (`raise exception`, ничего не меняет), если данные есть:

| Скрипт | Отказывает, если | Что это значит |
|---|---|---|
| `scripts/rollback/051_apify_source_runs_down.sql` | в `apify_source_runs` есть хотя бы одна строка | был хотя бы один (возможно) платный запуск соц-источника v3 |

Отказ — штатный исход: значит, откат схемы не нужен, оставьте её. Удалять эти данные ради отката не рекомендуется
(это учёт расходов и аудит); если владелец решит иначе — отдельная задача db-агенту с «ок» владельца.
Не запускайте `scripts/update.sh` с веткой v3 после отката схемы: он снова применит 051.
