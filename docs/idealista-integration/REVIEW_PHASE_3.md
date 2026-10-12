# Приёмка фаз 2–3

Дата: 2026-10-10. Проверка по HANDOFF/DESIGN/CHECKLIST; живые API и VPS не использовались.

## Что проверено

| Инвариант | Доказательство |
|---|---|
| Однократный платный запуск первого раунда | tests/test_listing_source_worker.py: test_first_round_imports_source_before_html_and_restart_never_launches_twice; test_uncertain_starting_claim_falls_back_without_launch |
| Возобновление известного run, сохранение BUSY результата | test_running_source_resumes_with_queries_already_generated; test_busy_api_import_keeps_offset_and_blocks_html_until_released |
| Успех/ошибки Apify, отсутствие токена в сообщениях, capped body/timeout | tests/test_apify_source.py: synthetic HTTPX tests (48 cases), без живого run |
| Цена не суммируется повторно, фактическая цена не заменяется оценкой | tests/test_costs.py и test_unique_costs_on_postgres_are_one_run_including_concurrent_callbacks |
| Расход сохранённого run не исчезает при crash/time_cap | test_crashed_paid_run_is_booked_and_settled_when_campaign_expires; test_settlement_aborts_existing_run_and_never_starts_an_actor |
| Failed URL можно взять API в той же или другой кампании | tests/test_web_search.py:1209; tests/test_web_search_postgres.py:161 (success/failure cases) |
| Fresh claim ждёт, stale claim восстанавливается | tests/test_web_search.py:1241,1264; tests/test_web_search_postgres.py:197 (также свежий orphan после recover) |
| Прочитанная карточка или snippet не перечитывается | те же тесты и tests/test_web_search_postgres.py:224 |
| API не снимает паузу/удаление источника, не возрождает capped/robots queue | tests/test_web_search.py:1241,1276; tests/test_web_search_postgres.py:224,245; test_paused_portal_does_not_start_paid_source |
| HTML-only block допускает импорт готовых фактов без обхода капчи | API layer в enqueue/begin_fetch; те же policy tests с host_blocked |
| API success — fetched, API failure — failed; HTTP/render отказы и блоки прежние | Memory/реальный Postgres success/failure tests с ненулевыми исходными refusals |
| Источник использует обычный post/analysis путь, нет HTTP fetch API-карточки | test_worker_imports_failed_url_via_api_and_reports_real_read; structured_api_listing_uses_the_existing_post_and_campaign_path |
| Structured API и HTML-unlocker различимы | tests/test_web_layers.py:207; persistent layer scrape не менялся, только статус unlocker |
| Ошибка API видна в пользовательском отчёте | tests/test_final_report.py:test_the_report_shows_the_money_the_skips_and_the_model_errors (api:apify_http_429) |
| Выключение/изменение задачи после рестарта не запускает provider | disabled_restart/changed_task tests; snapshot/run metadata сохраняется с явной причиной |

## Замечания и решения

1. Архитектурное ревью выявило пропавший из расходов незавершённый paid run после time_cap.
   Исправлено: estimated_pending_run записывается перед durable checkpoint, завершение кампании
   пытается bounded GET/abort/billing без нового actor POST. Cleanup failure явно остаётся в журнале;
   provider timeout/cap сохраняет ограничение. Оценка — не утверждение о фактическом списании.
2. В финальном отчёте прежде показывались только llm ошибки, несмотря на записи api в журнале.
   Добавлена отдельная строка API ошибок. «Без затрат» заменено на «без вызовов ИИ», потому что
   отбракованные данные могли уже потребовать платы провайдеру. Полная отчётность источника — фаза 5.
3. API enqueue и begin_fetch оба расширены: одной правки claim недостаточно. Дополнительно защищены
   operator/robots/cap skips, потому что worker импортирует URL из persisted snapshot непосредственно.
4. Frozen SourceListing не валидирует внешний payload; адаптер проверяет schema/URL/host/country/deal/
   municipality/числа. Площадь size неизвестной семантики не становится подтверждённым участком.
5. Оригинальные SQL 001–043 не редактировались; 044 добавлена фазой 2. Фаза 3 не требует новой миграции.
   Новые настройки имеют settings/env example/compose/docs; Dockerfile уже копирует bot целиком.

## Пределы приёмки

Реализованы контракты, адаптер и core store/worker API semantics. Фаза 4 (Scrape.do token/geo/render/super)
и фаза 5 (Idealista API label, pages_api и полное финальное ревью) впереди. Не утверждается готовность
живой земельной схемы, полнота выдачи/пагинация или жёсткое общее ограничение smoke ≤$1. Источник
выключен по умолчанию, ID муниципалитета не угадывается. Нужны подтверждённый fixture, тариф аккаунта
и координация общего денежного бюджета до живого запуска. Fetched snippet upgrade вне этой задачи.

Адресные проверки: Memory/worker/layers 115 passed; реальный Postgres 27 passed; reporting 20 passed;
fresh-orphan claim отдельно 1 passed. Полный финальный pytest: **1508 passed, 0 skipped** за 298.56 s, Ruff/diff-check чисто. Ревью принято; открытые пункты выше относятся к следующим фазам и live gate.
