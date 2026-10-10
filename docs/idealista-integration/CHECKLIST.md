# Final Acceptance Checklist

- [x] ListingSource protocol реализован — `bot/web_search/sources/base.py:28`; SourceListing с отдельным plot_m2, экспорт в sources/__init__.py.
- [x] ApifyIdealistaSource написан и покрыт обработкой ошибок — tests/test_apify_source.py, HTTPX mocks; default-off, live fixture не получен.
- [x] _from_sources создаёт claim/POST только в первом раунде; _resume_sources обрабатывает existing run/cache без нового запуска — tests/test_listing_source_worker.py.
- [x] via/layer=api, pages_api и полная отчётность проверены Memory/реальным PostgreSQL —
  `test_web_search.py`, `test_web_search_postgres.py`, `test_campaign_metrics.py`.
- [x] Failed URL можно повторно захватывать через API — Memory и реальный Postgres, same/other campaign; успешные/snippet URL защищены.
- [x] API success/failure не меняет HTTP/render refusals/blocked_until, не трогает HTML breaker — success/failure tests с исходными ненулевыми отказами.
- [x] В отчётах видно «Idealista (API)» только при API-чтении — `test_campaign_summary.py`,
  `SiteReport.read_api` в обоих stores; общий `site_lines` используют summary и final_report.
- [x] APIFY/Scrape.do Settings/.env.example/compose/docs синхронны; `test_deployment.py` в полном прогоне.
- [x] Базовые тесты проходят — фаза 5: полный pytest **1512 passed, 0 skipped**, Postgres/golden включены; Ruff чисто.
- [x] Mock/system регрессии других порталов прошли в полном pytest фазы 5; live-доступность 20 порталов не утверждается.
- [x] Токены `repr=False`; Scrape.do требует HTTPS, query-token скрыт из info-лога, ошибки без секрета —
  `test_apify_source.py`, `test_web_layers.py`, deployment tests; `.env.example` без ключей.

## Проверка foundation (фаза 1, 2026-10-10)

- [x] CHECK 043 допускает api/NULL и прежние http/render/scrape/none; другие CHECK остаются — `test_listing_source_migration_upgrades_populated_039_and_is_idempotent`.
- [x] Обновление реальной схемы 039 с данными и повторное применение 043 — тот же PostgreSQL-тест.
- [x] Пост API сохраняется с campaign linkage, raw_payload.via и layer; HTML refusals/blocks не меняются — `test_structured_api_listing_uses_the_existing_post_and_campaign_path`.
- [x] Memory сохраняет API-пост/layer и не добавляет refusals — `test_memory_store_persists_structured_api_listing`.
- [x] JSON-LD факты совместимы с prefilter; plot_m2 не подменяется площадью постройки — `test_source_listing_facts_preserve_plot_area_for_analysis_prefilter`.
- [x] Оба списка scripts/apply_migrations.sh перечисляют новую миграцию — `test_migration_script_lists_every_migration_by_its_real_path_in_order` (часть полного прогона).

Это была частичная приёмка foundation на момент фазы 1. Актор, первый раунд, retry failed
и счётчики api затем реализованы и проверены в фазах 2–5; live-проверка остаётся ниже.

## Дополнительная приёмка фаз 2–3

- [x] Durable launch/cache/offset и idempotent billing; нет повторного POST после сбоя.
- [x] Pending paid run имеет явно помеченную оценку и bounded settlement при time_cap/cancel.
- [x] Fresh claim/BUSY не перехватывается, stale claim восстанавливается; готовые факты сохраняются.
- [x] Пауза/удаление источника и robots/cap/operator queue policy сохранены; HTML-only block допускает API import.
- [x] Host success/failure и progress.read правильно учитывают API; persistent scrape не переименован.
- [x] API ошибки видны в финальном отчёте; skip означает отсутствие вызова ИИ, а не отсутствие расходов на источник.

Доказательства и пределы приёмки: [REVIEW_PHASE_3.md](REVIEW_PHASE_3.md),
[REVIEW_PHASE_5.md](REVIEW_PHASE_5.md). План и текущий статус: [SMOKE_TEST.md](SMOKE_TEST.md).

## Live-приёмка (шаги 6–7)

- [ ] Рабочий SSH-доступ к подтверждённому VPS и проверка фактической конфигурации.
- [ ] Миграции до 045 применены на VPS до деплоя; состояние сервиса и откат проверены.
- [ ] Перед платным вызовом подтверждён общий верхний предел $1, включая Apify и OpenRouter.
- [ ] Land/sale/Madrid fixture проверяет муниципалитет, `plot_m2` и смысл `size` без подмены площади.
- [ ] Живой smoke проведён и в SMOKE_TEST.md записаны расходы, отказы URL, воронка и сравнение с прошлым прогоном.
- [ ] После результатов smoke повторно проверены строки этого чек-листа и обновлён HANDOFF.md.
