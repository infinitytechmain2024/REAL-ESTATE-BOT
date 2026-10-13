# План: Apify-адаптер для Facebook-групп (объединённый с планом Idealista/Phase −1)

Версия 1.1 · 2026-10-13 · база `main` @ `f5f1b08` (это merge PR #49, совпадает со «снимком» из версии 1.0).
Исходная версия 1.0 (DeepSeek) сверена с кодом; ниже — реальные пути и правки. Где план 1.0 расходится с кодом,
действует этот файл.

## 0. Что уже есть в `main` и переиспользуется (не писать заново)

| Нужно по плану 1.0 | Уже есть | Где |
|---|---|---|
| Учёт стоимости, бюджет на кампанию | журнал `campaign_costs` (stage `api`), `CAMPAIGN_BUDGET_USD`, `costs.over_budget`, идемпотентный `record_unique` | `bot/utils/costs.py`, миграции 042, 044 |
| Apify-клиент: запуск, ожидание, abort, «не повторять неопределённый запуск», биллинг run, Temporary/Permanent ошибки | `ApifyIdealistaSource._request/_run/search/_bill`, `TemporaryApifyError`, `PermanentApifyError`, `SourceRunContext` | `bot/web_search/sources/apify.py` |
| Чекпоинт импорта и `run_id` до импорта | `web_listing_source_runs` (`run_id`, `dataset_id`, `import_offset`, state) | миграция 044, `bot/web_search/store.py` |
| Расход и ошибки в отчёте | строка «💶 Расход», «⚠️ Ошибки», «Отсеяно до ИИ» | `bot/campaign/final_report.py` |
| Префильтр до LLM, проверка фактов цитатами | `prefilter`, `verify_facts` | `bot/analysis_pipeline/` |
| Дедуп одного объекта с разных источников | `same_object`, кластеры | `bot/campaign/dedup.py`, `runner._deduplicate` |

## 1. Реальные пути (вместо предполагаемых `src/...`)

| План 1.0 | Реально |
|---|---|
| `src/contracts/evidence.py` | `bot/acquisition/` (сейчас там `models.py`); **`Evidence` уже занято**: `bot/analysis_pipeline/models.py:44` — вход LLM-анализа (`post_id, source_id, canonical_url, text, title, published_at, comments`) |
| `src/collectors/base.py`, `facebook_batch_collector.py` | `bot/facebook_collector/collector.py` (`FacebookBatchCollector.run(batch_id) -> str`), `models.py` (`BatchPlan`, `CollectedPost`, `GroupRead`), `store.py` (`PostgresCollectorStore.save_post` → `collected_posts`), `runner.py` (`CollectorRunner(store, run: Callable[[str], Awaitable[str]])`) |
| `src/collectors/apify/*` | `bot/facebook_collector/apify/*` (образ facebook-collector копирует только `bot/facebook_collector/`; добавить `COPY bot/utils/`) |
| `src/pipeline/analysis.py` | `bot/analysis_pipeline/{main,pipeline,openrouter}.py`; вход — строки `collected_posts`, а не DTO |
| `src/pipeline/dedup.py` | `bot/campaign/dedup.py` |
| `src/campaigns/runner.py` | `bot/campaign/runner.py` (`CampaignRunner`), окна Facebook — `bot/campaign/runs.py` (`start_window` → `acquisition_batches` → `collector_launch_requests`) |
| `src/config/settings.py` | настройки по модулям (pydantic-settings): `bot/facebook_collector/settings.py`, `bot/campaign/settings.py` |
| `migrations/XXXX_*.sql` | `bot/services/db/migrations/NNN_*.sql`, сырой SQL, только добавление, применяется `scripts/apply_migrations.sh` (checksum, оба списка); **следующий номер 046** |
| `campaigns.id BIGINT` | `uuid` |
| Prometheus-метрики | нет Prometheus: структурные логи (`log.info("event", extra=...)`) + таблицы `campaign_costs`, `campaign_metrics` |
| CI | `.github/workflows/ci.yml`: ruff, check-imports, pytest (Postgres-тесты по `SYSTEM_TEST_DATABASE_URL`) |

## 2. Задачи

Общие правила: всё за `FEATURE_APIFY_FACEBOOK=false`; секреты только из env (`repr=False`, коды ошибок без
токена); только моки; одна задача = один коммит `feat(apify): T<N> ...`; после каждой — полный pytest + ruff.

### T1. Контракт поста от провайдера
- Новый `bot/acquisition/contracts.py`: `AcquiredPost` (schema `"1"`): `campaign_id`, `source`
  (`provider`, `actor_id`, `run_id`, `group_url`, `post_url`, `published_at`, `collected_at`), `raw_text`,
  `provider_facts: ListingFacts | None`, `quotes: dict[str, str]`.
- Имя **не `Evidence`**: оно занято входом LLM. `ListingFacts` от скрейпера — подсказка, а не факт; цену и
  площадь по-прежнему извлекает и проверяет analysis (`verify_facts`).
- Правила из 1.0 сохраняются: пустая строка → `None`, `price_amount` без `price_currency` → `ValidationError`,
  rent без `price_period` → warning, `phones` как в источнике + опциональный `e164`.
- `to_collected_post(...)` — запись в `collected_posts` в том же виде, что `PostgresCollectorStore.save_post`.
- Тесты 1.0: `test_evidence_requires_currency_with_price`, `..._allows_null_fields`, `..._rejects_empty_string_as_null`
  (переименовать под `AcquiredPost`).

### T2. Шов провайдера Facebook
- Шов уже есть: `CollectorRunner` получает `run(batch_id) -> str`. `AcquisitionProvider` = Protocol
  `run(batch_id) -> str` + `health() -> ProviderHealth` в `bot/facebook_collector/provider.py`.
- `FacebookBatchCollector` удовлетворяет протоколу без изменения логики; сначала тест, что поведение то же.
- Переписывать коллектор на `AsyncIterator[Evidence]` **не надо**: он завязан на браузерную аренду и stop-on-challenge.

### T3. Apify Facebook adapter (только моки)
- `bot/facebook_collector/apify/{client,mapper,provider}.py`.
- Клиент: взять HTTP-, launch- и billing-логику из `bot/web_search/sources/apify.py`, вынеся её в общий
  `bot/acquisition/apify_client.py`. Это рефакторинг Idealista-кода — **только после «ок»**. Без «ок» — копия с
  пометкой «вынести общий клиент» в плане.
- Поведение: 3 попытки с backoff 2/4/8 + jitter только для 5xx/network; 429 по `Retry-After`; неопределённый POST
  запуска не повторяется (статус `LAUNCH_UNKNOWN`); `run_id` сохраняется до импорта; dataset постранично с
  чекпоинтом; «пустой успешный run» ≠ «группа недоступна» (→ `GroupState.INACCESSIBLE`); стоимость run через
  `costs.record_unique("api", key="apify:"+run_id, ...)`.
- Маппер: без догадок по цене/валюте/периоду; пост с несколькими объектами → `quotes["needs_split"]="true"`.
- Тесты 1.0: `test_adapter_does_not_retry_on_launch_unknown`, `..._resumes_from_checkpoint`,
  `..._distinguishes_empty_run_from_inaccessible_group`, `..._maps_currency_missing_to_validation_error`, тест на
  отсутствие токена в логах.

### T4. Миграция `046_facebook_apify_runs.sql`
- `apify_runs` (как в 1.0, но `campaign_id uuid`, `batch_id uuid` → `acquisition_batches`, статусы с CHECK,
  `idempotency` через unique `run_id`), `apify_raw_items` (`run_id`, `item_index`, `payload jsonb`, TTL).
- Расширить CHECK `acquisition_method` значением `'apify'` (сейчас `facebook_connector, agent_ridge, scrapling`).
- Down-миграций в проекте нет (только добавление). Откат — отдельный `docs/apify_adapter.md#rollback` с SQL.
- Внести в оба списка `scripts/apply_migrations.sh`; Postgres-тест.

### T5. Флаг и конфиг
- `bot/facebook_collector/settings.py`: `FEATURE_APIFY_FACEBOOK=false`, `APIFY_TOKEN` (repr=False; уже есть для
  Idealista — тот же), `APIFY_FACEBOOK_ACTOR_ID`, `APIFY_FACEBOOK_ACTOR_BUILD` (обязателен при включении, не
  `latest`), `APIFY_FACEBOOK_MAX_POSTS_PER_RUN`, `APIFY_FACEBOOK_MAX_USD_PER_CAMPAIGN`, `..._TIMEOUT_SECONDS`,
  `..._RAW_RETENTION_DAYS`.
- `.env.example` (пустые ключи) + `docker-compose.yml` (сервис facebook-collector; `tests/test_deployment.py`).
- `true` без токена или без build → ошибка старта сервиса.
- Имя флага одно: `FEATURE_APIFY_FACEBOOK` (в 1.0 встречается и `APIFY_FACEBOOK_ENABLED`).

### T6. Выбор провайдера
- Выбор в `bot/facebook_collector/runner.py:main` (кто исполняет батч), а не в `CampaignRunner`.
- `campaigns.acquisition_provider` — колонка в миграции 046 (`local` по умолчанию).
- Бюджет: до запуска — `costs.over_budget(campaign)` и сумма `apify_runs.billed_usd` против
  `APIFY_FACEBOOK_MAX_USD_PER_CAMPAIGN`.
- Состояния `paused_budget` у кампаний нет. Ввести его — миграция CHECK и переходы `bot/campaign/models.py`. Без
  «ок» — батч завершается с причиной `budget_cap`, кампания продолжает другие источники (как web-стадия сейчас).
- Тесты 1.0: `test_runner_pauses_on_budget_exceeded`, `test_runner_uses_local_by_default`.

### T7. Наблюдаемость
- Prometheus нет. Метрики 1.0 эмитятся как структурные лог-события (`apify.run_launched`, `apify.run_failed`, …)
  с `campaign_id/run_id/source_url` + записи в `campaign_costs` (cost, error, skip).
- Тест: события при mock-прогоне; токена нет ни в одной строке лога.

### T8. Retention
- `scripts/cleanup_apify_raw.py --dry-run`: удаляет `apify_raw_items` старше `APIFY_FACEBOOK_RAW_RETENTION_DAYS`,
  `apify_runs` не трогает. Cron в проекте нет — запуск вручную или по таймеру systemd на VPS, описать в доке.

## 3. Ограничения проекта, которые план 1.0 не учитывал

- `SAFETY_*` и окна ≤ 20 групп относятся к браузерному Facebook. Применять ли их к Apify — решение владельца.
- `COMPLIANCE.md` §3–4: Apify — сторонний обработчик контента Facebook-групп (имена, телефоны). Нужно письменное
  решение клиента до любого реального прогона.
- Агент работает только в `bot/`, `tests/`, `docs/`, `scripts/` и `bot/services/db/migrations/` (каталогов `src/`
  и `migrations/` нет). Плюс правки `docker/facebook-collector.Dockerfile`, `.env.example`, `docker-compose.yml`
  (иначе падают `test_image_imports` и `test_deployment`).

## 4. Связь с планом Idealista

- Idealista через Apify (`axlymxp/idealista-scraper`) и Scrape.do уже в `main` (фазы 0–5, миграции 043–045).
  Остаётся только живой smoke-тест, который ждёт разрешения владельца (`docs/idealista-integration/HANDOFF.md`).
- Общие части, которые Facebook-адаптер должен переиспользовать: журнал расходов, бюджет, классы ошибок Apify,
  идемпотентный биллинг, отчёт. Номера миграций сквозные: Facebook — 046.
