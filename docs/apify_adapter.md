# Apify-адаптер для Facebook-групп

Альтернативный провайдер для батчей Facebook: **открытые** группы читает Apify-актор (по умолчанию
`apify/facebook-groups-scraper`), закрытые — по-прежнему браузерный сборщик через профиль оператора.
По умолчанию выключен (`FEATURE_APIFY_FACEBOOK=false`). При выключенном флаге поведение системы такое же, как без
адаптера: браузер, ни одного лишнего запроса.

План и решения: `docs/codex_tasks.md`. Код: `bot/facebook_collector/apify/`, `bot/acquisition/`.

## Что он делает и чего не делает

- Посты попадают в `collected_posts` в том же виде, что у браузерного сборщика (`AcquiredPost`,
  `bot/acquisition/contracts.py`). Анализ, правила кампании, дедуп и отчёт работают без изменений.
- Актору не передаются cookies, аккаунт и сессия. Адаптер не вступает в группы и не обходит вход. Закрытую группу
  (актор ответил `private`) он один раз отдаёт браузерному батчу, который читает её через профиль, где аккаунт уже
  состоит.
- Комментарии не собираются, только посты верхнего уровня. Discovery через Apify не делается.
- **Соответствие требованиям:** Apify — сторонний обработчик содержимого групп (имена, телефоны).
  `COMPLIANCE.md` §4 требует письменного решения клиента **до** первого реального прогона.

## Как идёт батч

1. Кампания планирует окно групп как обычно (`bot/campaign/runs.py` → `acquisition_batches` →
   `collector_launch_requests`).
2. `facebook-runner` берёт батч и выбирает провайдера (`bot/facebook_collector/main.py:choose_provider`):
   - `apify` — флаг включён и `campaigns.acquisition_provider='apify'` (или `APIFY_FACEBOOK_SCOPE=all`);
   - иначе `local`. Батч закрытых групп (`requested_by = apify_fallback:<батч>`) всегда идёт через `local`.
3. Apify-провайдер (`provider.py`):
   1. Проверяет бюджеты. Если `CAMPAIGN_BUDGET_USD` исчерпан или потрачено Apify +
      `APIFY_FACEBOOK_MAX_USD_PER_RUN` > `APIFY_FACEBOOK_MAX_USD_PER_CAMPAIGN`, батч завершается `budget_cap`.
      Кампания при этом продолжает остальные источники.
   2. Делает **один** запуск актора на батч (`startUrls`, `resultsLimit`, `viewOption=CHRONOLOGICAL`, закреплённый
      `build`, `maxTotalChargeUsd`). Если ответ на запуск потерян, батч получает `LAUNCH_UNKNOWN` и повторно не
      запускается; лимит записывается в расходы как оценка.
   3. Сохраняет `run_id` сразу, ждёт завершения (по таймауту — abort) и один раз записывает стоимость
      (`campaign_costs`, ключ `apify:<run_id>`, сумма из `usageTotalUsd`).
   4. Импортирует датасет страницами с чекпоинта (`apify_runs.import_checkpoint`). Сырые элементы без полей
      cookie/session/token сохраняются в `apify_raw_items`.
   5. Проставляет статус каждой группы: `ACTIVE`, `ACTIVE/no_posts` (запуск прошёл, новых постов нет) или
      `INACCESSIBLE` (`group_private` / `group_not_found`).
   6. Закрытые группы отдаёт браузеру отдельным батчем (если `APIFY_FACEBOOK_BROWSER_FALLBACK=true`), один раз.

## Настройки (`.env`, сервисы `facebook-collector` и `facebook-runner`)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `FEATURE_APIFY_FACEBOOK` | `false` | Включает провайдер |
| `APIFY_TOKEN` | — | Общий с Idealista. Без него при включённом флаге сервис не стартует. Не логируется |
| `APIFY_FACEBOOK_ACTOR_ID` | `apify/facebook-groups-scraper` | Актор для открытых групп |
| `APIFY_FACEBOOK_ACTOR_BUILD` | — | **Обязателен**, закреплённая сборка, не `latest`. Без неё сервис не стартует |
| `APIFY_FACEBOOK_SCOPE` | `campaign` | `campaign` — только кампании с `acquisition_provider='apify'`; `all` — все |
| `APIFY_FACEBOOK_MAX_POSTS_PER_RUN` | `500` | Постов на один запуск (1–5000) |
| `APIFY_FACEBOOK_MAX_USD_PER_RUN` | `2` | Потолок стоимости одного запуска (передаётся актору) |
| `APIFY_FACEBOOK_MAX_USD_PER_CAMPAIGN` | `10` | Потолок Apify на кампанию (поверх `CAMPAIGN_BUDGET_USD`) |
| `APIFY_FACEBOOK_TIMEOUT_SECONDS` | `600` | Ожидание запуска, потом abort |
| `APIFY_FACEBOOK_BROWSER_FALLBACK` | `true` | Закрытые группы — браузеру |
| `APIFY_FACEBOOK_RAW_RETENTION_DAYS` | `7` | Срок хранения `apify_raw_items` |
| `CAMPAIGN_BUDGET_USD` | `5` | Общий бюджет кампании по всем сервисам |

## Включение для кампании

```sql
update campaigns set acquisition_provider = 'apify' where id = '<campaign uuid>';
```
Или `APIFY_FACEBOOK_SCOPE=all` — для всех кампаний. Обратно — `acquisition_provider = 'local'`.

## Деплой

1. Применить миграции `scripts/apply_migrations.sh`. Нужны 042 (`campaign_costs`), 043–045 (Idealista) и 046
   (`apify_runs`, `apify_raw_items`, метод `apify`, `campaigns.acquisition_provider`).
2. Пересобрать образы `facebook-collector` / `facebook-runner` (в них теперь копируются `bot/acquisition/` и
   `bot/utils/`).
3. Флаг держать выключенным, пока нет решения по `COMPLIANCE.md` §4 и закреплённого билда актора.
4. Ежедневная чистка (cron/systemd-таймер на VPS):
   ```bash
   docker compose run --rm facebook-runner python -m bot.facebook_collector.apify.retention --dry-run
   docker compose run --rm facebook-runner python -m bot.facebook_collector.apify.retention
   ```
   Удаляет только `apify_raw_items` старше срока. `apify_runs` (аудит) и посты не трогает.

## Первый ручной прогон (не в CI)

1. Узнать билд актора в Apify Console и записать в `APIFY_FACEBOOK_ACTOR_BUILD`. Поставить
   `APIFY_FACEBOOK_MAX_POSTS_PER_RUN=50`, `APIFY_FACEBOOK_MAX_USD_PER_RUN=0.5`, `CAMPAIGN_BUDGET_USD=1`.
2. Создать кампанию с **одной** открытой группой, перевести её на `apify` (SQL выше), включить флаг, перезапустить
   `facebook-runner`.
3. Проверить:
   ```sql
   select run_id, status, items_imported, billed_usd, import_checkpoint from apify_runs order by launched_at desc limit 1;
   select stage, kind, code, sum(units), sum(cost_usd) from campaign_costs where campaign_id = '<id>' group by 1,2,3;
   ```
   Должно быть `items_imported > 0`, а стоимость — совпадать с Apify Billing.
4. Сверить имена полей вывода актора с маппером (`mapper.py`: `url`/`postUrl`, `text`/`postText`, `time`/`date`,
   `facebookUrl`/`groupUrl`). Фикстуры тестов написаны по документации, а не по живому прогону.
5. Проверить возобновление: прервать импорт (`docker compose kill facebook-runner` во время импорта) и запустить
   снова. Импорт продолжится с `import_checkpoint`, нового запуска не будет.

## Метрики и логи

Prometheus в проекте нет. Метрики — строки логгера `apify.metrics`: `metric <имя>=<значение> <метки>` с теми же
полями в `extra`:
- `apify_run_launched_total`
- `apify_run_failed_total{reason}`
- `apify_items_imported_total`
- `apify_items_rejected_total{reason}`
- `apify_billed_usd_total`
- `apify_import_duration_seconds`
- `apify_checkpoint_resumed_total`

Метки: `campaign_id`, `batch_id`, `run_id`, `actor_id`, `source_url`, `reason`. Токена, cookies и текста постов в
логах нет (это проверяют тесты). Деньги и ошибки также лежат в `campaign_costs` и видны в итоговом отчёте
(«💶 Расход», «⚠️ Ошибки»).

## Откат

- Выключить: `FEATURE_APIFY_FACEBOOK=false` и перезапустить сервисы Facebook. Всё снова идёт через браузер.
- Схема (только если нужно убрать таблицы): `scripts/rollback/046_facebook_apify_runs_down.sql`. Он не сработает,
  пока есть `acquisition_runs` с методом `apify`. После отката удалить строку 046 из `schema_migrations`.

## Ограничения

- Браузерный батч закрытых групп не входит в окна кампании. Если кампания завершится раньше, его находки до неё не
  дойдут.
- Счётчики circuit breaker и возобновление ожидания запуска (`apify_wait_timeout`) не перезапускают батч
  автоматически: тот же батч при новом запуске продолжит существующий run.
- Команды Telegram для переключения кампании на Apify нет, только SQL или `APIFY_FACEBOOK_SCOPE=all`.

## Комментарии под постом по ссылке (v2 B1)

Флаг `APIFY_FACEBOOK_COMMENTS_ENABLED=false` (так же включает приём ссылки в telegram). Актор
`apify/facebook-comments-scraper` (`APIFY_FACEBOOK_COMMENTS_ACTOR`, обязателен закреплённый
`APIFY_FACEBOOK_COMMENTS_ACTOR_BUILD`), цена $0.0025 за комментарий + $0.001 за старт, запуск с `maxTotalChargeUsd`
(`APIFY_FACEBOOK_COMMENTS_MAX_USD_PER_POST`) и лимитом `APIFY_FACEBOOK_COMMENTS_MAX_PER_POST`. Текст поста берётся
из `postTitle`; `APIFY_FACEBOOK_POSTS_ACTOR` (пусто = выключен, $0.005) нужен только если его нет. Код:
`bot/acquisition/apify_facebook_comments.py`, `bot/campaign/post_leads.py`, таблица `campaign_post_reads` (048).
Кампания-ссылка (`spec.post_link`) не ищет группы, сайты и контакты, только читает пост и присылает лиды.

## Тесты

Только моки, live-запросов в CI нет:
- `tests/test_acquired_post.py`
- `tests/test_facebook_provider.py`
- `tests/test_apify_facebook*.py`
- `tests/test_apify_raw_retention.py`

Postgres-тесты запускаются при `SYSTEM_TEST_DATABASE_URL=…_test`.
